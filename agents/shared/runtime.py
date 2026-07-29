from __future__ import annotations

import os
from typing import Any

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer  # type: ignore[import-untyped]

from shared.credentials import get_credential

from .contracts import AgentManifest
from .factory import get_factory
from .profiles import get_manifest
from .settings import HarnessSettings
from .state import ConversationStore


def build_agent(
    profile_id: str,
    *,
    client: Any | None = None,
    settings: HarnessSettings | None = None,
    trusted_tenant_id: str | None = None,
    trusted_project_id: str | None = None,
    conversation_store: ConversationStore | None = None,
) -> Agent:
    return get_factory(profile_id).build_hosted(
        client=client,
        settings=settings,
        trusted_tenant_id=trusted_tenant_id,
        trusted_project_id=trusted_project_id,
        conversation_store=conversation_store,
    )


def run_profile(
    profile_id: str,
    *,
    trusted_tenant_id: str | None = None,
    trusted_project_id: str | None = None,
    conversation_store: ConversationStore | None = None,
) -> None:
    ResponsesHostServer(
        build_agent(
            profile_id,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
            conversation_store=conversation_store,
        ),
        configure_observability=None,
    ).run()


def describe_profile(profile_id: str) -> AgentManifest:
    return get_manifest(profile_id)


def _build_foundry_client() -> FoundryChatClient:
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    return FoundryChatClient(
        project_endpoint=endpoint,
        model=model,
        credential=get_credential(),
    )
