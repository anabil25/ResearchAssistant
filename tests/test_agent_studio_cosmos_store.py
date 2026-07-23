from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
    AgentRelease,
    AgentRole,
    AgentVersion,
    ApprovalKind,
    ApprovalState,
    BuilderProposal,
    BuilderProposalState,
    BuilderProvenance,
    DeploymentEnvironment,
    DeploymentRecord,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    ReleaseGateReport,
    ReleaseStatus,
    RuntimeTarget,
    StudioApprovalRecord,
    ToolRegistration,
    ToolRegistrationKind,
)
from research_assistant_api.agent_studio.store import AgentStudioStoreError
from research_assistant_api.config import Settings

TENANT = "demo"
OTHER_TENANT = "other-tenant"
AGENT_ID = "agent-cosmos-test"
OTHER_AGENT_ID = "agent-cosmos-other"
USER_ID = "user-1"


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
        self.query_calls = 0

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
                status_code=self.fail_replace_status,
                message="simulated failure",
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
        del query, enable_cross_partition_query
        self.query_calls += 1
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
    return cosmos_store.CosmosAgentStudioStore(
        "https://cosmos.example.test",
        "agent-studio",
        FakeCredential(),
    )


def _manifest(
    *,
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    project_id: str = "default",
    display_name: str = "Cosmos Test Agent",
) -> AgentManifest:
    return AgentManifest(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        display_name=display_name,
        owner_kind=AgentOwnerKind.USER,
        owner_id=USER_ID,
    )


def _draft(
    *,
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    project_id: str = "default",
    etag: str = "etag-1",
) -> AgentDraft:
    return AgentDraft(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        manifest=_manifest(tenant_id=tenant_id, logical_agent_id=logical_agent_id, project_id=project_id),
        updated_by=USER_ID,
        etag=etag,
    )


def _version(
    *,
    sequence: int = 1,
    version_id: str | None = None,
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
) -> AgentVersion:
    return AgentVersion(
        id=version_id or f"version-{sequence}",
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        sequence=sequence,
        manifest=_manifest(tenant_id=tenant_id, logical_agent_id=logical_agent_id),
        manifest_hash=f"hash-{sequence}",
        created_by=USER_ID,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
    )


def _release(
    *,
    release_id: str = "release-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    status: ReleaseStatus = ReleaseStatus.GATED,
    previous_release_id: str | None = None,
) -> AgentRelease:
    return AgentRelease(
        id=release_id,
        version_id=version_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        status=status,
        previous_release_id=previous_release_id,
        created_by=USER_ID,
    )


def _approval(
    *,
    approval_id: str = "approval-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    idempotency_key: str = "key-1",
    state: ApprovalState = ApprovalState.PENDING,
) -> StudioApprovalRecord:
    return StudioApprovalRecord(
        id=approval_id,
        version_id=version_id,
        tenant_id=tenant_id,
        kind=ApprovalKind.RELEASE_PROMOTION,
        state=state,
        gated_action="promote_version",
        destination="prod",
        requested_by=USER_ID,
        evidence_summary="Evidence.",
        risk="medium",
        idempotency_key=idempotency_key,
    )


def _deployment(
    *,
    deployment_id: str = "deployment-1",
    logical_agent_id: str = AGENT_ID,
    tenant_id: str = TENANT,
    version_id: str = "version-1",
    trace_ref: str | None = None,
) -> DeploymentRecord:
    return DeploymentRecord(
        id=deployment_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        version_id=version_id,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by=USER_ID,
        trace_ref=trace_ref,
    )


def _binding(
    *,
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    version_id: str = "version-1",
) -> LogicalAgentBinding:
    return LogicalAgentBinding(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        environment=DeploymentEnvironment.DEVELOPMENT,
        resolved_version_id=version_id,
        updated_by=USER_ID,
    )


def _tool_registration(
    *,
    registration_id: str = "reg-1",
    logical_agent_id: str = AGENT_ID,
    tenant_id: str = TENANT,
) -> ToolRegistration:
    return ToolRegistration(
        id=registration_id,
        tenant_id=tenant_id,
        logical_agent_id=logical_agent_id,
        descriptor_id="foundry.web_search",
        operation="search",
        kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
        handler_ref="builtin://web_search",
        registered_by=USER_ID,
    )


def test_persistence_label_and_drafts_reload_across_instances(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    draft = _draft()

    assert first.persistence == "Azure Cosmos DB"
    assert first.save_draft(draft) == draft

    second = _new_store(fake_client)
    assert second.get_draft(TENANT, AGENT_ID) == draft
    assert second.list_drafts(TENANT) == (draft,)
    assert second.get_draft(OTHER_TENANT, AGENT_ID) is None


def test_ownership_role_for_project_scoping_and_dedupe(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    grants = (
        OwnershipGrant(
            tenant_id=TENANT,
            logical_agent_id=AGENT_ID,
            principal_id=USER_ID,
            role=AgentRole.OWNER,
            granted_by="admin",
            project_id="p1",
        ),
        OwnershipGrant(
            tenant_id=TENANT,
            logical_agent_id=AGENT_ID,
            principal_id=USER_ID,
            role=AgentRole.VIEWER,
            granted_by="admin",
        ),
        OwnershipGrant(
            tenant_id=TENANT,
            logical_agent_id=AGENT_ID,
            principal_id="scoped-user",
            role=AgentRole.CONTRIBUTOR,
            granted_by="admin",
            project_id="p1",
        ),
    )
    for grant in grants:
        first.grant_ownership(grant)

    second = _new_store(fake_client)
    assert second.role_for(TENANT, AGENT_ID, USER_ID) is AgentRole.OWNER
    assert second.role_for(TENANT, AGENT_ID, USER_ID, project_id="p1") is AgentRole.OWNER
    assert second.role_for(TENANT, AGENT_ID, USER_ID, project_id="p2") is AgentRole.VIEWER
    assert second.role_for(TENANT, AGENT_ID, "scoped-user", project_id="p1") is AgentRole.CONTRIBUTOR
    assert second.role_for(TENANT, AGENT_ID, "scoped-user", project_id="p2") is None
    assert len(second.list_ownership(TENANT, AGENT_ID)) == 3
    assert len(second.list_ownership(TENANT, AGENT_ID)) == 3
    assert second.role_for(OTHER_TENANT, AGENT_ID, USER_ID) is None


def test_allocate_version_is_single_process_atomic_and_persists(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)

    def allocate(_index: int) -> AgentVersion:
        return store.allocate_version(TENANT, AGENT_ID, lambda sequence: _version(sequence=sequence))

    with ThreadPoolExecutor(max_workers=6) as executor:
        versions = list(executor.map(allocate, range(6)))

    assert sorted(version.sequence for version in versions) == [1, 2, 3, 4, 5, 6]

    reloaded = _new_store(fake_client)
    assert [version.sequence for version in reloaded.list_versions(TENANT, AGENT_ID)] == [1, 2, 3, 4, 5, 6]
    assert reloaded.next_sequence(TENANT, AGENT_ID) == 7


def test_versions_reload_get_and_list_without_duplicates(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    first.create_version(_version(sequence=1, version_id="version-1"))
    first.create_version(_version(sequence=2, version_id="version-2"))

    second = _new_store(fake_client)
    assert [version.id for version in second.list_versions(TENANT, AGENT_ID)] == ["version-1", "version-2"]
    assert len(second.list_versions(TENANT, AGENT_ID)) == 2

    third = _new_store(fake_client)
    version_queries = fake_client.database.containers["versions"]
    before = version_queries.query_calls
    assert third.get_version(TENANT, "version-1") is not None
    assert version_queries.query_calls == before + 1
    assert third.get_version(TENANT, "version-1") is not None
    assert version_queries.query_calls == before + 1
    assert third.get_version(TENANT, "missing") is None
    assert third.get_version(OTHER_TENANT, "version-1") is None


def test_lineage_and_gate_reports_reload_and_cache(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    edge = LineageEdge(
        tenant_id=TENANT,
        child_logical_agent_id=AGENT_ID,
        child_version_id="version-2",
        parent_logical_agent_id=OTHER_AGENT_ID,
        parent_version_id="version-1",
    )
    report = ReleaseGateReport(id="report-1", version_id="version-1", results=())

    first.add_lineage_edge(edge)
    first.save_gate_report(report)

    second = _new_store(fake_client)
    assert second.list_lineage(TENANT, AGENT_ID) == (edge,)
    assert second.list_lineage(TENANT, AGENT_ID) == (edge,)
    assert second.list_lineage(OTHER_TENANT, AGENT_ID) == ()

    third = _new_store(fake_client)
    versions_container = fake_client.database.containers["versions"]
    before = versions_container.query_calls
    assert third.get_gate_report("report-1") == report
    assert versions_container.query_calls == before + 1
    assert third.get_gate_report("report-1") == report
    assert versions_container.query_calls == before + 1
    assert third.get_gate_report("missing") is None


def test_releases_reload_get_list_latest_and_tenant_isolation(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    gated = _release(release_id="release-1")
    active = _release(
        release_id="release-2",
        status=ReleaseStatus.ACTIVE,
        previous_release_id=gated.id,
    )
    other_version = _release(release_id="release-3", version_id="version-2")
    for release in (gated, active, other_version):
        first.create_release(release)

    second = _new_store(fake_client)
    assert second.list_releases_for_version(TENANT, "version-1") == (gated, active)
    assert second.list_releases_for_version(TENANT, "version-1") == (gated, active)
    assert second.latest_release_for_version(TENANT, "version-1") == active
    assert second.latest_release_for_version(TENANT, "version-missing") is None
    assert second.list_releases_for_version(OTHER_TENANT, "version-1") == ()

    third = _new_store(fake_client)
    versions_container = fake_client.database.containers["versions"]
    before = versions_container.query_calls
    assert third.get_release(TENANT, gated.id) == gated
    assert versions_container.query_calls == before + 1
    assert third.get_release(TENANT, gated.id) == gated
    assert versions_container.query_calls == before + 1
    assert third.get_release(TENANT, "missing") is None


def test_get_release_does_not_leak_across_tenants(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    release = _release()
    store.create_release(release)

    reloaded = _new_store(fake_client)
    assert reloaded.get_release(OTHER_TENANT, release.id) is None


def test_approvals_persist_and_decisions_validate_state(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    pending = _approval()
    assert first.create_approval(pending) == pending
    assert first.create_approval(_approval(approval_id="approval-duplicate")) == pending

    second = _new_store(fake_client)
    assert second.get_approval(TENANT, pending.id) == pending
    assert second.get_approval(OTHER_TENANT, pending.id) is None
    assert second.list_approvals(TENANT, version_id=pending.version_id) == (pending,)
    decided = pending.model_copy(update={"state": ApprovalState.APPROVED})
    assert second.save_approval_decision(decided) == decided

    third = _new_store(fake_client)
    assert third.get_approval(TENANT, pending.id) == decided
    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        third.save_approval_decision(decided)

    missing = _new_store(fake_client)
    with pytest.raises(AgentStudioStoreError, match="not found"):
        missing.save_approval_decision(_approval(approval_id="missing-approval"))


def test_save_approval_decision_wraps_cosmos_conflicts(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_approval(_approval())
    decided = _approval().model_copy(update={"state": ApprovalState.APPROVED})

    fake_client.database.containers["governance"].fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="decided concurrently"):
        store.save_approval_decision(decided)

    fake_client.database.containers["governance"].fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        store.save_approval_decision(decided)


def _proposal(
    *,
    proposal_id: str = "proposal-1",
    tenant_id: str = TENANT,
    logical_agent_id: str = AGENT_ID,
    state: BuilderProposalState = BuilderProposalState.PENDING,
) -> BuilderProposal:
    manifest = _manifest(tenant_id=tenant_id, logical_agent_id=logical_agent_id)
    return BuilderProposal(
        id=proposal_id,
        tenant_id=tenant_id,
        logical_agent_id=logical_agent_id,
        draft_base_etag="etag-1",
        before_manifest=manifest,
        after_manifest=manifest,
        before_manifest_hash="hash-before",
        after_manifest_hash="hash-after",
        provenance=BuilderProvenance(
            generator="test-generator",
            message="Add a search tool.",
            requested_by=USER_ID,
        ),
        state=state,
    )


def test_builder_proposals_persist_and_decisions_validate_state(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    pending = _proposal()
    assert first.create_builder_proposal(pending) == pending

    second = _new_store(fake_client)
    assert second.get_builder_proposal(TENANT, pending.id) == pending
    assert second.get_builder_proposal(OTHER_TENANT, pending.id) is None
    assert second.list_builder_proposals(TENANT, AGENT_ID) == (pending,)
    decided = pending.model_copy(update={"state": BuilderProposalState.APPLIED})
    assert second.save_builder_proposal_decision(decided) == decided

    third = _new_store(fake_client)
    assert third.get_builder_proposal(TENANT, pending.id) == decided
    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        third.save_builder_proposal_decision(decided)

    missing = _new_store(fake_client)
    with pytest.raises(AgentStudioStoreError, match="not found"):
        missing.save_builder_proposal_decision(_proposal(proposal_id="missing-proposal"))


def test_save_builder_proposal_decision_wraps_cosmos_conflicts(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_builder_proposal(_proposal())
    decided = _proposal().model_copy(update={"state": BuilderProposalState.APPLIED})

    fake_client.database.containers["governance"].fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="decided concurrently"):
        store.save_builder_proposal_decision(decided)

    fake_client.database.containers["governance"].fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        store.save_builder_proposal_decision(decided)


def test_builder_proposals_list_avoids_duplicate_cache_entries(fake_client: FakeCosmosClient) -> None:
    store = _new_store(fake_client)
    store.create_builder_proposal(_proposal())
    assert len(store.list_builder_proposals(TENANT, AGENT_ID)) == 1
    # Re-listing must not duplicate the cached id (covers the "already cached" guard).
    assert len(store.list_builder_proposals(TENANT, AGENT_ID)) == 1


def test_deployments_reload_update_and_conflict_handling(fake_client: FakeCosmosClient) -> None:
    first = _new_store(fake_client)
    deployment = _deployment()
    first.create_deployment(deployment)

    second = _new_store(fake_client)
    assert second.list_deployments(TENANT, AGENT_ID) == (deployment,)
    assert second.list_deployments(TENANT, AGENT_ID) == (deployment,)

    third = _new_store(fake_client)
    governance = fake_client.database.containers["governance"]
    before = governance.query_calls
    assert third.get_deployment(TENANT, deployment.id) == deployment
    assert governance.query_calls == before + 1
    assert third.get_deployment(TENANT, deployment.id) == deployment
    assert governance.query_calls == before + 1
    assert third.get_deployment(TENANT, "missing") is None
    assert third.get_deployment(OTHER_TENANT, deployment.id) is None

    updated = deployment.model_copy(update={"trace_ref": "trace-1"})
    assert third.update_deployment(updated) == updated

    fresh = _new_store(fake_client)
    assert fresh.get_deployment(TENANT, deployment.id) == updated
    with pytest.raises(AgentStudioStoreError, match="not found"):
        fresh.update_deployment(_deployment(deployment_id="missing-deployment"))

    fake_client.database.containers["governance"].fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="changed concurrently"):
        fresh.update_deployment(updated)

    fake_client.database.containers["governance"].fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        fresh.update_deployment(updated)


def test_bindings_and_tool_registrations_reload_without_cross_tenant_leakage(
    fake_client: FakeCosmosClient,
) -> None:
    first = _new_store(fake_client)
    binding = _binding()
    registration = _tool_registration()
    other_registration = _tool_registration(registration_id="reg-2", logical_agent_id=OTHER_AGENT_ID)
    first.set_binding(binding)
    first.create_tool_registration(registration)
    first.create_tool_registration(other_registration)

    second = _new_store(fake_client)
    governance = fake_client.database.containers["governance"]
    before = governance.query_calls
    assert second.get_binding(TENANT, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) == binding
    assert governance.query_calls == before + 1
    assert second.get_binding(TENANT, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) == binding
    assert governance.query_calls == before + 1
    assert second.get_binding(OTHER_TENANT, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None

    assert second.list_tool_registrations(TENANT, AGENT_ID) == (registration,)
    assert second.list_tool_registrations(TENANT, OTHER_AGENT_ID) == (other_registration,)
    assert second.list_tool_registrations(TENANT, AGENT_ID) == (registration,)
    assert second.list_tool_registrations(OTHER_TENANT, AGENT_ID) == ()


def test_build_agent_studio_store_factory_and_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(AgentStudioStoreError, match="unavailable"):
        cosmos_store.build_agent_studio_store(Settings(cosmos_endpoint=None))

    managed: dict[str, Any] = {}

    class ManagedClient:
        def __init__(self, endpoint: str, credential: Any) -> None:
            managed["endpoint"] = endpoint
            managed["credential"] = credential

        def get_database_client(self, _name: str) -> FakeDatabase:
            return FakeDatabase()

    monkeypatch.setattr(cosmos_store, "CosmosClient", ManagedClient)
    monkeypatch.setattr(
        cosmos_store,
        "ManagedIdentityCredential",
        lambda *, client_id: f"managed:{client_id}",
    )
    managed_store = cosmos_store.build_agent_studio_store(
        Settings(
            cosmos_endpoint="https://cosmos.example.test",
            managed_identity_client_id="client-123",
        )
    )
    assert isinstance(managed_store, cosmos_store.CosmosAgentStudioStore)
    assert managed == {
        "endpoint": "https://cosmos.example.test",
        "credential": "managed:client-123",
    }

    default: dict[str, Any] = {}

    class DefaultClient:
        def __init__(self, endpoint: str, credential: Any) -> None:
            default["endpoint"] = endpoint
            default["credential"] = credential

        def get_database_client(self, _name: str) -> FakeDatabase:
            return FakeDatabase()

    monkeypatch.setattr(cosmos_store, "CosmosClient", DefaultClient)
    monkeypatch.setattr(cosmos_store, "DefaultAzureCredential", lambda: "default-credential")
    default_store = cosmos_store.build_agent_studio_store(Settings(cosmos_endpoint="https://cosmos.example.test"))
    assert isinstance(default_store, cosmos_store.CosmosAgentStudioStore)
    assert default == {
        "endpoint": "https://cosmos.example.test",
        "credential": "default-credential",
    }
