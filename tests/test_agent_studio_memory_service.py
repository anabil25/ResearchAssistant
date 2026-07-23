from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from research_assistant_api.agent_studio.memory_service import (
    CosmosMemoryStore,
    InMemoryMemoryStore,
    MemoryPolicyError,
    MemoryService,
    MemoryStoreUnavailableError,
    build_memory_store,
    validate_memory_scopes,
)
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    MemoryEntry,
    MemoryMechanism,
    MemoryPolicy,
    MemoryScopeBinding,
    MemoryScopeKind,
)
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


def _manifest(memory_scopes: tuple[MemoryScopeBinding, ...] = (), *, enabled: bool = True) -> AgentManifest:
    return AgentManifest(
        logical_agent_id="agent-memory-test",
        tenant_id="demo",
        display_name="Memory Test Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
        memory_policy=MemoryPolicy(enabled=enabled, scopes=memory_scopes),
    )


def test_memory_policy_defaults_to_disabled_with_no_scopes() -> None:
    manifest = AgentManifest(
        logical_agent_id="agent-memory-default",
        tenant_id="demo",
        display_name="Default Memory Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
    )
    assert manifest.memory_policy.enabled is False
    assert manifest.memory_policy.scopes == ()


def test_validate_memory_scopes_rejects_when_policy_disabled() -> None:
    manifest = _manifest((MemoryScopeBinding(kind=MemoryScopeKind.CONVERSATION),), enabled=False)
    with pytest.raises(MemoryPolicyError, match="disabled"):
        validate_memory_scopes(manifest)


def test_validate_memory_scopes_accepts_ga_mechanisms() -> None:
    manifest = _manifest((MemoryScopeBinding(kind=MemoryScopeKind.CONVERSATION),))
    validate_memory_scopes(manifest)  # does not raise


def test_validate_memory_scopes_rejects_non_ga_mechanism() -> None:
    manifest = _manifest(
        (MemoryScopeBinding(kind=MemoryScopeKind.USER, mechanism=MemoryMechanism.FOUNDRY_NATIVE_MEMORY_STORE),)
    )
    with pytest.raises(MemoryPolicyError, match="is not GA"):
        validate_memory_scopes(manifest)


def _entry(scope_kind: MemoryScopeKind = MemoryScopeKind.CONVERSATION, scope_id: str = "conv-1") -> MemoryEntry:
    return MemoryEntry(
        id="entry-1",
        tenant_id="demo",
        scope_kind=scope_kind,
        scope_id=scope_id,
        logical_agent_id="agent-memory-test",
        content="hello world",
    )


def test_in_memory_store_append_and_list_filters_by_scope() -> None:
    store = InMemoryMemoryStore()
    store.append(_entry())
    store.append(_entry(scope_kind=MemoryScopeKind.USER, scope_id="user-1"))
    matches = store.list_entries(
        tenant_id="demo",
        scope_kind=MemoryScopeKind.CONVERSATION,
        scope_id="conv-1",
        logical_agent_id="agent-memory-test",
    )
    assert len(matches) == 1
    assert matches[0].scope_kind == MemoryScopeKind.CONVERSATION


def test_in_memory_store_respects_limit_and_order() -> None:
    store = InMemoryMemoryStore()
    for index in range(5):
        store.append(
            MemoryEntry(
                id=f"entry-{index}",
                tenant_id="demo",
                scope_kind=MemoryScopeKind.CONVERSATION,
                scope_id="conv-1",
                logical_agent_id="agent-memory-test",
                content=f"message {index}",
            )
        )
    matches = store.list_entries(
        tenant_id="demo", scope_kind=MemoryScopeKind.CONVERSATION, scope_id="conv-1",
        logical_agent_id="agent-memory-test", limit=2,
    )
    assert [entry.id for entry in matches] == ["entry-3", "entry-4"]


def test_build_memory_store_raises_when_cosmos_not_configured() -> None:
    settings = Settings(cosmos_endpoint=None)
    with pytest.raises(MemoryStoreUnavailableError, match="unavailable"):
        build_memory_store(settings)


class FakeMemoryContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def upsert_item(self, item: dict[str, Any]) -> None:
        self.documents[item["id"]] = item

    def query_items(
        self, *, query: str, parameters: list[dict[str, str]], enable_cross_partition_query: bool
    ) -> list[dict[str, Any]]:
        values = {item["name"]: item["value"] for item in parameters}
        results = []
        for item in self.documents.values():
            if item["tenantId"] != values["@tenantId"]:
                continue
            if item["scopeKind"] != values["@scopeKind"]:
                continue
            if item["scopeId"] != values["@scopeId"]:
                continue
            if item["logicalAgentId"] != values["@logicalAgentId"]:
                continue
            results.append(item)
        return results


class FakeMemoryDatabase:
    def __init__(self) -> None:
        self.container = FakeMemoryContainer()

    def get_container_client(self, name: str) -> FakeMemoryContainer:
        assert name == "memory"
        return self.container


class FakeMemoryCosmosClient:
    def __init__(self, endpoint: str, credential: Any) -> None:
        self.database = FakeMemoryDatabase()

    def get_database_client(self, _name: str) -> FakeMemoryDatabase:
        return self.database


def test_cosmos_memory_store_append_and_list(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos

    monkeypatch.setattr(azure.cosmos, "CosmosClient", FakeMemoryCosmosClient)
    store = CosmosMemoryStore(
        "https://cosmos.example.test", "agent-studio", cast("TokenCredential", object())
    )
    store.append(_entry())
    matches = store.list_entries(
        tenant_id="demo", scope_kind=MemoryScopeKind.CONVERSATION, scope_id="conv-1",
        logical_agent_id="agent-memory-test",
    )
    assert len(matches) == 1
    assert matches[0].content == "hello world"

    no_match = store.list_entries(
        tenant_id="demo", scope_kind=MemoryScopeKind.USER, scope_id="conv-1",
        logical_agent_id="agent-memory-test",
    )
    assert no_match == ()


def test_build_memory_store_returns_cosmos_store_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos

    monkeypatch.setattr(azure.cosmos, "CosmosClient", FakeMemoryCosmosClient)
    settings = Settings(cosmos_endpoint="https://cosmos.example.test")
    store = build_memory_store(settings)
    assert isinstance(store, CosmosMemoryStore)


def test_build_memory_store_uses_managed_identity_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos
    import azure.identity

    captured: dict[str, Any] = {}

    class _CapturingClient(FakeMemoryCosmosClient):
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["credential"] = credential
            super().__init__(endpoint, credential)

    monkeypatch.setattr(azure.cosmos, "CosmosClient", _CapturingClient)
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", lambda client_id: f"managed:{client_id}")
    settings = Settings(
        cosmos_endpoint="https://cosmos.example.test", managed_identity_client_id="client-123"
    )
    build_memory_store(settings)
    assert captured["credential"] == "managed:client-123"


def test_memory_service_remember_requires_declared_scope() -> None:
    service = MemoryService(InMemoryMemoryStore())
    manifest = _manifest()  # no memory scopes declared
    with pytest.raises(MemoryPolicyError, match="does not declare"):
        service.remember(manifest, _entry())


def test_memory_service_remember_rejects_non_ga_manifest() -> None:
    service = MemoryService(InMemoryMemoryStore())
    manifest = _manifest(
        (MemoryScopeBinding(kind=MemoryScopeKind.CONVERSATION, mechanism=MemoryMechanism.FOUNDRY_NATIVE_MEMORY_STORE),)
    )
    with pytest.raises(MemoryPolicyError, match="is not GA"):
        service.remember(manifest, _entry())


def test_memory_service_remember_and_recall_round_trip() -> None:
    service = MemoryService(InMemoryMemoryStore())
    manifest = _manifest((MemoryScopeBinding(kind=MemoryScopeKind.CONVERSATION),))
    service.remember(manifest, _entry())
    recalled = service.recall(
        manifest, tenant_id="demo", scope_kind=MemoryScopeKind.CONVERSATION, scope_id="conv-1"
    )
    assert len(recalled) == 1
    assert recalled[0].content == "hello world"


def test_memory_service_recall_rejects_non_ga_manifest() -> None:
    service = MemoryService(InMemoryMemoryStore())
    manifest = _manifest(
        (MemoryScopeBinding(kind=MemoryScopeKind.CONVERSATION, mechanism=MemoryMechanism.FOUNDRY_NATIVE_MEMORY_STORE),)
    )
    with pytest.raises(MemoryPolicyError, match="is not GA"):
        service.recall(manifest, tenant_id="demo", scope_kind=MemoryScopeKind.CONVERSATION, scope_id="conv-1")
