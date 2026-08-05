from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

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
class HostedAgentReply:
    agent_name: str
    content: str
    response_id: str | None


SESSION_RETRY_DELAYS = (15, 30, 60)
RESPONSE_POLL_DELAYS = (2, 5, 10)

_RESPONSE_ERROR_MESSAGES = {
    "rate_limit_exceeded": "The agent model is temporarily rate limited. Retry shortly.",
    "server_error": "The Hosted Agent runtime reported an internal error.",
    "invalid_prompt": "The Hosted Agent rejected the request.",
}


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
        response = create_response_with_retries(client, target, {"input": message})
        return HostedAgentReply(
            agent_name=target,
            content=response.output_text.strip(),
            response_id=getattr(response, "id", None),
        )
