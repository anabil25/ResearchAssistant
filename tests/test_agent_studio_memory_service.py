from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from research_assistant_api.agent_studio.memory_service import (
    CosmosMemoryStore,
    InMemoryMemoryStore,
    MemoryAccessError,
    MemoryPolicyError,
    MemoryService,
    MemoryStoreUnavailableError,
    _can_read,
    _can_write,
    _is_active,
    build_memory_store,
    validate_memory_scopes,
)
from research_assistant_api.agent_studio.models import (
    AgentManifest,
    AgentOwnerKind,
    MemoryAuditAction,
    MemoryAuditRecord,
    MemoryEntry,
    MemoryMechanism,
    MemoryPolicy,
    MemoryScopeBinding,
    MemoryScopeKind,
)
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


BASE_TIME = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


def _manifest(
    scopes: tuple[MemoryScopeBinding, ...] = (MemoryScopeBinding(kind=MemoryScopeKind.CONVERSATION),),
    *,
    enabled: bool = True,
    logical_agent_id: str = "agent-memory-test",
) -> AgentManifest:
    return AgentManifest(
        logical_agent_id=logical_agent_id,
        tenant_id="demo",
        display_name="Memory Test Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
        memory_policy=MemoryPolicy(enabled=enabled, scopes=scopes),
    )


def _entry(
    *,
    entry_id: str = "entry-1",
    tenant_id: str = "demo",
    scope_kind: MemoryScopeKind = MemoryScopeKind.CONVERSATION,
    scope_id: str = "conv-1",
    logical_agent_id: str = "agent-memory-test",
    content: str = "hello world",
    created_at: datetime = BASE_TIME,
    created_by: str = "creator-1",
    ttl_days: int | None = None,
    expires_at: datetime | None = None,
    read_acl: tuple[str, ...] = (),
    write_acl: tuple[str, ...] = (),
    provenance: str = "user_conversation",
    deleted_at: datetime | None = None,
) -> MemoryEntry:
    return MemoryEntry(
        id=entry_id,
        tenant_id=tenant_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        logical_agent_id=logical_agent_id,
        content=content,
        created_at=created_at,
        created_by=created_by,
        ttl_days=ttl_days,
        expires_at=expires_at,
        read_acl=read_acl,
        write_acl=write_acl,
        provenance=provenance,
        deleted_at=deleted_at,
    )


def _scope_sentinel(scope_kind: MemoryScopeKind = MemoryScopeKind.CONVERSATION, scope_id: str = "conv-1") -> str:
    return f"scope:{scope_kind.value}:{scope_id}"


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
    manifest = _manifest(enabled=False)
    with pytest.raises(MemoryPolicyError, match="disabled"):
        validate_memory_scopes(manifest)


def test_validate_memory_scopes_accepts_ga_mechanisms() -> None:
    validate_memory_scopes(_manifest())


def test_validate_memory_scopes_rejects_non_ga_mechanism() -> None:
    manifest = _manifest(
        (MemoryScopeBinding(kind=MemoryScopeKind.USER, mechanism=MemoryMechanism.FOUNDRY_NATIVE_MEMORY_STORE),)
    )
    with pytest.raises(MemoryPolicyError, match="is not GA"):
        validate_memory_scopes(manifest)


@pytest.mark.parametrize("actor_id, expected", [("creator-1", True), ("reader-1", False), ("stranger", False)])
def test_can_read_creator_only_acl(actor_id: str, expected: bool) -> None:
    assert _can_read(_entry(), actor_id) is expected


@pytest.mark.parametrize("actor_id, expected", [("creator-1", True), ("reader-1", True), ("stranger", False)])
def test_can_read_non_empty_acl_is_additive(actor_id: str, expected: bool) -> None:
    assert _can_read(_entry(read_acl=("reader-1",)), actor_id) is expected


@pytest.mark.parametrize("actor_id, expected", [("creator-1", True), ("writer-1", False), ("stranger", False)])
def test_can_write_creator_only_acl(actor_id: str, expected: bool) -> None:
    assert _can_write(_entry(), actor_id) is expected


@pytest.mark.parametrize("actor_id, expected", [("creator-1", True), ("writer-1", True), ("stranger", False)])
def test_can_write_non_empty_acl_is_additive(actor_id: str, expected: bool) -> None:
    assert _can_write(_entry(write_acl=("writer-1",)), actor_id) is expected


def test_is_active_rejects_deleted_and_expired_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("research_assistant_api.agent_studio.memory_service.utc_now", lambda: BASE_TIME)
    assert _is_active(_entry()) is True
    assert _is_active(_entry(expires_at=BASE_TIME + timedelta(days=1))) is True
    assert _is_active(_entry(expires_at=BASE_TIME - timedelta(days=1))) is False
    assert _is_active(_entry(deleted_at=BASE_TIME + timedelta(minutes=1))) is False


def test_in_memory_store_entry_and_audit_operations() -> None:
    store = InMemoryMemoryStore()
    newer = _entry(entry_id="entry-2", created_at=BASE_TIME + timedelta(minutes=2), content="newer")
    older = _entry(entry_id="entry-1", created_at=BASE_TIME + timedelta(minutes=1), content="older")
    user_entry = _entry(
        entry_id="entry-3",
        scope_kind=MemoryScopeKind.USER,
        scope_id="user-1",
        created_at=BASE_TIME + timedelta(minutes=3),
    )

    store.append(newer)
    store.append(older)
    store.append(user_entry)

    matches = store.list_entries(
        tenant_id="demo",
        scope_kind=MemoryScopeKind.CONVERSATION,
        scope_id="conv-1",
        logical_agent_id="agent-memory-test",
        limit=1,
    )
    assert [entry.id for entry in matches] == ["entry-2"]
    assert store.get_entry(tenant_id="demo", entry_id="entry-1") == older
    assert store.get_entry(tenant_id="other", entry_id="entry-1") is None
    assert store.get_entry(tenant_id="demo", entry_id="missing") is None

    replaced = older.model_copy(update={"content": "updated"})
    assert store.replace_entry(replaced).content == "updated"
    assert store.get_entry(tenant_id="demo", entry_id="entry-1") == replaced

    late = MemoryAuditRecord(
        id="audit-2",
        tenant_id="demo",
        entry_id="entry-1",
        action=MemoryAuditAction.CORRECT,
        actor_id="writer-1",
        created_at=BASE_TIME + timedelta(minutes=2),
    )
    early = MemoryAuditRecord(
        id="audit-1",
        tenant_id="demo",
        entry_id="entry-1",
        action=MemoryAuditAction.REMEMBER,
        actor_id="creator-1",
        created_at=BASE_TIME + timedelta(minutes=1),
    )
    store.record_audit(late)
    store.record_audit(early)

    audit = store.list_audit(tenant_id="demo", entry_id="entry-1")
    assert [record.id for record in audit] == ["audit-1", "audit-2"]
    assert store.list_audit(tenant_id="demo", entry_id="entry-3") == ()


class FakeMemoryContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def upsert_item(self, item: dict[str, Any]) -> None:
        self.documents[item["id"]] = item

    def query_items(
        self, *, query: str, parameters: list[dict[str, Any]], enable_cross_partition_query: bool
    ) -> list[dict[str, Any]]:
        del query, enable_cross_partition_query
        filters = {item["name"]: item["value"] for item in parameters}
        results: list[dict[str, Any]] = []
        for document in self.documents.values():
            if "@documentType" in filters and document.get("documentType") != filters["@documentType"]:
                continue
            if "@tenantId" in filters and document.get("tenantId") != filters["@tenantId"]:
                continue
            if "@scopeKind" in filters and document.get("scopeKind") != filters["@scopeKind"]:
                continue
            if "@scopeId" in filters and document.get("scopeId") != filters["@scopeId"]:
                continue
            if "@logicalAgentId" in filters and document.get("logicalAgentId") != filters["@logicalAgentId"]:
                continue
            if "@id" in filters and document.get("id") != filters["@id"]:
                continue
            if "@entryId" in filters and document.get("entryId") != filters["@entryId"]:
                continue
            results.append(document)
        return results


class FakeMemoryDatabase:
    def __init__(self) -> None:
        self.container = FakeMemoryContainer()

    def get_container_client(self, name: str) -> FakeMemoryContainer:
        assert name == "memory"
        return self.container


class FakeMemoryCosmosClient:
    def __init__(self, endpoint: str, credential: Any) -> None:
        self.endpoint = endpoint
        self.credential = credential
        self.database = FakeMemoryDatabase()

    def get_database_client(self, name: str) -> FakeMemoryDatabase:
        assert name == "agent-studio"
        return self.database


def test_cosmos_memory_store_entry_and_audit_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos

    monkeypatch.setattr(azure.cosmos, "CosmosClient", FakeMemoryCosmosClient)
    store = CosmosMemoryStore("https://cosmos.example.test", "agent-studio", cast("TokenCredential", object()))

    first = _entry(entry_id="entry-1", created_at=BASE_TIME + timedelta(minutes=1))
    second = _entry(entry_id="entry-2", created_at=BASE_TIME + timedelta(minutes=2))
    store.append(second)
    store.append(first)
    store.append(_entry(entry_id="entry-3", tenant_id="other"))

    matches = store.list_entries(
        tenant_id="demo",
        scope_kind=MemoryScopeKind.CONVERSATION,
        scope_id="conv-1",
        logical_agent_id="agent-memory-test",
        limit=1,
    )
    assert [entry.id for entry in matches] == ["entry-2"]
    assert store.get_entry(tenant_id="demo", entry_id="entry-1") == first
    assert store.get_entry(tenant_id="demo", entry_id="missing") is None

    updated = first.model_copy(update={"content": "patched"})
    store.replace_entry(updated)
    assert store.get_entry(tenant_id="demo", entry_id="entry-1") == updated

    remember = MemoryAuditRecord(
        id="audit-1",
        tenant_id="demo",
        entry_id="entry-1",
        action=MemoryAuditAction.REMEMBER,
        actor_id="creator-1",
        created_at=BASE_TIME + timedelta(minutes=1),
    )
    forget = MemoryAuditRecord(
        id="audit-2",
        tenant_id="demo",
        entry_id="entry-1",
        action=MemoryAuditAction.FORGET,
        actor_id="creator-1",
        created_at=BASE_TIME + timedelta(minutes=2),
    )
    store.record_audit(forget)
    store.record_audit(remember)

    audit = store.list_audit(tenant_id="demo", entry_id="entry-1")
    assert [record.id for record in audit] == ["audit-1", "audit-2"]
    assert store.list_audit(tenant_id="demo", entry_id="missing") == ()

    container = cast(FakeMemoryContainer, store._container)
    entry_document = container.documents["entry-1"]
    assert entry_document["documentType"] == "entry"
    assert entry_document["payload"]["content"] == "patched"
    audit_document = container.documents["audit::audit-1"]
    assert audit_document["documentType"] == "audit"
    assert audit_document["entryId"] == "entry-1"


def test_build_memory_store_raises_when_cosmos_not_configured() -> None:
    with pytest.raises(MemoryStoreUnavailableError, match="unavailable"):
        build_memory_store(Settings(cosmos_endpoint=None))


def test_build_memory_store_returns_cosmos_store_with_default_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos
    import azure.identity

    captured: dict[str, Any] = {}

    class CapturingClient(FakeMemoryCosmosClient):
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential
            super().__init__(endpoint, credential)

    monkeypatch.setattr(azure.cosmos, "CosmosClient", CapturingClient)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: "default-credential")

    store = build_memory_store(Settings(cosmos_endpoint="https://cosmos.example.test"))

    assert isinstance(store, CosmosMemoryStore)
    assert captured == {
        "endpoint": "https://cosmos.example.test",
        "credential": "default-credential",
    }


def test_build_memory_store_uses_managed_identity_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos
    import azure.identity

    captured: dict[str, Any] = {}

    class CapturingClient(FakeMemoryCosmosClient):
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential
            super().__init__(endpoint, credential)

    monkeypatch.setattr(azure.cosmos, "CosmosClient", CapturingClient)
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", lambda client_id: f"managed:{client_id}")

    store = build_memory_store(
        Settings(
            cosmos_endpoint="https://cosmos.example.test",
            managed_identity_client_id="client-123",
        )
    )

    assert isinstance(store, CosmosMemoryStore)
    assert captured == {
        "endpoint": "https://cosmos.example.test",
        "credential": "managed:client-123",
    }


@pytest.mark.parametrize("operation", ["remember", "recall", "inspect", "correct", "forget", "export"])
def test_memory_service_rejects_all_operations_when_policy_disabled(operation: str) -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest(enabled=False)
    store.append(_entry())

    with pytest.raises(MemoryPolicyError, match="disabled"):
        if operation == "remember":
            service.remember(manifest, _entry(entry_id="remember"))
        elif operation == "recall":
            service.recall(
                manifest,
                tenant_id="demo",
                scope_kind=MemoryScopeKind.CONVERSATION,
                scope_id="conv-1",
                actor_id="creator-1",
            )
        elif operation == "inspect":
            service.inspect(manifest, tenant_id="demo", entry_id="entry-1", actor_id="creator-1")
        elif operation == "correct":
            service.correct(
                manifest,
                tenant_id="demo",
                entry_id="entry-1",
                actor_id="creator-1",
                content="fixed",
            )
        elif operation == "forget":
            service.forget(manifest, tenant_id="demo", entry_id="entry-1", actor_id="creator-1")
        else:
            service.export(
                manifest,
                tenant_id="demo",
                scope_kind=MemoryScopeKind.CONVERSATION,
                scope_id="conv-1",
                actor_id="creator-1",
            )


def test_memory_service_remember_requires_declared_scope() -> None:
    service = MemoryService(InMemoryMemoryStore())
    manifest = _manifest(scopes=())
    with pytest.raises(MemoryPolicyError, match="does not declare"):
        service.remember(manifest, _entry())


def test_memory_service_remember_rejects_non_ga_manifest() -> None:
    service = MemoryService(InMemoryMemoryStore())
    manifest = _manifest(
        (MemoryScopeBinding(kind=MemoryScopeKind.CONVERSATION, mechanism=MemoryMechanism.FOUNDRY_NATIVE_MEMORY_STORE),)
    )
    with pytest.raises(MemoryPolicyError, match="is not GA"):
        service.remember(manifest, _entry())


def test_memory_service_remember_computes_ttl_and_records_audit() -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest()
    entry = _entry(ttl_days=7)

    stored = service.remember(manifest, entry)

    assert stored.expires_at == BASE_TIME + timedelta(days=7)
    trail = service.audit_trail(tenant_id="demo", entry_id="entry-1")
    assert len(trail) == 1
    assert trail[0].action is MemoryAuditAction.REMEMBER
    assert trail[0].actor_id == "creator-1"
    assert trail[0].detail == "scope=conversation"


def test_memory_service_remember_preserves_explicit_expiry() -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest()
    explicit_expiry = BASE_TIME + timedelta(days=30)

    stored = service.remember(manifest, _entry(entry_id="entry-explicit", ttl_days=7, expires_at=explicit_expiry))

    assert stored.expires_at == explicit_expiry


def test_memory_service_recall_and_export_filter_inactive_and_acl_and_record_scope_audit() -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest()
    visible_to_creator = _entry(entry_id="entry-creator", created_at=BASE_TIME + timedelta(minutes=1))
    visible_via_acl = _entry(
        entry_id="entry-acl",
        created_at=BASE_TIME + timedelta(minutes=2),
        created_by="owner-2",
        read_acl=("reader-1",),
    )
    hidden_creator_only = _entry(
        entry_id="entry-hidden",
        created_at=BASE_TIME + timedelta(minutes=3),
        created_by="owner-3",
    )
    expired = _entry(
        entry_id="entry-expired",
        created_at=BASE_TIME + timedelta(minutes=4),
        expires_at=BASE_TIME - timedelta(minutes=1),
    )
    forgotten = _entry(
        entry_id="entry-forgotten",
        created_at=BASE_TIME + timedelta(minutes=5),
        deleted_at=BASE_TIME + timedelta(minutes=6),
    )
    for entry in (visible_to_creator, visible_via_acl, hidden_creator_only, expired, forgotten):
        service.remember(manifest, entry)

    recalled = service.recall(
        manifest,
        tenant_id="demo",
        scope_kind=MemoryScopeKind.CONVERSATION,
        scope_id="conv-1",
        actor_id="reader-1",
    )
    exported = service.export(
        manifest,
        tenant_id="demo",
        scope_kind=MemoryScopeKind.CONVERSATION,
        scope_id="conv-1",
        actor_id="reader-1",
    )

    assert [entry.id for entry in recalled] == ["entry-acl"]
    assert [entry.id for entry in exported] == ["entry-acl"]

    scope_trail = service.audit_trail(tenant_id="demo", entry_id=_scope_sentinel())
    assert [record.action for record in scope_trail] == [MemoryAuditAction.RECALL, MemoryAuditAction.EXPORT]
    assert [record.detail for record in scope_trail] == ["count=1", "count=1"]


def test_memory_service_inspect_returns_entry_for_authorized_reader() -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest()
    entry = _entry(read_acl=("reader-1",))
    service.remember(manifest, entry)

    inspected = service.inspect(manifest, tenant_id="demo", entry_id="entry-1", actor_id="reader-1")

    assert inspected == entry


def test_memory_service_inspect_rejects_missing_forgotten_and_unauthorized_entries() -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest()
    service.remember(manifest, _entry())
    service.remember(_manifest(), _entry(entry_id="entry-forgotten", deleted_at=BASE_TIME + timedelta(minutes=1)))

    with pytest.raises(MemoryPolicyError, match="not found"):
        service.inspect(manifest, tenant_id="demo", entry_id="missing", actor_id="creator-1")
    with pytest.raises(MemoryPolicyError, match="forgotten"):
        service.inspect(manifest, tenant_id="demo", entry_id="entry-forgotten", actor_id="creator-1")
    with pytest.raises(MemoryAccessError, match="read access"):
        service.inspect(manifest, tenant_id="demo", entry_id="entry-1", actor_id="reader-1")


def test_memory_service_correct_updates_content_and_records_audit() -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest()
    service.remember(manifest, _entry(write_acl=("writer-1",), read_acl=("reader-1",)))

    corrected = service.correct(
        manifest,
        tenant_id="demo",
        entry_id="entry-1",
        actor_id="writer-1",
        content="corrected value",
    )

    assert corrected.content == "corrected value"
    assert corrected.provenance == "operator_correction"
    assert store.get_entry(tenant_id="demo", entry_id="entry-1") == corrected
    trail = service.audit_trail(tenant_id="demo", entry_id="entry-1")
    assert [record.action for record in trail] == [MemoryAuditAction.REMEMBER, MemoryAuditAction.CORRECT]
    assert trail[-1].detail == "content corrected"


def test_memory_service_correct_rejects_missing_forgotten_and_unauthorized_entries() -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest()
    service.remember(manifest, _entry())
    service.remember(_manifest(), _entry(entry_id="entry-forgotten", deleted_at=BASE_TIME + timedelta(minutes=1)))
    service.remember(_manifest(), _entry(entry_id="entry-reader", read_acl=("reader-1",)))

    with pytest.raises(MemoryPolicyError, match="not found"):
        service.correct(manifest, tenant_id="demo", entry_id="missing", actor_id="creator-1", content="fixed")
    with pytest.raises(MemoryPolicyError, match="forgotten"):
        service.correct(manifest, tenant_id="demo", entry_id="entry-forgotten", actor_id="creator-1", content="fixed")
    with pytest.raises(MemoryAccessError, match="write access"):
        service.correct(manifest, tenant_id="demo", entry_id="entry-reader", actor_id="reader-1", content="fixed")


def test_memory_service_forget_soft_deletes_and_records_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest()
    forgotten_at = BASE_TIME + timedelta(days=1)
    monkeypatch.setattr("research_assistant_api.agent_studio.memory_service.utc_now", lambda: forgotten_at)
    service.remember(manifest, _entry(write_acl=("writer-1",)))

    forgotten = service.forget(
        manifest,
        tenant_id="demo",
        entry_id="entry-1",
        actor_id="writer-1",
        reason="user requested deletion",
    )

    assert forgotten.deleted_at == forgotten_at
    assert store.get_entry(tenant_id="demo", entry_id="entry-1") == forgotten
    trail = service.audit_trail(tenant_id="demo", entry_id="entry-1")
    assert [record.action for record in trail] == [MemoryAuditAction.REMEMBER, MemoryAuditAction.FORGET]
    assert trail[-1].detail == "user requested deletion"


def test_memory_service_forget_rejects_unauthorized_actor_but_allows_already_deleted_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest()
    first_deleted_at = BASE_TIME + timedelta(days=1)
    second_deleted_at = BASE_TIME + timedelta(days=2)
    service.remember(manifest, _entry(deleted_at=first_deleted_at))

    with pytest.raises(MemoryAccessError, match="write access"):
        service.forget(manifest, tenant_id="demo", entry_id="entry-1", actor_id="writer-1")

    monkeypatch.setattr("research_assistant_api.agent_studio.memory_service.utc_now", lambda: second_deleted_at)
    forgotten = service.forget(manifest, tenant_id="demo", entry_id="entry-1", actor_id="creator-1")
    assert forgotten.deleted_at == second_deleted_at


@pytest.mark.parametrize("method", ["inspect", "correct", "forget"])
def test_memory_service_cross_agent_entries_are_treated_as_not_found(method: str) -> None:
    store = InMemoryMemoryStore()
    service = MemoryService(store)
    manifest = _manifest(logical_agent_id="agent-memory-test")
    store.append(_entry(logical_agent_id="agent-other"))

    with pytest.raises(MemoryPolicyError, match="not found"):
        if method == "inspect":
            service.inspect(manifest, tenant_id="demo", entry_id="entry-1", actor_id="creator-1")
        elif method == "correct":
            service.correct(
                manifest,
                tenant_id="demo",
                entry_id="entry-1",
                actor_id="creator-1",
                content="fixed",
            )
        else:
            service.forget(manifest, tenant_id="demo", entry_id="entry-1", actor_id="creator-1")
