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
from .release import ReleaseAttestor
from .settings import HarnessSettings
from .state import ConversationStore, LongTermMemoryStore
from .telemetry import GovernanceAuditSink


def build_agent(
    profile_id: str,
    *,
    client: Any | None = None,
    settings: HarnessSettings | None = None,
    provider_adapter: ProviderContractAdapter | None = None,
    trusted_tenant_id: str | None = None,
    trusted_project_id: str | None = None,
    idempotency_store: IdempotencyStore | None = None,
    approval_adapter: ApprovalConsumptionAdapter | None = None,
    release_attestor: ReleaseAttestor | None = None,
    conversation_store: ConversationStore | None = None,
    long_term_memory_store: LongTermMemoryStore | None = None,
    audit_sink: GovernanceAuditSink | None = None,
    allow_test_idempotency_store: bool = False,
    allow_test_approval_adapter: bool = False,
    allow_test_release_attestor: bool = False,
) -> Agent:
    return get_factory(profile_id).build(
        client=client,
        settings=settings,
        provider_adapter=provider_adapter,
        trusted_tenant_id=trusted_tenant_id,
        trusted_project_id=trusted_project_id,
        idempotency_store=idempotency_store,
        approval_adapter=approval_adapter,
        release_attestor=release_attestor,
        conversation_store=conversation_store,
        long_term_memory_store=long_term_memory_store,
        audit_sink=audit_sink,
        allow_test_idempotency_store=allow_test_idempotency_store,
        allow_test_approval_adapter=allow_test_approval_adapter,
        allow_test_release_attestor=allow_test_release_attestor,
    )


def run_profile(
    profile_id: str,
    *,
    provider_adapter: ProviderContractAdapter | None = None,
    trusted_tenant_id: str | None = None,
    trusted_project_id: str | None = None,
    idempotency_store: IdempotencyStore | None = None,
    approval_adapter: ApprovalConsumptionAdapter | None = None,
    release_attestor: ReleaseAttestor | None = None,
    conversation_store: ConversationStore | None = None,
    long_term_memory_store: LongTermMemoryStore | None = None,
    audit_sink: GovernanceAuditSink | None = None,
) -> None:
    ResponsesHostServer(
        build_agent(
            profile_id,
            provider_adapter=provider_adapter,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
            idempotency_store=idempotency_store,
            approval_adapter=approval_adapter,
            release_attestor=release_attestor,
            conversation_store=conversation_store,
            long_term_memory_store=long_term_memory_store,
            audit_sink=audit_sink,
        )
    ).run()


def describe_profile(profile_id: str) -> AgentManifest:
    return get_manifest(profile_id)
