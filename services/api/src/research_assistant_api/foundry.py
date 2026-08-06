from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from openai import APIStatusError
from research_assistant_core.azure_auth import azure_credential

from research_assistant_api.config import Settings

logger = logging.getLogger("research_assistant.foundry")


class HostedAgentConfigurationError(RuntimeError):
    pass


class HostedAgentNotReadyError(RuntimeError):
    pass


class HostedAgentInvocationError(RuntimeError):
    pass


def parse_hosted_agent_payload(content: str) -> dict[str, Any]:
    """Return the final complete JSON object emitted by a Hosted Agent."""
    decoder = json.JSONDecoder()
    resolved: dict[str, Any] | None = None
    for index, character in enumerate(content):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(content[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            resolved = candidate
    if resolved is None:
        raise HostedAgentInvocationError("Hosted Agent returned no valid JSON object.")
    return resolved


@dataclass(frozen=True, slots=True)
class HostedAgentActivity:
    kind: Literal["approach", "tool"]
    label: str
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HostedAgentReply:
    agent_name: str
    content: str
    response_id: str | None
    activity: tuple[HostedAgentActivity, ...] = ()
    duration_ms: int | None = None
    source_count: int = 0


@dataclass(frozen=True, slots=True)
class HostedAgentProgress:
    type: Literal["activity", "text_delta", "completed"]
    activity_id: str | None = None
    activity: HostedAgentActivity | None = None
    delta: str | None = None
    reply: HostedAgentReply | None = None


SESSION_RETRY_DELAYS = (15, 30, 60)
RESPONSE_POLL_DELAYS = (2, 5, 10)

_RESPONSE_ERROR_MESSAGES = {
    "rate_limit_exceeded": "The agent model is temporarily rate limited. Retry shortly.",
    "server_error": "The Hosted Agent runtime reported an internal error.",
    "invalid_prompt": "The Hosted Agent rejected the request.",
}

_URL_PATTERN = re.compile(r"https://[^\s)\]}>]+")
_TOOL_LABELS = {
    "call_tool": "Research connector",
    "code_interpreter": "Code Interpreter",
    "file_search": "File Search",
    "tool_search": "Tool Search",
    "web_search": "Web Search",
}


def _tool_label(name: str) -> str:
    if name in _TOOL_LABELS:
        return _TOOL_LABELS[name]
    return name.replace("___", " / ").replace("_", " ").title()


def _called_tool_name(item: Any) -> str:
    name = str(getattr(item, "name", "") or "")
    if name != "call_tool":
        return name
    try:
        arguments = json.loads(str(getattr(item, "arguments", "") or ""))
    except json.JSONDecodeError:
        return name
    called = arguments.get("name") if isinstance(arguments, dict) else None
    return called if isinstance(called, str) and called else name


def _tool_activity(item: Any, *, status: str | None = None) -> HostedAgentActivity | None:
    item_type = str(getattr(item, "type", "") or "")
    name = ""
    if item_type == "function_call":
        name = _called_tool_name(item)
    elif item_type == "mcp_call":
        name = str(getattr(item, "name", "") or "")
    elif item_type == "web_search_call":
        name = "web_search"
    elif item_type == "file_search_call":
        name = "file_search"
    elif item_type == "code_interpreter_call":
        name = "code_interpreter"
    elif item_type == "tool_search_call":
        name = "tool_search"
    if not name:
        return None
    return HostedAgentActivity(
        kind="tool",
        label=_tool_label(name),
        status=status or str(getattr(item, "status", "completed") or "completed"),
    )


def response_activity(response: Any) -> tuple[HostedAgentActivity, ...]:
    """Return public summaries and tool names, never raw reasoning or tool data."""
    activity: list[HostedAgentActivity] = []
    for item in getattr(response, "output", ()) or ():
        item_type = str(getattr(item, "type", "") or "")
        status = str(getattr(item, "status", "completed") or "completed")
        if item_type == "reasoning":
            for summary in getattr(item, "summary", ()) or ():
                text = str(getattr(summary, "text", "") or "").strip()
                if text:
                    activity.append(
                        HostedAgentActivity(
                            kind="approach",
                            label="Approach",
                            status=status,
                            detail=text[:500],
                        )
                    )
            continue
        tool_activity = _tool_activity(item, status=status)
        if tool_activity is not None:
            activity.append(tool_activity)
    return tuple(activity[:16])


class _PublicTextStream:
    """Extract user-facing text without streaming a specialist's JSON contract."""

    def __init__(self) -> None:
        self._mode: Literal["unknown", "prose", "structured"] = "unknown"
        self._buffer = ""
        self._summary_started = False
        self._summary_done = False
        self._escaped = False
        self._unicode_escape = ""

    def feed(self, delta: str) -> str:
        if not delta or self._summary_done:
            return ""
        if self._mode == "unknown":
            self._buffer += delta
            stripped = self._buffer.lstrip()
            if not stripped:
                return ""
            self._mode = "structured" if stripped.startswith("{") else "prose"
            if self._mode == "prose":
                public = self._buffer
                self._buffer = ""
                return public
            delta = self._buffer
            self._buffer = ""
        if self._mode == "prose":
            return delta
        return self._structured_delta(delta)

    def _structured_delta(self, delta: str) -> str:
        if not self._summary_started:
            self._buffer += delta
            match = re.search(r'"summary"\s*:\s*"', self._buffer)
            if match is None:
                self._buffer = self._buffer[-80:]
                return ""
            delta = self._buffer[match.end() :]
            self._buffer = ""
            self._summary_started = True

        public: list[str] = []
        escapes = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        for character in delta:
            if self._unicode_escape:
                self._unicode_escape += character
                if len(self._unicode_escape) == 5:
                    with suppress(ValueError):
                        public.append(chr(int(self._unicode_escape[1:], 16)))
                    self._unicode_escape = ""
                    self._escaped = False
                continue
            if self._escaped:
                if character == "u":
                    self._unicode_escape = "u"
                else:
                    public.append(escapes.get(character, character))
                    self._escaped = False
                continue
            if character == "\\":
                self._escaped = True
                continue
            if character == '"':
                self._summary_done = True
                break
            public.append(character)
        return "".join(public)


def stream_response_events(
    client: Any,
    target: str,
    payload: dict[str, Any],
) -> Iterator[HostedAgentProgress]:
    """Stream public response activity while retaining the canonical final reply."""
    session_retries = 0
    while True:
        try:
            stream = client.responses.create(**payload, stream=True)
        except APIStatusError as exc:
            error = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
            is_session_not_ready = (
                exc.status_code == 424
                and isinstance(error, dict)
                and error.get("code") == "session_not_ready"
            )
            if not is_session_not_ready:
                raise HostedAgentInvocationError(
                    f"Hosted Agent {target} invocation failed with status {exc.status_code}."
                ) from exc
            if session_retries == len(SESSION_RETRY_DELAYS):
                raise HostedAgentNotReadyError(
                    f"Hosted Agent {target} did not become ready after bounded retries."
                ) from exc
            time.sleep(SESSION_RETRY_DELAYS[session_retries])
            session_retries += 1
            continue
        break

    started_at = time.monotonic()
    public_text = _PublicTextStream()
    activities: dict[str, HostedAgentActivity] = {}
    call_activities: dict[str, str] = {}
    approach_details: dict[str, str] = {}
    completed = False
    try:
        for event in stream:
            event_type = str(getattr(event, "type", "") or "")
            if event_type == "response.output_item.added":
                item = getattr(event, "item", None)
                activity = _tool_activity(item, status="in_progress")
                activity_id = str(getattr(item, "id", "") or "")
                if activity is not None and activity_id and len(activities) < 16:
                    activities[activity_id] = activity
                    call_id = str(getattr(item, "call_id", "") or "")
                    if call_id:
                        call_activities[call_id] = activity_id
                    yield HostedAgentProgress(
                        type="activity",
                        activity_id=activity_id,
                        activity=activity,
                    )
                continue
            if event_type == "response.output_item.done":
                item = getattr(event, "item", None)
                item_type = str(getattr(item, "type", "") or "")
                call_id = str(getattr(item, "call_id", "") or "")
                if item_type == "function_call_output" and call_id in call_activities:
                    activity_id = call_activities[call_id]
                    previous = activities[activity_id]
                    activity = HostedAgentActivity(
                        kind=previous.kind,
                        label=previous.label,
                        status="completed",
                        detail=previous.detail,
                    )
                    activities[activity_id] = activity
                    yield HostedAgentProgress(
                        type="activity",
                        activity_id=activity_id,
                        activity=activity,
                    )
                    continue
                activity_id = str(getattr(item, "id", "") or "")
                if activity_id in activities:
                    previous = activities[activity_id]
                    resolved = _tool_activity(
                        item,
                        status=(
                            "running"
                            if item_type == "function_call"
                            else str(getattr(item, "status", "completed") or "completed")
                        ),
                    )
                    if resolved is not None:
                        activity = HostedAgentActivity(
                            kind=resolved.kind,
                            label=resolved.label,
                            status=resolved.status,
                            detail=previous.detail,
                        )
                        activities[activity_id] = activity
                        yield HostedAgentProgress(
                            type="activity",
                            activity_id=activity_id,
                            activity=activity,
                        )
                continue
            if event_type == "response.reasoning_summary_text.delta":
                detail = str(getattr(event, "delta", "") or "")
                activity_id = f"approach-{getattr(event, 'item_id', 'current')}"
                if detail and (activity_id in activities or len(activities) < 16):
                    combined = (approach_details.get(activity_id, "") + detail)[:500]
                    approach_details[activity_id] = combined
                    activity = HostedAgentActivity(
                        kind="approach",
                        label="Approach",
                        status="in_progress",
                        detail=combined,
                    )
                    activities[activity_id] = activity
                    yield HostedAgentProgress(
                        type="activity",
                        activity_id=activity_id,
                        activity=activity,
                    )
                continue
            if event_type == "response.reasoning_summary_text.done":
                activity_id = f"approach-{getattr(event, 'item_id', 'current')}"
                if activity_id in activities:
                    previous = activities[activity_id]
                    activity = HostedAgentActivity(
                        kind="approach",
                        label="Approach",
                        status="completed",
                        detail=previous.detail,
                    )
                    activities[activity_id] = activity
                    yield HostedAgentProgress(
                        type="activity",
                        activity_id=activity_id,
                        activity=activity,
                    )
                continue
            if event_type == "response.output_text.delta":
                delta = public_text.feed(str(getattr(event, "delta", "") or ""))
                if delta:
                    yield HostedAgentProgress(type="text_delta", delta=delta)
                continue
            if event_type in {"response.failed", "response.incomplete"}:
                response = getattr(event, "response", None)
                status = str(getattr(response, "status", "failed") or "failed")
                raise HostedAgentInvocationError(_failed_response_detail(response, target, status))
            if event_type == "response.completed":
                response = getattr(event, "response", None)
                if response is None:
                    raise HostedAgentInvocationError(
                        f"Hosted Agent {target} completed without a response."
                    )
                completed = True
                yield HostedAgentProgress(
                    type="completed",
                    reply=build_hosted_agent_reply(response, target, started_at),
                )
                break
    except APIStatusError as exc:
        raise HostedAgentInvocationError(
            f"Hosted Agent {target} stream failed with status {exc.status_code}."
        ) from exc
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    if not completed:
        raise HostedAgentInvocationError(
            f"Hosted Agent {target} stream ended without a terminal response."
        )


def build_hosted_agent_reply(response: Any, target: str, started_at: float) -> HostedAgentReply:
    content = response.output_text.strip()
    return HostedAgentReply(
        agent_name=target,
        content=content,
        response_id=getattr(response, "id", None),
        activity=response_activity(response),
        duration_ms=max(0, round((time.monotonic() - started_at) * 1000)),
        source_count=len(set(_URL_PATTERN.findall(content))),
    )


def _failed_response_detail(response: Any, target: str, status: str) -> str:
    error = getattr(response, "error", None)
    code = getattr(error, "code", None)
    if isinstance(code, str):
        detail = _RESPONSE_ERROR_MESSAGES.get(
            code,
            "The Hosted Agent could not complete the request.",
        )
        return f"Hosted Agent {target} ended with status {status} ({code}). {detail}"
    incomplete = getattr(response, "incomplete_details", None)
    reason = getattr(incomplete, "reason", None)
    if isinstance(reason, str):
        return f"Hosted Agent {target} ended with status {status} ({reason})."
    return f"Hosted Agent {target} response ended with status {status}."


def create_response_with_retries(client: Any, target: str, payload: dict[str, Any]) -> Any:
    """Call ``responses.create`` absorbing the two documented hosted-agent stalls.

    A cold sandbox answers 424 ``session_not_ready`` until the platform has
    provisioned compute, and a warm one can return an empty output before the
    first token lands. Both are transient and bounded; anything else is a real
    failure and is raised immediately.
    """
    session_retries = 0
    while True:
        try:
            response = client.responses.create(**payload)
        except APIStatusError as exc:
            error = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
            is_session_not_ready = (
                exc.status_code == 424 and isinstance(error, dict) and error.get("code") == "session_not_ready"
            )
            if not is_session_not_ready:
                raise HostedAgentInvocationError(
                    f"Hosted Agent {target} invocation failed with status {exc.status_code}."
                ) from exc
            if session_retries == len(SESSION_RETRY_DELAYS):
                raise HostedAgentNotReadyError(
                    f"Hosted Agent {target} did not become ready after bounded retries."
                ) from exc
            time.sleep(SESSION_RETRY_DELAYS[session_retries])
            session_retries += 1
            continue
        break

    response_status = getattr(response, "status", None)
    if response_status in {"failed", "cancelled", "incomplete"}:
        raise HostedAgentInvocationError(
            _failed_response_detail(response, target, response_status)
        )
    if response.output_text.strip():
        return response
    response_id = getattr(response, "id", None)
    if not response_id:
        raise HostedAgentInvocationError(f"Hosted Agent {target} returned no response identifier.")
    for delay in RESPONSE_POLL_DELAYS:
        status = getattr(response, "status", None)
        if status in {"failed", "cancelled", "incomplete"}:
            raise HostedAgentInvocationError(_failed_response_detail(response, target, status))
        logger.info(
            "Hosted Agent %s response %s has no output yet; polling in %s seconds.",
            target,
            response_id,
            delay,
        )
        time.sleep(delay)
        response = client.responses.retrieve(response_id)
        if response.output_text.strip():
            return response
    status = getattr(response, "status", "unknown")
    raise HostedAgentInvocationError(
        f"Hosted Agent {target} returned no output after bounded polling (status {status})."
    )


class HostedAgentGateway:
    def __init__(
        self,
        settings: Settings,
        credential: TokenCredential | None = None,
    ) -> None:
        self._settings = settings
        self._credential = credential or self._build_credential()

    def _build_credential(self) -> TokenCredential:
        return azure_credential(self._settings.managed_identity_client_id)

    def invoke(
        self,
        message: str,
        *,
        agent_name: str | None = None,
    ) -> HostedAgentReply:
        endpoint = self._settings.foundry_project_endpoint
        if not endpoint:
            raise HostedAgentConfigurationError("FOUNDRY_PROJECT_ENDPOINT is required in hosted execution mode")

        target = agent_name or self._settings.coordinator_agent_name
        project = AIProjectClient(
            endpoint=endpoint,
            credential=self._credential,
            allow_preview=True,
        )
        client = project.get_openai_client(agent_name=target)
        started_at = time.monotonic()
        response = create_response_with_retries(client, target, {"input": message})
        return build_hosted_agent_reply(response, target, started_at)
