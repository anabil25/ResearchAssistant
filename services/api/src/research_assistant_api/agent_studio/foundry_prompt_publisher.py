"""Narrow Foundry prompt-agent publication port.

This adapter publishes only a pre-cut Managed Foundry version. It neither
deploys Hosted Agent code nor creates models, connections, or infrastructure.
Durable publication binding and idempotency are intentionally owned by the
control-plane service that invokes this port, not by the SDK adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from research_assistant_core.azure_auth import azure_credential

from research_assistant_api.agent_studio.models import AgentVersion, RuntimeTarget
from research_assistant_api.agent_studio.prompt_agent_compiler import (
    PromptAgentCompilationError,
    compile_prompt_agent_definition,
)
from research_assistant_api.config import Settings


class PromptAgentPublicationError(RuntimeError):
    """Raised when a Studio version cannot be safely published to Foundry."""


@dataclass(frozen=True)
class PromptAgentPublication:
    """The remote result of publishing one immutable Studio version."""

    logical_agent_id: str
    studio_version_id: str
    manifest_hash: str
    remote_agent_name: str
    remote_version: str
    remote_status: str | None


class PromptAgentPublisher(Protocol):
    def publish(self, version: AgentVersion) -> PromptAgentPublication: ...


def _value(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


class AIProjectPromptAgentPublisher:
    """Publishes a compiled prompt-agent definition through ``AIProjectClient``."""

    def __init__(self, endpoint: str, credential: TokenCredential) -> None:
        self._endpoint = endpoint
        self._credential = credential

    def publish(self, version: AgentVersion) -> PromptAgentPublication:
        if version.runtime_target is not RuntimeTarget.MANAGED_FOUNDRY:
            raise PromptAgentPublicationError(
                f"Studio version '{version.id}' targets '{version.runtime_target}' and cannot be "
                "published as a prompt agent."
            )
        try:
            definition = compile_prompt_agent_definition(version.manifest)
        except PromptAgentCompilationError as exc:
            raise PromptAgentPublicationError(str(exc)) from exc

        client = AIProjectClient(endpoint=self._endpoint, credential=self._credential, allow_preview=True)
        try:
            remote = client.agents.create_version(
                version.logical_agent_id,
                definition=definition,
                description=version.manifest.description or None,
                metadata={
                    "agent_studio_version_id": version.id,
                    "agent_studio_manifest_hash": version.manifest_hash,
                },
            )
        except Exception as exc:
            raise PromptAgentPublicationError(
                f"Publishing prompt agent '{version.logical_agent_id}' to Foundry failed."
            ) from exc

        remote_version = _value(getattr(remote, "version", None))
        if not remote_version:
            raise PromptAgentPublicationError(
                f"Foundry did not return a version for prompt agent '{version.logical_agent_id}'."
            )
        remote_agent_name = _value(getattr(remote, "name", None)) or version.logical_agent_id
        return PromptAgentPublication(
            logical_agent_id=version.logical_agent_id,
            studio_version_id=version.id,
            manifest_hash=version.manifest_hash,
            remote_agent_name=remote_agent_name,
            remote_version=remote_version,
            remote_status=_value(getattr(remote, "status", None)),
        )


class UnavailablePromptAgentPublisher:
    """Explicit unavailable port when no Foundry project is configured."""

    def publish(self, version: AgentVersion) -> PromptAgentPublication:
        del version
        raise PromptAgentPublicationError(
            "No Foundry project endpoint is configured; prompt-agent publication is unavailable."
        )


def build_prompt_agent_publisher(settings: Settings) -> PromptAgentPublisher:
    """Build a publisher for the configured project or an explicit unavailable port."""
    if not settings.foundry_project_endpoint:
        return UnavailablePromptAgentPublisher()
    return AIProjectPromptAgentPublisher(
        settings.foundry_project_endpoint,
        azure_credential(settings.managed_identity_client_id),
    )