from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
import research_assistant_api.agent_studio.cosmos_store as cosmos_store
from azure.core.credentials import AccessToken, TokenCredential
from azure.cosmos.exceptions import CosmosHttpResponseError
from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRole,
    AgentVersion,
    AgentVersionStatus,
    ApprovalKind,
    ApprovalState,
    DeploymentEnvironment,
    DeploymentRecord,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    ReleaseGateReport,
    RuntimeTarget,
    StudioApprovalRecord,
)
from research_assistant_api.agent_studio.store import AgentStudioStoreError
from research_assistant_api.config import Settings


class FakeCredential(TokenCredential):
    def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        enable_cae: bool = False,
        **kwargs: Any,
    ) -> AccessToken:
        return AccessToken("fake", int(datetime.now(UTC).timestamp()) + 3600)


class FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.version = 0
        self.fail_replace_status: int | None = None

    def upsert_item(self, item: dict[str, Any]) -> dict[str, Any]:
        self.version += 1
        stored = deepcopy(item)
        stored["_etag"] = str(self.version)
        self.documents[item["id"]] = stored
        return deepcopy(stored)

    def replace_item(
        self,
        *,
        item: str,
        body: dict[str, Any],
        etag: str | None,
        match_condition: Any,
    ) -> dict[str, Any]:
        if self.fail_replace_status is not None:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=self.fail_replace_status, message="simulated failure"
            )
        assert self.documents[item]["_etag"] == etag
        return self.upsert_item(body)

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, str]],
        enable_cross_partition_query: bool,
    ) -> list[dict[str, Any]]:
        values = {item["name"]: item["value"] for item in parameters}
        results = []
        for item in self.documents.values():
            if "@documentType" in values and item.get("documentType") != values["@documentType"]:
                continue
            if "@tenantId" in values and item.get("tenantId") != values["@tenantId"]:
                continue
            if "@id" in values and item.get("id") != values["@id"]:
                continue
            results.append(deepcopy(item))
        return results


class FakeDatabase:
    def __init__(self) -> None:
        self.containers = {
            "manifests": FakeContainer(),
            "versions": FakeContainer(),
            "governance": FakeContainer(),
        }

    def get_container_client(self, name: str) -> FakeContainer:
        return self.containers[name]


class FakeCosmosClient:
    def __init__(self) -> None:
        self.database = FakeDatabase()

    def get_database_client(self, _name: str) -> FakeDatabase:
        return self.database


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> FakeCosmosClient:
    client = FakeCosmosClient()
    monkeypatch.setattr(cosmos_store, "CosmosClient", lambda _endpoint, credential: client)
    return client


def _new_store(fake_client: FakeCosmosClient) -> cosmos_store.CosmosAgentStudioStore:
    return cosmos_store.CosmosAgentStudioStore("https://cosmos.example.test", "agent-studio", FakeCredential())


def _manifest() -> AgentManifest:
    return AgentManifest(
        logical_agent_id="agent-cosmos-test",
        tenant_id="demo",
        display_name="Cosmos Test Agent",
        owner_kind=AgentOwnerKind.USER,
        owner_id="user-1",
    )


def test_persistence_label_is_cosmos(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    assert store.persistence == "Azure Cosmos DB"


def test_draft_persists_and_reloads_across_store_instances(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    draft = AgentDraft(
        logical_agent_id="agent-cosmos-test",
        tenant_id="demo",
        manifest=_manifest(),
        updated_by="user-1",
    )
    first.save_draft(draft)

    second = _new_store(fake_client)
    reloaded = second.get_draft("demo", "agent-cosmos-test")
    assert reloaded is not None
    assert reloaded.manifest.display_name == "Cosmos Test Agent"
    assert len(second.list_drafts("demo")) == 1
    assert second.get_draft("other-tenant", "agent-cosmos-test") is None


def test_ownership_persists_and_role_for_reloads(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    first.grant_ownership(
        OwnershipGrant(
            tenant_id="demo", logical_agent_id="agent-1", principal_id="user-1",
            role=AgentRole.OWNER,
            granted_by="admin",
        )
    )
    second = _new_store(fake_client)
    assert second.role_for("demo", "agent-1", "user-1") == AgentRole.OWNER
    assert len(second.list_ownership("demo", "agent-1")) == 1
    # Re-listing must not duplicate already-cached grants.
    assert len(second.list_ownership("demo", "agent-1")) == 1


def _version(**overrides: object) -> AgentVersion:
    base = dict(
        id="version-1",
        logical_agent_id="agent-cosmos-test",
        tenant_id="demo",
        sequence=1,
        manifest=_manifest(),
        manifest_hash="hash",
        created_by="user-1",
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
    )
    base.update(overrides)
    return AgentVersion(**base)  # type: ignore[arg-type]


def test_version_persists_and_reloads(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    first.create_version(_version())

    second = _new_store(fake_client)
    assert len(second.list_versions("demo", "agent-cosmos-test")) == 1
    # Re-listing must not duplicate an already-cached version.
    assert len(second.list_versions("demo", "agent-cosmos-test")) == 1
    assert second.get_version("demo", "version-1") is not None
    assert second.get_version("demo", "missing") is None

    third = _new_store(fake_client)
    assert third.get_version("demo", "version-1") is not None


def test_update_version_status_persists_across_instances(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    first.create_version(_version())
    first.update_version_status("demo", "version-1", AgentVersionStatus.GATED)

    second = _new_store(fake_client)
    reloaded = second.get_version("demo", "version-1")
    assert reloaded is not None
    assert reloaded.status == AgentVersionStatus.GATED


def test_update_version_status_raises_for_missing_version(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.update_version_status("demo", "missing", AgentVersionStatus.GATED)


def test_update_version_status_wraps_concurrent_modification(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_version(_version())
    fake_client.database.containers["versions"].fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="changed concurrently"):
        store.update_version_status("demo", "version-1", AgentVersionStatus.GATED)


def test_update_version_status_reraises_non_conflict_errors(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_version(_version())
    fake_client.database.containers["versions"].fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        store.update_version_status("demo", "version-1", AgentVersionStatus.GATED)


def test_attach_gate_report_persists_and_raises_for_missing(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_version(_version())
    updated = store.attach_gate_report("demo", "version-1", "report-1")
    assert updated.gate_report_id == "report-1"
    with pytest.raises(AgentStudioStoreError, match="not found"):
        store.attach_gate_report("demo", "missing", "report-1")


def test_attach_gate_report_wraps_concurrent_modification(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_version(_version())
    fake_client.database.containers["versions"].fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="changed concurrently"):
        store.attach_gate_report("demo", "version-1", "report-1")


def test_attach_gate_report_reraises_non_conflict_errors(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_version(_version())
    fake_client.database.containers["versions"].fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        store.attach_gate_report("demo", "version-1", "report-1")


def test_lineage_persists_and_reloads(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    edge = LineageEdge(
        tenant_id="demo",
        child_logical_agent_id="agent-child",
        child_version_id="v-child",
        parent_logical_agent_id="agent-parent",
        parent_version_id="v-parent",
    )
    first.add_lineage_edge(edge)

    second = _new_store(fake_client)
    assert second.list_lineage("demo", "agent-child") == (edge,)
    # Re-listing must not duplicate.
    assert len(second.list_lineage("demo", "agent-child")) == 1


def test_gate_report_persists_and_reloads(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    report = ReleaseGateReport(id="report-1", version_id="version-1", results=())
    first.save_gate_report(report)

    second = _new_store(fake_client)
    assert second.get_gate_report("report-1") == report
    assert second.get_gate_report("missing") is None
    # Second lookup of the same id must hit the local cache, not re-query Cosmos.
    assert second.get_gate_report("report-1") == report


def _approval(**overrides: object) -> StudioApprovalRecord:
    base: dict[str, object] = dict(
        id="approval-1",
        version_id="version-1",
        tenant_id="demo",
        kind=ApprovalKind.RELEASE_PROMOTION,
        gated_action="promote_version",
        destination="prod",
        requested_by="user-1",
        evidence_summary="Evidence.",
        risk="medium",
        idempotency_key="key-1",
    )
    base.update(overrides)
    return StudioApprovalRecord(**base)  # type: ignore[arg-type]


def test_create_approval_persists_and_is_idempotent(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    created = first.create_approval(_approval())
    duplicate = first.create_approval(_approval())
    assert duplicate.id == created.id
    assert len(first.list_approvals("demo")) == 1

    second = _new_store(fake_client)
    assert second.get_approval("demo", "approval-1") is not None


def test_save_approval_decision_persists_and_raises_appropriately(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    first.create_approval(_approval())
    decided = _approval().model_copy(update={"state": ApprovalState.APPROVED})
    first.save_approval_decision(decided)

    second = _new_store(fake_client)
    reloaded = second.get_approval("demo", "approval-1")
    assert reloaded is not None
    assert reloaded.state.value == "approved"

    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        second.save_approval_decision(decided)

    fresh_store = _new_store(fake_client)
    with pytest.raises(AgentStudioStoreError, match="not found"):
        fresh_store.save_approval_decision(_approval(id="missing-approval"))


def test_save_approval_decision_wraps_concurrent_modification(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_approval(_approval())
    fake_client.database.containers["governance"].fail_replace_status = 412
    decided = _approval().model_copy(update={"state": ApprovalState.APPROVED})
    with pytest.raises(AgentStudioStoreError, match="decided concurrently"):
        store.save_approval_decision(decided)


def test_save_approval_decision_reraises_non_conflict_errors(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_approval(_approval())
    fake_client.database.containers["governance"].fail_replace_status = 500
    decided = _approval().model_copy(update={"state": ApprovalState.APPROVED})
    with pytest.raises(CosmosHttpResponseError):
        store.save_approval_decision(decided)


def _deployment(**overrides: object) -> DeploymentRecord:
    base: dict[str, object] = dict(
        id="deployment-1",
        logical_agent_id="agent-cosmos-test",
        tenant_id="demo",
        version_id="version-1",
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by="user-1",
    )
    base.update(overrides)
    return DeploymentRecord(**base)  # type: ignore[arg-type]


def test_deployment_persists_and_reloads(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    first.create_deployment(_deployment())

    second = _new_store(fake_client)
    assert len(second.list_deployments("demo", "agent-cosmos-test")) == 1
    # Re-listing must not duplicate an already-cached deployment.
    assert len(second.list_deployments("demo", "agent-cosmos-test")) == 1
    assert second.get_deployment("demo", "deployment-1") is not None
    assert second.get_deployment("demo", "missing") is None


def test_update_deployment_persists_and_raises_for_missing(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    first.create_deployment(_deployment())
    updated = first.update_deployment(_deployment().model_copy(update={"trace_ref": "trace-1"}))
    assert updated.trace_ref == "trace-1"

    second = _new_store(fake_client)
    reloaded = second.get_deployment("demo", "deployment-1")
    assert reloaded is not None

    with pytest.raises(AgentStudioStoreError, match="not found"):
        second.update_deployment(_deployment(id="missing-deployment"))


def test_update_deployment_wraps_concurrent_modification(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_deployment(_deployment())
    fake_client.database.containers["governance"].fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="changed concurrently"):
        store.update_deployment(_deployment().model_copy(update={"trace_ref": "trace-1"}))


def test_update_deployment_reraises_non_conflict_errors(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_deployment(_deployment())
    fake_client.database.containers["governance"].fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        store.update_deployment(_deployment().model_copy(update={"trace_ref": "trace-1"}))


def test_binding_persists_and_reloads(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    binding = LogicalAgentBinding(
        logical_agent_id="agent-cosmos-test",
        tenant_id="demo",
        environment=DeploymentEnvironment.DEVELOPMENT,
        resolved_version_id="version-1",
        updated_by="user-1",
    )
    first.set_binding(binding)

    second = _new_store(fake_client)
    reloaded = second.get_binding("demo", "agent-cosmos-test", DeploymentEnvironment.DEVELOPMENT)
    assert reloaded is not None
    assert reloaded.resolved_version_id == "version-1"
    assert second.get_binding("demo", "agent-cosmos-test", DeploymentEnvironment.DEVELOPMENT) is not None
    assert second.get_binding("other-tenant", "agent-cosmos-test", DeploymentEnvironment.DEVELOPMENT) is None


def test_build_agent_studio_store_raises_when_cosmos_not_configured() -> None:
    settings = Settings(cosmos_endpoint=None)
    with pytest.raises(AgentStudioStoreError, match="unavailable"):
        cosmos_store.build_agent_studio_store(settings)


def test_build_agent_studio_store_uses_managed_identity_client_id_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeCosmosClient:
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential

        def get_database_client(self, _name: str) -> FakeDatabase:
            return FakeDatabase()

    monkeypatch.setattr(cosmos_store, "CosmosClient", _FakeCosmosClient)
    monkeypatch.setattr(
        cosmos_store, "ManagedIdentityCredential", lambda client_id: f"managed:{client_id}"
    )
    settings = Settings(
        cosmos_endpoint="https://cosmos.example.test",
        managed_identity_client_id="client-123",
    )
    store = cosmos_store.build_agent_studio_store(settings)
    assert isinstance(store, cosmos_store.CosmosAgentStudioStore)
    assert captured["credential"] == "managed:client-123"


def test_build_agent_studio_store_uses_default_credential_when_no_client_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _FakeCosmosClient:
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["credential"] = credential

        def get_database_client(self, _name: str) -> FakeDatabase:
            return FakeDatabase()

    monkeypatch.setattr(cosmos_store, "CosmosClient", _FakeCosmosClient)
    monkeypatch.setattr(cosmos_store, "DefaultAzureCredential", lambda: "default-credential")
    settings = Settings(cosmos_endpoint="https://cosmos.example.test")
    cosmos_store.build_agent_studio_store(settings)
    assert captured["credential"] == "default-credential"
