from __future__ import annotations

from typing import Any, cast

import pytest
import research_assistant_api.agent_studio.audit_service as audit_service
from research_assistant_api.agent_studio.audit_service import (
    AuditService,
    AuditStoreUnavailableError,
    CosmosAuditStore,
    InMemoryAuditStore,
    build_audit_store,
)
from research_assistant_api.agent_studio.models import AuditEvent, AuditEventKind
from research_assistant_api.agent_studio.scope import ScopeContext, compute_scope_key
from research_assistant_api.config import Settings

TENANT = "demo"
PROJECT = "proj-1"
OTHER_PROJECT = "proj-2"


def _scope(tenant_id: str = TENANT, project_id: str = PROJECT) -> ScopeContext:
    return ScopeContext(tenant_id=tenant_id, project_id=project_id)


def test_in_memory_store_records_and_lists_events_scoped_to_tenant_and_project() -> None:
    store = InMemoryAuditStore()
    same_scope = AuditEvent(
        id="evt-1",
        tenant_id=TENANT,
        project_id=PROJECT,
        logical_agent_id="agent-1",
        kind=AuditEventKind.APPROVAL_DECIDED,
        actor_id="actor-1",
        subject_id="approval-1",
    )
    other_project = AuditEvent(
        id="evt-2",
        tenant_id=TENANT,
        project_id=OTHER_PROJECT,
        logical_agent_id="agent-1",
        kind=AuditEventKind.APPROVAL_DECIDED,
        actor_id="actor-1",
        subject_id="approval-2",
    )
    other_tenant = AuditEvent(
        id="evt-3",
        tenant_id="other-tenant",
        project_id=PROJECT,
        logical_agent_id="agent-1",
        kind=AuditEventKind.APPROVAL_DECIDED,
        actor_id="actor-1",
        subject_id="approval-3",
    )
    store.record(same_scope)
    store.record(other_project)
    store.record(other_tenant)

    events = store.list_events(scope=_scope())
    assert [event.id for event in events] == ["evt-1"]

    # Negative: neither cross-project nor cross-tenant events leak into the query.
    assert store.list_events(scope=_scope(project_id=OTHER_PROJECT)) == (other_project,)
    assert store.list_events(scope=_scope(tenant_id="other-tenant")) == (other_tenant,)


def test_in_memory_store_filters_by_logical_agent_id_and_kind() -> None:
    store = InMemoryAuditStore()
    matching = AuditEvent(
        id="evt-1",
        tenant_id=TENANT,
        project_id=PROJECT,
        logical_agent_id="agent-1",
        kind=AuditEventKind.DEPLOYMENT_ROLLED_BACK,
        actor_id="actor-1",
        subject_id="deployment-1",
    )
    wrong_agent = AuditEvent(
        id="evt-2",
        tenant_id=TENANT,
        project_id=PROJECT,
        logical_agent_id="agent-2",
        kind=AuditEventKind.DEPLOYMENT_ROLLED_BACK,
        actor_id="actor-1",
        subject_id="deployment-2",
    )
    wrong_kind = AuditEvent(
        id="evt-3",
        tenant_id=TENANT,
        project_id=PROJECT,
        logical_agent_id="agent-1",
        kind=AuditEventKind.DEPLOYMENT_ACTIVATED,
        actor_id="actor-1",
        subject_id="deployment-3",
    )
    store.record(matching)
    store.record(wrong_agent)
    store.record(wrong_kind)

    filtered = store.list_events(
        scope=_scope(), logical_agent_id="agent-1", kind=AuditEventKind.DEPLOYMENT_ROLLED_BACK
    )
    assert filtered == (matching,)
    assert store.list_events(scope=_scope(), logical_agent_id="agent-1") == (matching, wrong_kind)
    assert store.list_events(scope=_scope(), kind=AuditEventKind.DEPLOYMENT_ROLLED_BACK) == (matching, wrong_agent)


def test_in_memory_store_list_events_respects_limit_and_order() -> None:
    store = InMemoryAuditStore()
    for index in range(3):
        store.record(
            AuditEvent(
                id=f"evt-{index}",
                tenant_id=TENANT,
                project_id=PROJECT,
                kind=AuditEventKind.RELEASE_CUT,
                actor_id="actor-1",
                subject_id=f"release-{index}",
            )
        )
    events = store.list_events(scope=_scope(), limit=2)
    assert [event.id for event in events] == ["evt-1", "evt-2"]


def test_audit_service_record_mints_id_and_writes_through() -> None:
    store = InMemoryAuditStore()
    service = AuditService(store)
    event = service.record(
        tenant_id=TENANT,
        project_id=PROJECT,
        kind=AuditEventKind.OWNERSHIP_GRANTED,
        actor_id="actor-1",
        subject_id="agent-1::user-2::owner",
        logical_agent_id="agent-1",
        detail={"role": "owner"},
    )
    assert event.id
    assert event.detail == {"role": "owner"}
    assert service.list_events(scope=_scope()) == (event,)


def test_audit_service_record_defaults_detail_to_empty_dict() -> None:
    store = InMemoryAuditStore()
    service = AuditService(store)
    event = service.record(
        tenant_id=TENANT,
        project_id=PROJECT,
        kind=AuditEventKind.ARTIFACT_DELETED,
        actor_id="actor-1",
        subject_id="entry-1",
    )
    assert event.detail == {}
    assert event.logical_agent_id is None


class FakeAuditContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def create_item(self, item: dict[str, Any]) -> None:
        assert item["id"] not in self.documents, "audit events must never be overwritten"
        self.documents[item["id"]] = item

    def query_items(
        self, *, query: str, parameters: list[dict[str, Any]], partition_key: str
    ) -> list[dict[str, Any]]:
        del query
        filters = {item["name"]: item["value"] for item in parameters}
        results: list[dict[str, Any]] = []
        for document in self.documents.values():
            if document.get("scope_key") != partition_key:
                continue
            if "@documentType" in filters and document.get("documentType") != filters["@documentType"]:
                continue
            if "@logicalAgentId" in filters and document.get("logicalAgentId") != filters["@logicalAgentId"]:
                continue
            if "@kind" in filters and document.get("kind") != filters["@kind"]:
                continue
            results.append(document)
        return results


class FakeAuditDatabase:
    def __init__(self) -> None:
        self.container = FakeAuditContainer()

    def get_container_client(self, name: str) -> FakeAuditContainer:
        assert name == "agentStudioAuditV1"
        return self.container


class FakeAuditCosmosClient:
    def __init__(self, endpoint: str, credential: Any) -> None:
        self.endpoint = endpoint
        self.credential = credential
        self.database = FakeAuditDatabase()

    def get_database_client(self, name: str) -> FakeAuditDatabase:
        assert name == "agent-studio"
        return self.database


def test_cosmos_audit_store_records_and_queries_within_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos

    monkeypatch.setattr(azure.cosmos, "CosmosClient", FakeAuditCosmosClient)
    store = CosmosAuditStore("https://cosmos.example.test", "agent-studio", cast("Any", object()))

    matching = AuditEvent(
        id="evt-1",
        tenant_id=TENANT,
        project_id=PROJECT,
        logical_agent_id="agent-1",
        kind=AuditEventKind.APPROVAL_DECIDED,
        actor_id="actor-1",
        subject_id="approval-1",
    )
    other_project = AuditEvent(
        id="evt-2",
        tenant_id=TENANT,
        project_id=OTHER_PROJECT,
        logical_agent_id="agent-1",
        kind=AuditEventKind.APPROVAL_DECIDED,
        actor_id="actor-1",
        subject_id="approval-2",
    )
    store.record(matching)
    store.record(other_project)

    assert store.list_events(scope=_scope()) == (matching,)
    assert store.list_events(scope=_scope(project_id=OTHER_PROJECT)) == (other_project,)
    assert store.list_events(scope=_scope(), logical_agent_id="agent-1") == (matching,)
    assert store.list_events(scope=_scope(), logical_agent_id="agent-missing") == ()
    assert store.list_events(scope=_scope(), kind=AuditEventKind.APPROVAL_DECIDED) == (matching,)
    assert store.list_events(scope=_scope(), kind=AuditEventKind.RELEASE_CUT) == ()

    container = cast(FakeAuditContainer, store._container)
    document = container.documents["evt-1"]
    assert document["documentType"] == "audit_event"
    assert document["scope_key"] == compute_scope_key(TENANT, PROJECT)
    assert document["kind"] == AuditEventKind.APPROVAL_DECIDED.value


def test_cosmos_audit_store_rejects_duplicate_ids() -> None:
    store = CosmosAuditStore.__new__(CosmosAuditStore)
    store._container = FakeAuditContainer()  # type: ignore[assignment]
    event = AuditEvent(
        id="evt-1",
        tenant_id=TENANT,
        project_id=PROJECT,
        kind=AuditEventKind.RELEASE_CUT,
        actor_id="actor-1",
        subject_id="release-1",
    )
    store.record(event)
    with pytest.raises(AssertionError, match="never be overwritten"):
        store.record(event)


def test_build_audit_store_raises_when_cosmos_not_configured() -> None:
    with pytest.raises(AuditStoreUnavailableError, match="unavailable"):
        build_audit_store(Settings(cosmos_endpoint=None))


def test_build_audit_store_returns_cosmos_store_with_default_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos

    captured: dict[str, Any] = {}

    class CapturingClient(FakeAuditCosmosClient):
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential
            super().__init__(endpoint, credential)

    monkeypatch.setattr(azure.cosmos, "CosmosClient", CapturingClient)
    monkeypatch.setattr(audit_service, "azure_credential", lambda _client_id=None: "default-credential")

    store = build_audit_store(Settings(cosmos_endpoint="https://cosmos.example.test"))

    assert isinstance(store, CosmosAuditStore)
    assert captured == {
        "endpoint": "https://cosmos.example.test",
        "credential": "default-credential",
    }


def test_build_audit_store_uses_managed_identity_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos

    captured: dict[str, Any] = {}
    requested: list[Any] = []

    class CapturingClient(FakeAuditCosmosClient):
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential
            super().__init__(endpoint, credential)

    def _credential(client_id: Any = None) -> str:
        requested.append(client_id)
        return f"managed:{client_id}"

    monkeypatch.setattr(azure.cosmos, "CosmosClient", CapturingClient)
    monkeypatch.setattr(audit_service, "azure_credential", _credential)

    store = build_audit_store(
        Settings(
            cosmos_endpoint="https://cosmos.example.test",
            managed_identity_client_id="client-123",
        )
    )

    assert isinstance(store, CosmosAuditStore)
    assert captured == {
        "endpoint": "https://cosmos.example.test",
        "credential": "managed:client-123",
    }
    assert requested == ["client-123"]
