from __future__ import annotations

from typing import Any

from agent_framework import Agent
from agent_framework_foundry_hosting import ResponsesHostServer  # type: ignore[import-untyped]

from .approvals import ApprovalConsumptionAdapter
from .capabilities import ProviderContractAdapter
from .contracts import AgentManifest
from .factory import get_factory
from .idempotency import IdempotencyStore
from .profiles import get_manifest
from .settings import HarnessSettings


def build_agent(
    profile_id: str,
    *,
    client: Any | None = None,
    settings: HarnessSettings | None = None,
    provider_adapter: ProviderContractAdapter | None = None,
    idempotency_store: IdempotencyStore | None = None,
    approval_adapter: ApprovalConsumptionAdapter | None = None,
) -> Agent:
    return get_factory(profile_id).build(
        client=client,
        settings=settings,
        provider_adapter=provider_adapter,
        idempotency_store=idempotency_store,
        approval_adapter=approval_adapter,
    )


def run_profile(
    profile_id: str,
    *,
    provider_adapter: ProviderContractAdapter | None = None,
    idempotency_store: IdempotencyStore | None = None,
    approval_adapter: ApprovalConsumptionAdapter | None = None,
) -> None:
    ResponsesHostServer(
        build_agent(
            profile_id,
            provider_adapter=provider_adapter,
            idempotency_store=idempotency_store,
            approval_adapter=approval_adapter,
        )
    ).run()


def describe_profile(profile_id: str) -> AgentManifest:
    return get_manifest(profile_id)
