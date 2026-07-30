from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from openai import APIStatusError

from research_assistant_api.config import Settings

logger = logging.getLogger("research_assistant.foundry")


class HostedAgentConfigurationError(RuntimeError):
    pass


class HostedAgentNotReadyError(RuntimeError):
    pass


class HostedAgentInvocationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HostedAgentReply:
    agent_name: str
    content: str
    response_id: str | None


SESSION_RETRY_DELAYS = (15, 30, 60)
EMPTY_OUTPUT_RETRY_DELAYS = (2, 5)


def create_response_with_retries(client: Any, target: str, payload: dict[str, Any]) -> Any:
    """Call ``responses.create`` absorbing the two documented hosted-agent stalls.

    A cold sandbox answers 424 ``session_not_ready`` until the platform has
    provisioned compute, and a warm one can return an empty output before the
    first token lands. Both are transient and bounded; anything else is a real
    failure and is raised immediately.
    """
    session_retries = 0
    empty_output_retries = 0
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
        if response.output_text.strip():
            return response
        if empty_output_retries == len(EMPTY_OUTPUT_RETRY_DELAYS):
            raise HostedAgentInvocationError(f"Hosted Agent {target} returned no output after bounded retries.")
        delay = EMPTY_OUTPUT_RETRY_DELAYS[empty_output_retries]
        logger.warning(
            "Hosted Agent %s returned empty output for response %s; retrying in %s seconds.",
            target,
            getattr(response, "id", "unknown"),
            delay,
        )
        time.sleep(delay)
        empty_output_retries += 1


class HostedAgentGateway:
    def __init__(
        self,
        settings: Settings,
        credential: TokenCredential | None = None,
    ) -> None:
        self._settings = settings
        self._credential = credential or self._build_credential()

    def _build_credential(self) -> TokenCredential:
        if self._settings.managed_identity_client_id:
            return ManagedIdentityCredential(client_id=self._settings.managed_identity_client_id)
        return DefaultAzureCredential()

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
