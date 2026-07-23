from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from openai import APIStatusError
from pydantic import BaseModel, ConfigDict, Field

from .errors import DeadlineExceededError, InvocationError, RetryableInvocationError


class HostedInvocationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_retry_delays: tuple[float, ...] = (15, 30, 60)
    empty_output_retry_delays: tuple[float, ...] = (2, 5)
    timeout_seconds: float = Field(default=120, gt=0, le=600)


class HostedAgentReply(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_name: str
    content: str
    response_id: str | None = None


class RetryingResponsesInvoker:
    def __init__(
        self,
        policy: HostedInvocationPolicy | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._policy = policy or HostedInvocationPolicy()
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

    def invoke(
        self,
        client: Any,
        request: str,
        agent_name: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> HostedAgentReply:
        started = self._monotonic()
        deadline = deadline_monotonic or started + self._policy.timeout_seconds
        session_retries = 0
        empty_retries = 0
        bounded_client = self._without_sdk_retries(client, agent_name)
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise DeadlineExceededError(
                    "Hosted Agent invocation deadline expired",
                    context={"agent_name": agent_name},
                )
            try:
                response = bounded_client.responses.create(
                    input=request,
                    timeout=remaining,
                )
            except APIStatusError as exc:
                if not _is_session_not_ready(exc):
                    raise InvocationError(
                        "Hosted Agent invocation failed",
                        context={"agent_name": agent_name, "status": str(exc.status_code)},
                    ) from exc
                if session_retries == len(self._policy.session_retry_delays):
                    raise RetryableInvocationError(
                        "Hosted Agent did not become ready after bounded retries",
                        context={"agent_name": agent_name},
                    ) from exc
                self._sleep_bounded(
                    self._policy.session_retry_delays[session_retries],
                    deadline,
                    agent_name,
                )
                session_retries += 1
                continue
            if self._monotonic() >= deadline:
                raise DeadlineExceededError(
                    "Hosted Agent invocation exceeded its deadline",
                    context={"agent_name": agent_name},
                )
            raw_output = getattr(response, "output_text", None)
            output = raw_output.strip() if isinstance(raw_output, str) else ""
            if output:
                return HostedAgentReply(
                    agent_name=agent_name,
                    content=output,
                    response_id=getattr(response, "id", None),
                )
            if empty_retries == len(self._policy.empty_output_retry_delays):
                raise InvocationError(
                    "Hosted Agent returned no output after bounded retries",
                    context={"agent_name": agent_name},
                )
            self._sleep_bounded(
                self._policy.empty_output_retry_delays[empty_retries],
                deadline,
                agent_name,
            )
            empty_retries += 1

    @staticmethod
    def _without_sdk_retries(client: Any, agent_name: str) -> Any:
        with_options = getattr(client, "with_options", None)
        if not callable(with_options):
            raise InvocationError(
                "Hosted Agent client cannot enforce bounded SDK retries",
                context={"agent_name": agent_name},
            )
        return with_options(max_retries=0)

    def _sleep_bounded(self, delay: float, deadline: float, agent_name: str) -> None:
        if self._monotonic() + delay >= deadline:
            raise DeadlineExceededError(
                "Hosted Agent retry would exceed the invocation deadline",
                context={"agent_name": agent_name},
            )
        self._sleep(delay)


def _is_session_not_ready(exc: APIStatusError) -> bool:
    error = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
    return exc.status_code == 424 and isinstance(error, dict) and error.get("code") == "session_not_ready"
