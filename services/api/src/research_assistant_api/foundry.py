from __future__ import annotations

import logging
import time
from dataclasses import dataclass

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
        session_retry_delays = (15, 30, 60)
        empty_output_retry_delays = (2, 5)
        session_retries = 0
        empty_output_retries = 0
        while True:
            try:
                response = client.responses.create(input=message)
            except APIStatusError as exc:
                error = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
                is_session_not_ready = (
                    exc.status_code == 424
                    and isinstance(error, dict)
                    and error.get("code") == "session_not_ready"
                )
                if not is_session_not_ready:
                    raise HostedAgentInvocationError(
                        f"Hosted Agent {target} invocation failed with status "
                        f"{exc.status_code}."
                    ) from exc
                if session_retries == len(session_retry_delays):
                    raise HostedAgentNotReadyError(
                        f"Hosted Agent {target} did not become ready after "
                        "bounded retries."
                    ) from exc
                time.sleep(session_retry_delays[session_retries])
                session_retries += 1
                continue
            output_text = response.output_text.strip()
            if output_text:
                return HostedAgentReply(
                    agent_name=target,
                    content=output_text,
                    response_id=getattr(response, "id", None),
                )
            if empty_output_retries == len(empty_output_retry_delays):
                raise HostedAgentInvocationError(
                    f"Hosted Agent {target} returned no output after bounded retries."
                )
            delay = empty_output_retry_delays[empty_output_retries]
            logger.warning(
                "Hosted Agent %s returned empty output for response %s; retrying in %s seconds.",
                target,
                getattr(response, "id", "unknown"),
                delay,
            )
            time.sleep(delay)
            empty_output_retries += 1
