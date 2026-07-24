"""Tests for the Cosmos-backed Agent Studio metadata store."""

# mypy: disable-error-code=import-untyped

from __future__ import annotations

import importlib
import threading
from copy import deepcopy
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
import research_assistant_api.agent_studio.cosmos_store as cosmos_store
from azure.core import MatchConditions
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError, CosmosResourceNotFoundError
from research_assistant_api.agent_studio.models import (
    AgentDraft,
    AgentManifest,
    AgentOwnerKind,
    AgentRelease,
    AgentRole,
    AgentVersion,
    ApprovalConsumptionRecord,
    ApprovalKind,
    ApprovalRevocation,
    ApprovalState,
    BuilderProposal,
    BuilderProposalState,
    BuilderProvenance,
    DeploymentEnvironment,
    DeploymentHealth,
    DeploymentRecord,
    EvaluationRun,
    EvaluationRunStatus,
    EvaluationSuite,
    EvaluationTestCase,
    EvaluationTestResult,
    GateName,
    GateResult,
    GateStatus,
    HealthStatus,
    IdempotencyClaimDisposition,
    IdempotencyKey,
    IdempotencyState,
    LineageEdge,
    LogicalAgentBinding,
    OwnershipGrant,
    PlaygroundRunStatus,
    PlaygroundTestRun,
    ReleaseGateReport,
    ReleaseStatus,
    RuntimeTarget,
    StudioApprovalRecord,
    ToolRegistrationKind,
    ToolRegistrationSpec,
    utc_now,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import (
    AgentStudioStore,
    AgentStudioStoreError,
    DraftConflictError,
    IdempotencyConcurrencyError,
    IdempotencyNotFoundError,
    ReleaseSuccessorConflictError,
)
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
PROJECT = "project-1"
OTHER_PROJECT = "project-2"
AGENT_ID = "agent-cosmos-test"
OTHER_AGENT_ID = "agent-cosmos-other"
USER_ID = "user-1"

SCOPE = ScopeContext(tenant_id=TENANT, project_id=PROJECT)
SAME_TENANT_OTHER_PROJECT_SCOPE = ScopeContext(tenant_id=TENANT, project_id=OTHER_PROJECT)
OTHER_TENANT_SAME_PROJECT_SCOPE = ScopeContext(tenant_id=OTHER_TENANT, project_id=PROJECT)


def _token_credential() -> TokenCredential:
    return cast("TokenCredential", object())


def _manifest(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
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
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    etag: str = "etag-1",
) -> AgentDraft:
    return AgentDraft(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        manifest=_manifest(tenant_id=tenant_id, project_id=project_id, logical_agent_id=logical_agent_id),
        updated_by=USER_ID,
        etag=etag,
    )


def _version(
    *,
    sequence: int = 1,
    version_id: str | None = None,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
) -> AgentVersion:
    return AgentVersion(
        id=version_id or f"version-{sequence}",
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        sequence=sequence,
        manifest=_manifest(tenant_id=tenant_id, project_id=project_id, logical_agent_id=logical_agent_id),
        manifest_hash=f"hash-{sequence}",
        created_by=USER_ID,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
    )


def _lineage(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    child_logical_agent_id: str = AGENT_ID,
    child_version_id: str = "version-2",
    parent_logical_agent_id: str = OTHER_AGENT_ID,
    parent_version_id: str = "version-1",
) -> LineageEdge:
    return LineageEdge(
        tenant_id=tenant_id,
        project_id=project_id,
        child_logical_agent_id=child_logical_agent_id,
        child_version_id=child_version_id,
        parent_logical_agent_id=parent_logical_agent_id,
        parent_version_id=parent_version_id,
    )


def _release(
    *,
    release_id: str = "release-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    status: ReleaseStatus = ReleaseStatus.GATED,
    previous_release_id: str | None = None,
) -> AgentRelease:
    return AgentRelease(
        id=release_id,
        version_id=version_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        manifest_hash="hash-" + version_id,
        status=status,
        previous_release_id=previous_release_id,
        created_by=USER_ID,
    )


def _gate_report(
    report_id: str = "report-1",
    version_id: str = "version-1",
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
) -> ReleaseGateReport:
    return ReleaseGateReport(
        id=report_id,
        version_id=version_id,
        tenant_id=tenant_id,
        project_id=project_id,
        results=(GateResult(name=GateName.TEST, status=GateStatus.PASSED, detail="ok"),),
    )


def _approval(
    *,
    approval_id: str = "approval-1",
    version_id: str = "version-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    idempotency_key: str = "key-1",
    state: ApprovalState = ApprovalState.PENDING,
) -> StudioApprovalRecord:
    return StudioApprovalRecord(
        id=approval_id,
        version_id=version_id,
        tenant_id=tenant_id,
        project_id=project_id,
        kind=ApprovalKind.RELEASE_PROMOTION,
        state=state,
        gated_action="promote_version",
        destination="prod",
        requested_by=USER_ID,
        evidence_summary="Evidence.",
        risk="medium",
        idempotency_key=idempotency_key,
    )


def _revocation(
    *,
    revocation_id: str = "revocation-1",
    approval_id: str = "approval-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    actor_id: str = USER_ID,
    reason: str = "no longer needed",
    idempotency_key: str = "rev-key-1",
) -> ApprovalRevocation:
    return ApprovalRevocation(
        id=revocation_id,
        approval_id=approval_id,
        tenant_id=tenant_id,
        project_id=project_id,
        actor_id=actor_id,
        reason=reason,
        idempotency_key=idempotency_key,
    )


def _consumption(
    *,
    consumption_id: str = "consumption-1",
    approval_id: str = "approval-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    principal_id: str = USER_ID,
    binding_id: str = "binding-1",
    operation_id: str = "search",
    invocation_id: str = "invocation-1",
    idempotency_key: str = "consume-key-1",
) -> ApprovalConsumptionRecord:
    return ApprovalConsumptionRecord(
        id=consumption_id,
        approval_id=approval_id,
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=principal_id,
        binding_id=binding_id,
        operation_id=operation_id,
        args_hash="args-hash-1",
        destination_hash="destination-hash-1",
        invocation_id=invocation_id,
        idempotency_key=idempotency_key,
        approval_version="version-1",
    )


def _deployment(
    *,
    deployment_id: str = "deployment-1",
    logical_agent_id: str = AGENT_ID,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    version_id: str = "version-1",
    trace_ref: str | None = None,
) -> DeploymentRecord:
    return DeploymentRecord(
        id=deployment_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        version_id=version_id,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by=USER_ID,
        trace_ref=trace_ref,
    )


def _idempotency_key(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    binding_digest: str = "a" * 64,
    operation_id: str = "search",
    destination: str = "descriptor-1.search",
    caller_key: str = "caller-1",
    argument_hash: str = "b" * 64,
) -> IdempotencyKey:
    return IdempotencyKey(
        tenant_id=tenant_id,
        project_id=project_id,
        binding_digest=binding_digest,
        operation_id=operation_id,
        destination=destination,
        caller_key=caller_key,
        argument_hash=argument_hash,
    )


def _binding(
    *,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    version_id: str = "version-1",
) -> LogicalAgentBinding:
    return LogicalAgentBinding(
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        environment=DeploymentEnvironment.DEVELOPMENT,
        resolved_version_id=version_id,
        updated_by=USER_ID,
    )


def _tool_registration(
    *,
    registration_id: str = "reg-1",
    logical_agent_id: str = AGENT_ID,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
) -> ToolRegistrationSpec:
    return ToolRegistrationSpec(
        id=registration_id,
        tenant_id=tenant_id,
        project_id=project_id,
        logical_agent_id=logical_agent_id,
        descriptor_id="foundry.web_search",
        operation="search",
        kind=ToolRegistrationKind.MANAGED_FOUNDRY_NATIVE,
        handler_ref="builtin://web_search",
        registered_by=USER_ID,
    )


def _proposal(
    *,
    proposal_id: str = "proposal-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
    state: BuilderProposalState = BuilderProposalState.PENDING,
) -> BuilderProposal:
    manifest = _manifest(tenant_id=tenant_id, project_id=project_id, logical_agent_id=logical_agent_id)
    return BuilderProposal(
        id=proposal_id,
        tenant_id=tenant_id,
        project_id=project_id,
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


def _evaluation_suite(
    *,
    suite_id: str = "eval-suite-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
) -> EvaluationSuite:
    return EvaluationSuite(
        id=suite_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        name="Regression suite",
        test_cases=(EvaluationTestCase(id="case-1", name="Case 1", input="What is 2+2?"),),
        created_by=USER_ID,
    )


def _evaluation_run(
    *,
    run_id: str = "eval-run-1",
    suite_id: str = "eval-suite-1",
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
) -> EvaluationRun:
    return EvaluationRun(
        id=run_id,
        suite_id=suite_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        status=EvaluationRunStatus.COMPLETED,
        results=(EvaluationTestResult(test_case_id="case-1", score=1.0, passed=True),),
        requested_by=USER_ID,
    )


def _test_run(
    *,
    run_id: str = "test-run-1",
    version_id: str | None = None,
    tenant_id: str = TENANT,
    project_id: str = PROJECT,
    logical_agent_id: str = AGENT_ID,
) -> PlaygroundTestRun:
    return PlaygroundTestRun(
        id=run_id,
        logical_agent_id=logical_agent_id,
        tenant_id=tenant_id,
        project_id=project_id,
        version_id=version_id,
        input="What is 2+2?",
        output="4",
        status=PlaygroundRunStatus.COMPLETED,
        requested_by=USER_ID,
    )


class FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self.version = 0
        self.fail_replace_status: int | None = None
        self.fail_batch_status: int | None = None
        self.query_calls = 0
        self.query_log: list[dict[str, Any]] = []
        self.read_log: list[tuple[str, str]] = []
        # Guards ``create_item``/``replace_item`` critical sections so
        # concurrent threads racing against the same fake container observe
        # genuine optimistic-concurrency semantics (one wins, the other gets
        # a 409/412) instead of silently corrupting ``self.documents`` via
        # unsynchronized dict mutation -- this is what makes the parallel
        # sequence-allocation tests meaningful.
        self._lock = threading.Lock()

    def upsert_item(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.version += 1
            stored = deepcopy(item)
            stored["_etag"] = str(self.version)
            key = (stored["scope_key"], stored["id"])
            self.documents[key] = stored
            return deepcopy(stored)

    def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            key = (body["scope_key"], body["id"])
            if key in self.documents:
                raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                    status_code=409,
                    message="conflict: document already exists",
                )
            self.version += 1
            stored = deepcopy(body)
            stored["_etag"] = str(self.version)
            self.documents[key] = stored
            return deepcopy(stored)

    def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        self.read_log.append((partition_key, item))
        key = (partition_key, item)
        with self._lock:
            if key not in self.documents:
                raise CosmosResourceNotFoundError(  # type: ignore[no-untyped-call]
                    status_code=404,
                    message="missing",
                )
            return deepcopy(self.documents[key])

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, str]],
        partition_key: str,
    ) -> list[dict[str, Any]]:
        self.query_calls += 1
        values = {item["name"]: item["value"] for item in parameters}
        self.query_log.append(
            {
                "query": query,
                "parameters": deepcopy(parameters),
                "partition_key": partition_key,
            }
        )
        document_type = values["@documentType"]
        version_id = values.get("@versionId")
        with self._lock:
            return [
                deepcopy(document)
                for (scope_key, _), document in self.documents.items()
                if scope_key == partition_key
                and document["documentType"] == document_type
                and (version_id is None or document["payload"].get("version_id") == version_id)
            ]

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
        assert match_condition is MatchConditions.IfNotModified
        key = (body["scope_key"], item)
        with self._lock:
            if self.documents[key]["_etag"] != etag:
                raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                    status_code=412,
                    message="etag mismatch: document was modified concurrently",
                )
            self.version += 1
            stored = deepcopy(body)
            stored["_etag"] = str(self.version)
            self.documents[key] = stored
            return deepcopy(stored)

    def execute_item_batch(
        self,
        batch_operations: list[tuple[str, tuple[Any, ...]] | tuple[str, tuple[Any, ...], dict[str, Any]]],
        partition_key: str,
    ) -> list[dict[str, Any]]:
        """Simulate Cosmos's atomic transactional batch: every operation is
        validated against the *current* state before any mutation is
        applied, so a batch either applies in full or leaves every document
        exactly as it was -- there is no path that partially applies some
        operations and not others, matching real Cosmos batch semantics."""
        if self.fail_batch_status is not None:
            raise CosmosBatchOperationError(  # type: ignore[no-untyped-call]
                error_index=0,
                headers={},
                status_code=self.fail_batch_status,
                message="simulated batch failure",
                operation_responses=[],
            )
        with self._lock:
            for index, operation in enumerate(batch_operations):
                op_type = operation[0]
                args = operation[1]
                kwargs = operation[2] if len(operation) > 2 else {}
                if op_type == "replace":
                    item_id, body = args
                    key = (partition_key, item_id)
                    if key not in self.documents:
                        raise CosmosBatchOperationError(  # type: ignore[no-untyped-call]
                            error_index=index,
                            headers={},
                            status_code=404,
                            message="missing document for replace",
                            operation_responses=[],
                        )
                    expected_etag = kwargs.get("if_match_etag")
                    if expected_etag is not None and self.documents[key]["_etag"] != expected_etag:
                        raise CosmosBatchOperationError(  # type: ignore[no-untyped-call]
                            error_index=index,
                            headers={},
                            status_code=412,
                            message="etag mismatch: document was modified concurrently",
                            operation_responses=[],
                        )
                elif op_type == "create":
                    (body,) = args
                    key = (partition_key, body["id"])
                    if key in self.documents:
                        raise CosmosBatchOperationError(  # type: ignore[no-untyped-call]
                            error_index=index,
                            headers={},
                            status_code=409,
                            message="conflict: document already exists",
                            operation_responses=[],
                        )
                elif op_type == "upsert":
                    pass
                else:
                    raise NotImplementedError(f"FakeContainer.execute_item_batch does not simulate '{op_type}'.")
            # All operations validated against pre-batch state -- now apply
            # every one of them, atomically from the caller's perspective.
            results: list[dict[str, Any]] = []
            for operation in batch_operations:
                op_type = operation[0]
                args = operation[1]
                if op_type == "replace":
                    item_id, body = args
                    key = (partition_key, item_id)
                elif op_type in ("create", "upsert"):
                    (body,) = args
                    key = (partition_key, body["id"])
                else:  # pragma: no cover - unreachable, validated above
                    continue
                self.version += 1
                stored = deepcopy(body)
                stored["_etag"] = str(self.version)
                self.documents[key] = stored
                results.append(deepcopy(stored))
            return results

    def delete_item(self, *, item: str, partition_key: str) -> None:
        key = (partition_key, item)
        with self._lock:
            if key not in self.documents:
                raise CosmosResourceNotFoundError(  # type: ignore[no-untyped-call]
                    status_code=404,
                    message="missing",
                )
            del self.documents[key]

    def get_document(self, scope_key: str, document_id: str) -> dict[str, Any]:
        return deepcopy(self.documents[(scope_key, document_id)])

    def inject_document(
        self,
        *,
        scope_key: str,
        document_id: str,
        document_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.upsert_item(
            {
                "id": document_id,
                "documentType": document_type,
                "scope_key": scope_key,
                "payload": deepcopy(payload),
            }
        )


class FakeDatabase:
    def __init__(self) -> None:
        self.containers: dict[str, FakeContainer] = {}
        self.requested_container_names: list[str] = []

    def get_container_client(self, name: str) -> FakeContainer:
        self.requested_container_names.append(name)
        return self.containers.setdefault(name, FakeContainer())


class FakeCosmosClient:
    def __init__(self, endpoint: str, credential: Any, database: FakeDatabase) -> None:
        self.endpoint = endpoint
        self.credential = credential
        self._database = database
        self.requested_database_names: list[str] = []

    def get_database_client(self, name: str) -> FakeDatabase:
        self.requested_database_names.append(name)
        return self._database


class FakeCosmosClientFactory:
    def __init__(self) -> None:
        self.database = FakeDatabase()
        self.clients: list[FakeCosmosClient] = []

    def __call__(self, endpoint: str, credential: Any) -> FakeCosmosClient:
        client = FakeCosmosClient(endpoint, credential, self.database)
        self.clients.append(client)
        return client


@pytest.fixture
def fake_client_factory(monkeypatch: pytest.MonkeyPatch) -> FakeCosmosClientFactory:
    factory = FakeCosmosClientFactory()
    monkeypatch.setattr(cosmos_store, "CosmosClient", factory)
    return factory


def _new_store(
    _factory: FakeCosmosClientFactory,
    *,
    metadata_container_name: str = "agentStudioMetadataV1",
) -> cosmos_store.CosmosAgentStudioStore:
    return cosmos_store.CosmosAgentStudioStore(
        "https://cosmos.example.test",
        "agent-studio",
        _token_credential(),
        metadata_container_name,
    )


def _metadata_container(
    factory: FakeCosmosClientFactory,
    *,
    name: str = "agentStudioMetadataV1",
) -> FakeContainer:
    return factory.database.containers[name]


def test_constructor_uses_requested_database_and_metadata_container(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    credential = _token_credential()
    store = cosmos_store.CosmosAgentStudioStore(
        "https://cosmos.example.test",
        "agent-studio",
        credential,
        "custom-metadata",
    )

    assert isinstance(store, cosmos_store.CosmosAgentStudioStore)
    assert len(fake_client_factory.clients) == 1
    client = fake_client_factory.clients[0]
    assert client.endpoint == "https://cosmos.example.test"
    assert client.credential is credential
    assert client.requested_database_names == ["agent-studio"]
    assert fake_client_factory.database.requested_container_names == ["custom-metadata"]


def test_drafts_round_trip_document_shape_and_scope_isolation(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    draft = _draft()

    assert store.persistence == "Azure Cosmos DB"
    assert store.save_draft(SCOPE, draft) == draft

    container = _metadata_container(fake_client_factory)
    document = container.get_document(SCOPE.scope_key, "draft::agent-cosmos-test")
    assert document == {
        "id": "draft::agent-cosmos-test",
        "documentType": "draft",
        "scope_key": SCOPE.scope_key,
        "payload": draft.model_dump(mode="json"),
        "_etag": "1",
    }

    reloaded = _new_store(fake_client_factory)
    assert reloaded.get_draft(SCOPE, AGENT_ID) == draft
    assert reloaded.list_drafts(SCOPE) == (draft,)
    assert reloaded.get_draft(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) is None
    assert reloaded.list_drafts(SAME_TENANT_OTHER_PROJECT_SCOPE) == ()
    assert reloaded.get_draft(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) is None
    assert reloaded.list_drafts(OTHER_TENANT_SAME_PROJECT_SCOPE) == ()
    assert any(
        entry["partition_key"] == SCOPE.scope_key
        and entry["parameters"] == [{"name": "@documentType", "value": "draft"}]
        for entry in container.query_log
    )


def test_save_draft_enforces_expected_etag_app_level_and_cosmos_native_concurrency(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Review finding #6: Cosmos ``save_draft`` must enforce optimistic
    concurrency both at the app level (comparing the stored ``AgentDraft.etag``)
    and at the infra level (Cosmos ``MatchConditions.IfNotModified`` + native
    ``_etag``), converting either failure mode into ``DraftConflictError``."""
    store = _new_store(fake_client_factory)
    draft = _draft()
    assert store.save_draft(SCOPE, draft) == draft

    first_editor = _new_store(fake_client_factory)
    fetched = first_editor.get_draft(SCOPE, AGENT_ID)
    assert fetched is not None

    updated = fetched.model_copy(update={"display_name": "First Editor Update", "etag": "etag-after-first-editor"})
    assert first_editor.save_draft(SCOPE, updated, expected_etag=fetched.etag) == updated

    second_editor = _new_store(fake_client_factory)
    stale_update = fetched.model_copy(
        update={"display_name": "Second Editor Lost", "etag": "etag-after-second-editor"}
    )
    with pytest.raises(DraftConflictError, match="modified concurrently"):
        second_editor.save_draft(SCOPE, stale_update, expected_etag=fetched.etag)
    assert second_editor.get_draft(SCOPE, AGENT_ID) == updated

    with pytest.raises(DraftConflictError, match="modified concurrently"):
        second_editor.save_draft(SCOPE, stale_update, expected_etag="never-issued-etag")

    missing_draft_store = _new_store(fake_client_factory)
    with pytest.raises(DraftConflictError, match="modified concurrently"):
        missing_draft_store.save_draft(
            SAME_TENANT_OTHER_PROJECT_SCOPE,
            _draft(project_id=SAME_TENANT_OTHER_PROJECT_SCOPE.project_id),
            expected_etag="any-etag",
        )

    container = _metadata_container(fake_client_factory)
    container.fail_replace_status = 412
    race_store = _new_store(fake_client_factory)
    with pytest.raises(DraftConflictError, match="modified concurrently"):
        race_store.save_draft(
            SCOPE,
            updated.model_copy(update={"display_name": "Raced Out"}),
            expected_etag=updated.etag,
        )

    container.fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        race_store.save_draft(
            SCOPE,
            updated.model_copy(update={"display_name": "Unexpected Failure"}),
            expected_etag=updated.etag,
        )
    container.fail_replace_status = None


def test_ownership_listing_role_resolution_and_partition_scoping(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    owner = OwnershipGrant(
        tenant_id=TENANT,
        project_id=PROJECT,
        logical_agent_id=AGENT_ID,
        principal_id=USER_ID,
        role=AgentRole.OWNER,
        granted_by="admin",
    )
    unrelated = OwnershipGrant(
        tenant_id=TENANT,
        project_id=PROJECT,
        logical_agent_id=OTHER_AGENT_ID,
        principal_id=USER_ID,
        role=AgentRole.VIEWER,
        granted_by="admin",
    )

    assert store.grant_ownership(SCOPE, owner) == owner
    assert store.grant_ownership(SCOPE, unrelated) == unrelated

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_ownership(SCOPE, AGENT_ID) == (owner,)
    assert reloaded.list_ownership(SCOPE, AGENT_ID) == (owner,)
    assert reloaded.list_ownership(SCOPE, OTHER_AGENT_ID) == (unrelated,)
    assert reloaded.role_for(SCOPE, AGENT_ID, USER_ID) is AgentRole.OWNER
    assert reloaded.role_for(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID, USER_ID) is None
    assert reloaded.role_for(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID, USER_ID) is None


def test_versions_create_allocate_get_list_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    first_version = _version(sequence=1, version_id="version-1")
    other_agent_version = _version(
        sequence=1,
        version_id="other-agent-version-1",
        logical_agent_id=OTHER_AGENT_ID,
    )
    assert first.create_version(SCOPE, first_version) == first_version
    assert first.create_version(SCOPE, other_agent_version) == other_agent_version

    second = _new_store(fake_client_factory)
    allocated = second.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )
    assert allocated.sequence == 2
    assert second.list_versions(SCOPE, AGENT_ID) == (first_version, allocated)
    assert second.list_versions(SCOPE, AGENT_ID) == (first_version, allocated)

    third = _new_store(fake_client_factory)
    assert third.get_version(SCOPE, first_version.id) == first_version
    assert third.get_version(SCOPE, first_version.id) == first_version
    assert third.get_version(SCOPE, "missing-version") is None
    assert third.get_version(SAME_TENANT_OTHER_PROJECT_SCOPE, first_version.id) is None
    assert third.list_versions(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert third.get_version(OTHER_TENANT_SAME_PROJECT_SCOPE, first_version.id) is None
    assert third.list_versions(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    container = _metadata_container(fake_client_factory)
    mismatched = _version(sequence=7, version_id="version-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="version::version-mismatch",
        document_type="version",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_version(SCOPE, "version-mismatch") is None


def test_allocate_sequence_cas_retries_after_create_conflict_then_succeeds(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First sequence allocation for an agent: the counter document does not
    exist yet, so ``_allocate_sequence_cas`` takes the create-if-absent
    branch. Simulate another process winning the create race once (a 409)
    before this instance retries and succeeds."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item
    calls = {"count": 0}

    def flaky_create_item(body: dict[str, Any]) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=409,
                message="simulated concurrent counter creation",
            )
        return original_create_item(body)

    monkeypatch.setattr(container, "create_item", flaky_create_item)

    allocated = store.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    assert allocated.sequence == 1
    assert calls["count"] == 2


def test_allocate_sequence_cas_retries_after_replace_conflict_then_succeeds(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second-and-later sequence allocations replace the existing counter
    document via ETag compare-and-swap. Simulate another process winning
    that replace race once (a 412) before this instance re-reads and
    succeeds."""
    store = _new_store(fake_client_factory)
    store.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    container = _metadata_container(fake_client_factory)
    original_replace_item = container.replace_item
    calls = {"count": 0}

    def flaky_replace_item(
        *, item: str, body: dict[str, Any], etag: str | None, match_condition: Any
    ) -> dict[str, Any]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=412,
                message="simulated concurrent counter replace",
            )
        return original_replace_item(item=item, body=body, etag=etag, match_condition=match_condition)

    monkeypatch.setattr(container, "replace_item", flaky_replace_item)

    allocated = store.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    assert allocated.sequence == 2
    assert calls["count"] == 2


def test_allocate_sequence_cas_raises_after_exhausting_retry_budget(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the counter replace keeps losing the CAS race beyond the bounded
    retry budget, allocation must fail loudly (never silently reuse or
    fabricate a sequence number)."""
    store = _new_store(fake_client_factory)
    store.allocate_version(
        SCOPE,
        AGENT_ID,
        lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
    )

    container = _metadata_container(fake_client_factory)

    def always_conflicting_replace_item(
        *, item: str, body: dict[str, Any], etag: str | None, match_condition: Any
    ) -> dict[str, Any]:
        raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
            status_code=412,
            message="simulated permanent counter replace conflict",
        )

    monkeypatch.setattr(container, "replace_item", always_conflicting_replace_item)

    with pytest.raises(AgentStudioStoreError, match="Exceeded"):
        store.allocate_version(
            SCOPE,
            AGENT_ID,
            lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
        )


def test_allocate_version_rejects_builder_returning_wrong_sequence(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A builder that ignores the atomically-reserved sequence and stamps a
    different one onto the version must be rejected rather than silently
    persisted under a mismatched sequence."""
    store = _new_store(fake_client_factory)

    with pytest.raises(AgentStudioStoreError, match="expected atomically-reserved"):
        store.allocate_version(
            SCOPE,
            AGENT_ID,
            lambda sequence: _version(sequence=sequence + 41, version_id="version-wrong-sequence"),
        )


def test_allocate_sequence_cas_reraises_unexpected_create_and_replace_errors(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-conflict Cosmos errors from the counter create/replace calls must
    propagate immediately rather than being swallowed as a benign race."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)

    def failing_create_item(body: dict[str, Any]) -> dict[str, Any]:
        raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
            status_code=500,
            message="simulated unexpected create failure",
        )

    monkeypatch.setattr(container, "create_item", failing_create_item)

    with pytest.raises(CosmosHttpResponseError):
        store.allocate_version(
            SCOPE,
            AGENT_ID,
            lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
        )

    monkeypatch.undo()
    container_after_undo = _metadata_container(fake_client_factory)

    def failing_replace_item(
        *, item: str, body: dict[str, Any], etag: str | None, match_condition: Any
    ) -> dict[str, Any]:
        raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
            status_code=500,
            message="simulated unexpected replace failure",
        )

    with pytest.MonkeyPatch.context() as replace_patch:
        replace_patch.setattr(container_after_undo, "replace_item", failing_replace_item)
        store.allocate_version(
            SCOPE,
            AGENT_ID,
            lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
        )
        with pytest.raises(CosmosHttpResponseError):
            store.allocate_version(
                SCOPE,
                AGENT_ID,
                lambda sequence: _version(sequence=sequence, version_id=f"version-{sequence}"),
            )


def test_sync_versions_swallows_benign_local_cache_race(
    fake_client_factory: FakeCosmosClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_sync_versions`` reads a document not yet present in this
    instance's local cache and tries to insert it. If a concurrent call on
    the same instance already inserted the exact same document between the
    membership check and the insert (the only way ``AgentStudioStore.
    create_version`` can raise here), that must be swallowed as a harmless
    race rather than propagated."""
    store = _new_store(fake_client_factory)
    version = _version(sequence=1, version_id="version-race")
    container = _metadata_container(fake_client_factory)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="version::version-race",
        document_type="version",
        payload=version.model_dump(mode="json"),
    )

    def _raise_already_exists(
        self: cosmos_store.CosmosAgentStudioStore, scope: ScopeContext, version: AgentVersion
    ) -> AgentVersion:
        raise AgentStudioStoreError(f"Version '{version.id}' already exists; versions are immutable.")

    monkeypatch.setattr(AgentStudioStore, "create_version", _raise_already_exists)

    store._sync_versions(SCOPE, AGENT_ID)  # must not raise

    assert version.id not in store._versions


def test_allocate_version_is_race_free_across_concurrent_store_instances(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Simulate multiple app instances/processes racing to cut versions.

    Each thread uses its OWN ``CosmosAgentStudioStore`` instance (its own
    in-memory cache), but all instances share the same underlying
    ``FakeContainer`` documents -- mirroring multiple API replicas hitting
    the same Cosmos container concurrently. The CAS-based sequence counter
    must hand out a strictly-unique sequence to every successful caller with
    no duplicates, even though gaps are acceptable.
    """
    thread_count = 12
    allocated_sequences: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def _allocate(worker_index: int) -> None:
        store = _new_store(fake_client_factory)
        barrier.wait()

        def _builder(sequence: int, worker_index: int = worker_index) -> AgentVersion:
            return _version(sequence=sequence, version_id=f"concurrent-version-{worker_index}")

        try:
            version = store.allocate_version(SCOPE, AGENT_ID, _builder)
        except BaseException as exc:  # capture every failure mode for the assertion below
            with lock:
                errors.append(exc)
            return
        with lock:
            allocated_sequences.append(version.sequence)

    threads = [threading.Thread(target=_allocate, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(allocated_sequences) == thread_count
    assert len(set(allocated_sequences)) == thread_count, (
        f"expected {thread_count} unique sequence numbers, got duplicates: {allocated_sequences}"
    )

    final_store = _new_store(fake_client_factory)
    persisted = final_store.list_versions(SCOPE, AGENT_ID)
    assert len(persisted) == thread_count
    assert sorted(v.sequence for v in persisted) == sorted(allocated_sequences)
    assert len({v.id for v in persisted}) == thread_count


def test_lineage_and_gate_reports_round_trip_without_scope_leakage(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    edge = _lineage()
    report = _gate_report()

    assert store.add_lineage_edge(SCOPE, edge) == edge
    assert store.save_gate_report(SCOPE, report) == report

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_lineage(SCOPE, AGENT_ID) == (edge,)
    assert reloaded.list_lineage(SCOPE, AGENT_ID) == (edge,)
    assert reloaded.list_lineage(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert reloaded.list_lineage(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()
    assert reloaded.get_gate_report(SCOPE, report.id) == report
    assert reloaded.get_gate_report(SCOPE, report.id) == report
    assert reloaded.get_gate_report(SCOPE, "missing-report") is None
    assert reloaded.get_gate_report(SAME_TENANT_OTHER_PROJECT_SCOPE, report.id) is None
    assert reloaded.get_gate_report(OTHER_TENANT_SAME_PROJECT_SCOPE, report.id) is None

    container = _metadata_container(fake_client_factory)
    gate_report_document = container.get_document(SCOPE.scope_key, "gate_report::report-1")
    assert gate_report_document["documentType"] == "gate_report"
    assert gate_report_document["scope_key"] == SCOPE.scope_key
    assert gate_report_document["payload"] == report.model_dump(mode="json")

    mismatched = _gate_report(report_id="mismatch", project_id=OTHER_PROJECT)
    with pytest.raises(AgentStudioStoreError, match="does not match"):
        store.save_gate_report(SCOPE, mismatched)


def test_get_gate_report_rejects_document_with_mismatched_scope_payload(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """``save_gate_report`` always writes a payload whose tenant/project
    agree with the partition it is stored under, so an untampered read can
    never observe a scope mismatch -- this defends against a corrupted or
    hand-edited document landing in the right partition (e.g. an operator
    fixing up data, or a future bug in a sibling writer) with the wrong
    ``tenant_id``/``project_id`` recorded inside its payload. Inject such a
    document directly into the fake container (bypassing the store's own
    validated write path) to prove the read-time guard still refuses to
    return -- and does not cache -- a document whose payload scope disagrees
    with the partition it was read from."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)

    corrupted = _gate_report(report_id="corrupted", tenant_id=OTHER_TENANT, project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="gate_report::corrupted",
        document_type="gate_report",
        payload=corrupted.model_dump(mode="json"),
    )

    assert store.get_gate_report(SCOPE, "corrupted") is None
    # The mismatch guard must prevent caching, too -- a second read hits the
    # same rejected path rather than returning a stale, cached local copy.
    assert store.get_gate_report(SCOPE, "corrupted") is None
    assert AgentStudioStore.get_gate_report(store, SCOPE, "corrupted") is None


def test_releases_round_trip_latest_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    gated = _release(release_id="release-1")
    active = _release(
        release_id="release-2",
        status=ReleaseStatus.ACTIVE,
        previous_release_id=gated.id,
    )
    other_version = _release(release_id="release-3", version_id="version-2")
    assert first.create_release(SCOPE, gated) == gated
    assert first.create_release(SCOPE, active) == active
    assert first.create_release(SCOPE, other_version) == other_version

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_releases_for_version(SCOPE, "version-1") == (gated, active)
    assert reloaded.list_releases_for_version(SCOPE, "version-1") == (gated, active)
    assert reloaded.latest_release_for_version(SCOPE, "version-1") == active
    assert reloaded.latest_release_for_version(SCOPE, "missing-version") is None

    # Finding #9 regression: the release lookup must filter by version_id
    # *inside* the Cosmos query (server-side), never load every release
    # ever created in scope and filter client-side. Assert the exact query
    # shape sent to the container.
    container_for_reloaded = _metadata_container(fake_client_factory)
    assert any(
        entry["partition_key"] == SCOPE.scope_key
        and "c.payload.version_id = @versionId" in entry["query"]
        and entry["parameters"]
        == [
            {"name": "@documentType", "value": "release"},
            {"name": "@versionId", "value": "version-1"},
        ]
        for entry in container_for_reloaded.query_log
    )

    getter = _new_store(fake_client_factory)
    assert getter.get_release(SCOPE, gated.id) == gated
    assert getter.get_release(SCOPE, gated.id) == gated
    assert getter.get_release(SCOPE, "missing-release") is None
    assert getter.get_release(SAME_TENANT_OTHER_PROJECT_SCOPE, gated.id) is None
    assert getter.list_releases_for_version(SAME_TENANT_OTHER_PROJECT_SCOPE, "version-1") == ()
    assert getter.get_release(OTHER_TENANT_SAME_PROJECT_SCOPE, gated.id) is None
    assert getter.list_releases_for_version(OTHER_TENANT_SAME_PROJECT_SCOPE, "version-1") == ()

    container = _metadata_container(fake_client_factory)
    mismatched = _release(release_id="release-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="release::release-mismatch",
        document_type="release",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_release(SCOPE, "release-mismatch") is None


def test_create_release_rejects_duplicate_successor_for_same_predecessor(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Two releases that both claim the same predecessor (including two
    "first" releases for the same version, whose predecessor is ``None``)
    must never both succeed -- this is the promotion/activation
    double-release race. The loser must raise
    ``ReleaseSuccessorConflictError`` naming the winner, and the losing
    release must never be persisted.
    """
    store = _new_store(fake_client_factory)
    first_release = _release(release_id="release-first")
    assert store.create_release(SCOPE, first_release) == first_release

    duplicate_first = _release(release_id="release-first-duplicate")
    with pytest.raises(ReleaseSuccessorConflictError, match="release-first"):
        store.create_release(SCOPE, duplicate_first)
    assert store.get_release(SCOPE, "release-first-duplicate") is None

    approved = _release(
        release_id="release-approved",
        status=ReleaseStatus.APPROVED,
        previous_release_id=first_release.id,
    )
    assert store.create_release(SCOPE, approved) == approved

    rival_approved = _release(
        release_id="release-approved-rival",
        status=ReleaseStatus.APPROVED,
        previous_release_id=first_release.id,
    )
    with pytest.raises(ReleaseSuccessorConflictError, match="release-approved"):
        store.create_release(SCOPE, rival_approved)
    assert store.get_release(SCOPE, "release-approved-rival") is None

    # A distinct predecessor (a different version entirely) is unaffected.
    other_version_first = _release(release_id="release-other-first", version_id="version-2")
    assert store.create_release(SCOPE, other_version_first) == other_version_first


def test_create_release_raises_when_successor_guard_conflict_document_vanishes(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """If the guard document is deleted between the conflicting
    ``create_item`` 409 and the read-back (an extremely narrow window),
    the error must still surface clearly rather than raising a bare
    ``KeyError``/``TypeError`` or silently succeeding.
    """
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item

    def _flaky_create_item(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "release_successor":
            container.documents.pop((body["scope_key"], body["id"]), None)
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=409,
                message="conflict: document already exists",
            )
        return original_create_item(body)

    container.create_item = _flaky_create_item  # type: ignore[method-assign]

    with pytest.raises(ReleaseSuccessorConflictError, match="<unknown>"):
        store.create_release(SCOPE, _release(release_id="release-vanishing-guard"))


def test_create_release_propagates_non_conflict_successor_guard_errors(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)

    def _failing_create_item(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "release_successor":
            raise CosmosHttpResponseError(status_code=503, message="unavailable")  # type: ignore[no-untyped-call]
        raise AssertionError("unexpected create_item call")

    container.create_item = _failing_create_item  # type: ignore[method-assign]

    with pytest.raises(CosmosHttpResponseError):
        store.create_release(SCOPE, _release(release_id="release-service-unavailable"))


def test_create_release_is_race_free_across_concurrent_store_instances(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Simulate multiple app instances/processes racing to promote the same
    GATED release (or otherwise transition the same predecessor). Each
    thread uses its OWN ``CosmosAgentStudioStore`` instance, mirroring
    multiple API replicas, but all instances share the same underlying
    ``FakeContainer`` documents. Exactly one successor may be created for
    that predecessor; every other thread must observe
    ``ReleaseSuccessorConflictError``, never a silently-coexisting sibling.
    """
    first = _new_store(fake_client_factory)
    gated = _release(release_id="release-race-gated")
    assert first.create_release(SCOPE, gated) == gated

    thread_count = 12
    successes: list[AgentRelease] = []
    conflicts: list[BaseException] = []
    other_errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def _promote(worker_index: int) -> None:
        store = _new_store(fake_client_factory)
        candidate = _release(
            release_id=f"release-race-approved-{worker_index}",
            status=ReleaseStatus.APPROVED,
            previous_release_id=gated.id,
        )
        barrier.wait()
        try:
            record = store.create_release(SCOPE, candidate)
        except ReleaseSuccessorConflictError as exc:
            with lock:
                conflicts.append(exc)
            return
        except BaseException as exc:  # pragma: no cover - only hit on genuine bugs
            with lock:
                other_errors.append(exc)
            return
        with lock:
            successes.append(record)

    threads = [threading.Thread(target=_promote, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert other_errors == []
    assert len(successes) == 1, f"expected exactly one winning successor, got: {successes}"
    assert len(conflicts) == thread_count - 1

    final_store = _new_store(fake_client_factory)
    releases = final_store.list_releases_for_version(SCOPE, gated.version_id)
    approved_releases = [release for release in releases if release.status == ReleaseStatus.APPROVED]
    assert len(approved_releases) == 1
    assert approved_releases[0].id == successes[0].id


def test_approvals_create_list_get_and_scope_isolation(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    pending = _approval()
    assert first.create_approval(SCOPE, pending) == pending

    duplicate = _approval(approval_id="approval-duplicate")
    reloaded = _new_store(fake_client_factory)
    assert reloaded.create_approval(SCOPE, duplicate) == pending
    assert reloaded.list_approvals(SCOPE) == (pending,)
    assert reloaded.list_approvals(SCOPE, version_id=pending.version_id) == (pending,)
    assert reloaded.list_approvals(SCOPE, version_id="missing-version") == ()
    assert reloaded.get_approval(SCOPE, pending.id) == pending
    assert reloaded.get_approval(SAME_TENANT_OTHER_PROJECT_SCOPE, pending.id) is None
    assert reloaded.list_approvals(SAME_TENANT_OTHER_PROJECT_SCOPE) == ()
    assert reloaded.get_approval(OTHER_TENANT_SAME_PROJECT_SCOPE, pending.id) is None
    assert reloaded.list_approvals(OTHER_TENANT_SAME_PROJECT_SCOPE) == ()

    container = _metadata_container(fake_client_factory)
    approval_documents = [
        document
        for document in container.documents.values()
        if document["scope_key"] == SCOPE.scope_key and document["documentType"] == "approval"
    ]
    assert len(approval_documents) == 1


def test_save_approval_decision_handles_success_missing_decided_and_conflicts(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    pending = _approval()
    store.create_approval(SCOPE, pending)

    approved = pending.model_copy(update={"state": ApprovalState.APPROVED, "approver_id": "approver-1"})
    assert store.save_approval_decision(SCOPE, approved) == approved
    assert store.get_approval(SCOPE, pending.id) == approved

    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        store.save_approval_decision(SCOPE, approved)

    missing_store = _new_store(fake_client_factory)
    with pytest.raises(AgentStudioStoreError, match="Approval 'missing-approval' not found"):
        missing_store.save_approval_decision(SCOPE, _approval(approval_id="missing-approval"))

    conflict_store = _new_store(fake_client_factory)
    conflict_store.create_approval(SCOPE, _approval(approval_id="approval-conflict", idempotency_key="key-conflict"))
    container = _metadata_container(fake_client_factory)
    container.fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="decided concurrently"):
        conflict_store.save_approval_decision(
            SCOPE,
            _approval(
                approval_id="approval-conflict",
                idempotency_key="key-conflict",
                state=ApprovalState.APPROVED,
            ),
        )

    container.fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        conflict_store.save_approval_decision(
            SCOPE,
            _approval(
                approval_id="approval-conflict",
                idempotency_key="key-conflict",
                state=ApprovalState.APPROVED,
            ),
        )
    container.fail_replace_status = None


def test_create_approval_raises_when_dedup_conflict_document_vanishes(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Defensive branch: if ``create_item`` reports a 409 conflict for the
    dedup guard document but a subsequent point-read can't find it (an
    extremely narrow window that should never occur in practice -- e.g. the
    guard was deleted by a concurrent ``save_approval_decision`` between the
    conflict and this read), the store must fail loudly rather than
    silently fabricate a winner from nothing."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item

    def _always_conflict(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "approval_dedup":
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=409, message="conflict: document already exists"
            )
        return original_create_item(body)

    container.create_item = _always_conflict  # type: ignore[method-assign]

    with pytest.raises(AgentStudioStoreError, match="could not be read back"):
        store.create_approval(SCOPE, _approval())


def test_create_approval_propagates_non_conflict_dedup_guard_errors(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A non-409 failure creating the dedup guard document (e.g. a genuine
    service error) must propagate unchanged rather than being swallowed or
    misinterpreted as "someone else already won the race"."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item

    def _always_fail(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "approval_dedup":
            raise CosmosHttpResponseError(status_code=500, message="simulated service error")  # type: ignore[no-untyped-call]
        return original_create_item(body)

    container.create_item = _always_fail  # type: ignore[method-assign]

    with pytest.raises(CosmosHttpResponseError):
        store.create_approval(SCOPE, _approval())


def test_save_approval_decision_tolerates_already_removed_dedup_guard(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Deciding an approval whose dedup guard document is already gone (e.g.
    it was already cleaned up, or this data was seeded without ever going
    through ``create_approval``) must still succeed -- the guard removal is
    best-effort cleanup, not a precondition for a valid decision."""
    store = _new_store(fake_client_factory)
    pending = _approval()
    store.create_approval(SCOPE, pending)

    container = _metadata_container(fake_client_factory)
    dedup_key = store._approval_dedup_key(SCOPE, pending.kind, pending.idempotency_key)
    container.delete_item(item=store._approval_dedup_id(dedup_key), partition_key=SCOPE.scope_key)

    approved = pending.model_copy(update={"state": ApprovalState.APPROVED, "approver_id": "approver-1"})
    assert store.save_approval_decision(SCOPE, approved) == approved
    assert store.get_approval(SCOPE, pending.id) == approved


def test_create_approval_is_race_free_across_concurrent_store_instances(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Simulate multiple app instances/processes racing to request the same
    logical approval (identical idempotency key). Each thread uses its OWN
    ``CosmosAgentStudioStore`` instance (its own in-memory cache), but all
    instances share the same underlying ``FakeContainer`` documents --
    mirroring multiple API replicas hitting the same Cosmos container
    concurrently. Exactly one distinct approval record must result; every
    caller (winner and losers alike) must observe that same record."""
    thread_count = 12
    results: list[StudioApprovalRecord] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def _request(worker_index: int) -> None:
        store = _new_store(fake_client_factory)
        candidate = _approval(approval_id=f"approval-race-{worker_index}", idempotency_key="race-key")
        barrier.wait()
        try:
            record = store.create_approval(SCOPE, candidate)
        except BaseException as exc:  # capture every failure mode for the assertion below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(record)

    threads = [threading.Thread(target=_request, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == thread_count
    assert len({record.id for record in results}) == 1, (
        f"expected exactly one distinct approval record, got: {sorted({r.id for r in results})}"
    )

    final_store = _new_store(fake_client_factory)
    assert len(final_store.list_approvals(SCOPE)) == 1

    winner = results[0]
    decided = winner.model_copy(update={"state": ApprovalState.APPROVED, "approver_id": "approver-1"})
    final_store.save_approval_decision(SCOPE, decided)

    replacement = _approval(approval_id="approval-race-replacement", idempotency_key="race-key")
    reopened_store = _new_store(fake_client_factory)
    assert reopened_store.create_approval(SCOPE, replacement) == replacement


def test_revocations_create_list_sync_and_scope_isolation(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Revocations round-trip via list_revocations, are visible to a fresh
    store instance through ``_sync_revocations`` (mirroring how other
    replicas pick up revocations they didn't create locally), are
    idempotent by ``(scope, approval, idempotency_key)``, and never leak
    across a sibling project or tenant."""
    first = _new_store(fake_client_factory)
    assert first.list_revocations(SCOPE, "approval-1") == ()

    revocation = _revocation()
    assert first.create_revocation(SCOPE, revocation) == revocation

    duplicate = _revocation(revocation_id="revocation-duplicate")
    assert first.create_revocation(SCOPE, duplicate) == revocation

    # A brand new store instance (no local cache) must sync from Cosmos
    # rather than seeing an empty history.
    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_revocations(SCOPE, "approval-1") == (revocation,)
    # A second sync on the same (already-populated) local cache must not
    # attempt to re-add the already-known revocation.
    assert reloaded.list_revocations(SCOPE, "approval-1") == (revocation,)
    assert reloaded.list_revocations(SAME_TENANT_OTHER_PROJECT_SCOPE, "approval-1") == ()
    assert reloaded.list_revocations(OTHER_TENANT_SAME_PROJECT_SCOPE, "approval-1") == ()

    with pytest.raises(AgentStudioStoreError):
        reloaded.create_revocation(SCOPE, _revocation(project_id=OTHER_PROJECT))

    container = _metadata_container(fake_client_factory)
    revocation_documents = [
        document
        for document in container.documents.values()
        if document["scope_key"] == SCOPE.scope_key and document["documentType"] == "approval_revocation"
    ]
    assert len(revocation_documents) == 1


def test_create_revocation_raises_when_dedup_conflict_document_vanishes(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Mirrors the identical defensive branch on ``create_approval``: a 409
    on the revocation dedup guard whose winning document can no longer be
    read back must fail loudly rather than fabricate a result."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item

    def _always_conflict(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "approval_revocation_dedup":
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=409, message="conflict: document already exists"
            )
        return original_create_item(body)

    container.create_item = _always_conflict  # type: ignore[method-assign]

    with pytest.raises(AgentStudioStoreError, match="could not be read back"):
        store.create_revocation(SCOPE, _revocation())


def test_create_revocation_propagates_non_conflict_dedup_guard_errors(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A non-409 failure creating the revocation dedup guard must propagate
    unchanged, exactly like the equivalent approval-dedup-guard branch."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item

    def _always_fail(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "approval_revocation_dedup":
            raise CosmosHttpResponseError(status_code=500, message="simulated service error")  # type: ignore[no-untyped-call]
        return original_create_item(body)

    container.create_item = _always_fail  # type: ignore[method-assign]

    with pytest.raises(CosmosHttpResponseError):
        store.create_revocation(SCOPE, _revocation())


def test_create_revocation_is_race_free_across_concurrent_store_instances(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Simulate multiple app instances racing to revoke the same approval
    under the same idempotency key (same actor/reason). Exactly one
    distinct revocation record must result, and -- unlike approvals -- the
    dedup guard is never released, so a later retry under the same key
    still resolves to the original, permanent revocation."""
    thread_count = 12
    results: list[ApprovalRevocation] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def _request(worker_index: int) -> None:
        store = _new_store(fake_client_factory)
        candidate = _revocation(revocation_id=f"revocation-race-{worker_index}", idempotency_key="rev-race-key")
        barrier.wait()
        try:
            record = store.create_revocation(SCOPE, candidate)
        except BaseException as exc:  # capture every failure mode for the assertion below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(record)

    threads = [threading.Thread(target=_request, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == thread_count
    assert len({revocation.id for revocation in results}) == 1, (
        f"expected exactly one distinct revocation record, got: {sorted({r.id for r in results})}"
    )

    final_store = _new_store(fake_client_factory)
    assert len(final_store.list_revocations(SCOPE, "approval-1")) == 1

    retried = _revocation(revocation_id="revocation-race-retry", idempotency_key="rev-race-key")
    reopened_store = _new_store(fake_client_factory)
    assert reopened_store.create_revocation(SCOPE, retried) == results[0]


def test_approval_consumptions_create_get_sync_and_scope_isolation(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Approval consumption records round-trip via ``get_approval_consumption``,
    are visible to a fresh store instance through the Cosmos point read
    (mirroring how other replicas pick up a consumption they didn't create
    locally), are single-use per ``(scope, approval)`` regardless of
    idempotency key, and never leak across a sibling project or tenant."""
    first = _new_store(fake_client_factory)
    assert first.get_approval_consumption(SCOPE, "approval-1") is None

    consumption = _consumption()
    assert first.create_approval_consumption(SCOPE, consumption) == consumption

    # A second attempt against the same approval never wins, even under a
    # different idempotency key -- single-use, not merely idempotent.
    duplicate = _consumption(consumption_id="consumption-duplicate", idempotency_key="different-key")
    assert first.create_approval_consumption(SCOPE, duplicate) == consumption

    # A brand new store instance (no local cache) must read through to
    # Cosmos rather than seeing no consumption record.
    reloaded = _new_store(fake_client_factory)
    assert reloaded.get_approval_consumption(SCOPE, "approval-1") == consumption
    # A second read on the same (already-populated) local cache must not
    # re-fetch from Cosmos.
    assert reloaded.get_approval_consumption(SCOPE, "approval-1") == consumption
    assert reloaded.get_approval_consumption(SAME_TENANT_OTHER_PROJECT_SCOPE, "approval-1") is None
    assert reloaded.get_approval_consumption(OTHER_TENANT_SAME_PROJECT_SCOPE, "approval-1") is None

    with pytest.raises(AgentStudioStoreError):
        reloaded.create_approval_consumption(SCOPE, _consumption(project_id=OTHER_PROJECT))

    container = _metadata_container(fake_client_factory)
    consumption_documents = [
        document
        for document in container.documents.values()
        if document["scope_key"] == SCOPE.scope_key and document["documentType"] == "approval_consumption"
    ]
    assert len(consumption_documents) == 1


def test_create_approval_consumption_raises_when_conflict_document_vanishes(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Mirrors the identical defensive branch on ``create_revocation``: a
    409 on the consumption guard whose winning document can no longer be
    read back must fail loudly rather than fabricate a result."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item

    def _always_conflict(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "approval_consumption":
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=409, message="conflict: document already exists"
            )
        return original_create_item(body)

    container.create_item = _always_conflict  # type: ignore[method-assign]

    with pytest.raises(AgentStudioStoreError, match="could not be read back"):
        store.create_approval_consumption(SCOPE, _consumption())


def test_create_approval_consumption_propagates_non_conflict_errors(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A non-409 failure creating the consumption record must propagate
    unchanged, exactly like the equivalent revocation-guard branch."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item

    def _always_fail(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "approval_consumption":
            raise CosmosHttpResponseError(status_code=500, message="simulated service error")  # type: ignore[no-untyped-call]
        return original_create_item(body)

    container.create_item = _always_fail  # type: ignore[method-assign]

    with pytest.raises(CosmosHttpResponseError):
        store.create_approval_consumption(SCOPE, _consumption())


def test_create_approval_consumption_is_race_free_across_concurrent_store_instances(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Simulate multiple app instances racing to consume the same
    single-use approval under different idempotency keys (different
    invocations). Exactly one distinct consumption record must result, and
    a later retry -- even under yet another idempotency key -- still
    resolves to the original, permanent consumption."""
    thread_count = 12
    results: list[ApprovalConsumptionRecord] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def _request(worker_index: int) -> None:
        store = _new_store(fake_client_factory)
        candidate = _consumption(
            consumption_id=f"consumption-race-{worker_index}", idempotency_key=f"race-key-{worker_index}"
        )
        barrier.wait()
        try:
            record = store.create_approval_consumption(SCOPE, candidate)
        except BaseException as exc:  # capture every failure mode for the assertion below
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(record)

    threads = [threading.Thread(target=_request, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(results) == thread_count
    assert len({record.id for record in results}) == 1, (
        f"expected exactly one distinct consumption record, got: {sorted({r.id for r in results})}"
    )

    final_store = _new_store(fake_client_factory)
    assert final_store.get_approval_consumption(SCOPE, "approval-1") == results[0]

    retried = _consumption(consumption_id="consumption-race-retry", idempotency_key="race-key-retry")
    reopened_store = _new_store(fake_client_factory)
    assert reopened_store.create_approval_consumption(SCOPE, retried) == results[0]


def test_idempotency_claim_lifecycle_round_trips_across_store_instances(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Full claim -> mark_in_progress -> complete -> load_result round trip,
    each step performed from a *fresh* store instance (no shared in-process
    cache) to prove every step genuinely reads/writes through Cosmos rather
    than relying on the claiming instance's own local cache."""
    key = _idempotency_key()

    claimer = _new_store(fake_client_factory)
    claim = claimer.claim_idempotency(SCOPE, key, actor_id=USER_ID, release_id="release-1", lease_seconds=300)
    assert claim.disposition is IdempotencyClaimDisposition.ACQUIRED
    assert claim.claim_token is not None

    progresser = _new_store(fake_client_factory)
    in_progress = progresser.mark_idempotency_in_progress(
        SCOPE, key, claim_token=claim.claim_token, expected_version=claim.record.version, irreversible=True
    )
    assert in_progress.state is IdempotencyState.IN_PROGRESS
    assert in_progress.version == "2"

    completer = _new_store(fake_client_factory)
    completed = completer.complete_idempotency(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version=in_progress.version,
        result={"status": "ok", "value": 42},
        result_hash="e" * 64,
    )
    assert completed.state is IdempotencyState.COMPLETED
    assert completed.result_ref is not None

    loader = _new_store(fake_client_factory)
    assert loader.load_idempotency_result(SCOPE, completed.result_ref) == {"status": "ok", "value": 42}
    # Repeated read hits the now-populated local cache rather than Cosmos again.
    assert loader.load_idempotency_result(SCOPE, completed.result_ref) == {"status": "ok", "value": 42}

    getter = _new_store(fake_client_factory)
    assert getter.get_idempotency_record(SCOPE, key) == completed
    assert getter.get_idempotency_record(SCOPE, key) == completed

    # Cross-project/cross-tenant isolation, even though the key digest and
    # result_ref strings themselves are identical apart from scope.
    # ``get_idempotency_record`` takes the full key (not just an opaque
    # ref), so a scope/key mismatch is a hard application-level error --
    # never a silent None -- exactly like ``claim_idempotency``'s own guard.
    with pytest.raises(AgentStudioStoreError):
        getter.get_idempotency_record(SAME_TENANT_OTHER_PROJECT_SCOPE, key)
    assert loader.load_idempotency_result(SAME_TENANT_OTHER_PROJECT_SCOPE, completed.result_ref) is None
    with pytest.raises(AgentStudioStoreError):
        getter.get_idempotency_record(OTHER_TENANT_SAME_PROJECT_SCOPE, key)
    assert loader.load_idempotency_result(OTHER_TENANT_SAME_PROJECT_SCOPE, completed.result_ref) is None


def test_idempotency_get_record_and_load_result_return_none_when_never_claimed(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    assert store.get_idempotency_record(SCOPE, _idempotency_key()) is None
    assert store.load_idempotency_result(SCOPE, "idempotency-result::" + "e" * 64) is None


def test_claim_idempotency_rejects_out_of_bounds_lease_and_scope_mismatch(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    with pytest.raises(ValueError, match="lease_seconds"):
        store.claim_idempotency(SCOPE, _idempotency_key(), actor_id=USER_ID, release_id="release-1", lease_seconds=0)

    mismatched_key = _idempotency_key(project_id=OTHER_PROJECT)
    with pytest.raises(AgentStudioStoreError, match="does not"):
        store.claim_idempotency(
            SCOPE, mismatched_key, actor_id=USER_ID, release_id="release-1", lease_seconds=300
        )


def test_claim_idempotency_on_conflict_translates_existing_state_to_disposition(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A second ``claim_idempotency`` against an already-claimed key must
    read back the winning Cosmos document (never raise) and translate its
    current state into the correct non-``ACQUIRED`` disposition -- covering
    all three non-acquiring branches: in-progress/claimed-and-fresh,
    lease-expired, and terminally failed."""
    key = _idempotency_key(caller_key="conflict-fresh")
    store = _new_store(fake_client_factory)
    first = store.claim_idempotency(SCOPE, key, actor_id=USER_ID, release_id="release-1", lease_seconds=300)
    assert first.claim_token is not None

    second_store = _new_store(fake_client_factory)
    second = second_store.claim_idempotency(SCOPE, key, actor_id=USER_ID, release_id="release-1", lease_seconds=300)
    assert second.disposition is IdempotencyClaimDisposition.IN_PROGRESS
    assert second.claim_token is None
    assert second.record == first.record

    # Lease-expired branch.
    expired_key = _idempotency_key(caller_key="conflict-expired")
    now = utc_now()
    expiring_store = _new_store(fake_client_factory)
    expiring_store.claim_idempotency(
        SCOPE, expired_key, actor_id=USER_ID, release_id="release-1", lease_seconds=1, now=now - timedelta(seconds=10)
    )
    reclaim_store = _new_store(fake_client_factory)
    reclaim = reclaim_store.claim_idempotency(
        SCOPE, expired_key, actor_id=USER_ID, release_id="release-1", lease_seconds=300, now=now
    )
    assert reclaim.disposition is IdempotencyClaimDisposition.RECONCILIATION_REQUIRED

    # Terminally-failed branch.
    failed_key = _idempotency_key(caller_key="conflict-failed")
    failing_store = _new_store(fake_client_factory)
    failed_claim = failing_store.claim_idempotency(
        SCOPE, failed_key, actor_id=USER_ID, release_id="release-1", lease_seconds=300
    )
    assert failed_claim.claim_token is not None
    failing_store.fail_idempotency(
        SCOPE, failed_key, claim_token=failed_claim.claim_token, expected_version="1", failure_code="boom"
    )
    reclaim_failed_store = _new_store(fake_client_factory)
    reclaim_failed = reclaim_failed_store.claim_idempotency(
        SCOPE, failed_key, actor_id=USER_ID, release_id="release-1", lease_seconds=300
    )
    assert reclaim_failed.disposition is IdempotencyClaimDisposition.RECONCILIATION_REQUIRED
    assert reclaim_failed.record.state is IdempotencyState.FAILED

    # Completed branch.
    completed_key = _idempotency_key(caller_key="conflict-completed")
    completing_store = _new_store(fake_client_factory)
    completed_claim = completing_store.claim_idempotency(
        SCOPE, completed_key, actor_id=USER_ID, release_id="release-1", lease_seconds=300
    )
    assert completed_claim.claim_token is not None
    completing_store.complete_idempotency(
        SCOPE,
        completed_key,
        claim_token=completed_claim.claim_token,
        expected_version="1",
        result={"status": "ok"},
        result_hash="e" * 64,
    )
    reclaim_completed_store = _new_store(fake_client_factory)
    reclaim_completed = reclaim_completed_store.claim_idempotency(
        SCOPE, completed_key, actor_id=USER_ID, release_id="release-1", lease_seconds=300
    )
    assert reclaim_completed.disposition is IdempotencyClaimDisposition.COMPLETED
    assert reclaim_completed.claim_token is None


def test_claim_idempotency_raises_when_conflict_document_vanishes(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Mirrors the identical defensive branch on approval-consumption/
    revocation creation: a 409 on the claim guard whose winning document can
    no longer be read back must fail loudly rather than fabricate a claim."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item

    def _always_conflict(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "idempotency_record":
            raise CosmosHttpResponseError(  # type: ignore[no-untyped-call]
                status_code=409, message="conflict: document already exists"
            )
        return original_create_item(body)

    container.create_item = _always_conflict  # type: ignore[method-assign]

    with pytest.raises(AgentStudioStoreError, match="could not be read back"):
        store.claim_idempotency(SCOPE, _idempotency_key(), actor_id=USER_ID, release_id="release-1", lease_seconds=300)


def test_claim_idempotency_propagates_non_conflict_errors(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A non-409 failure creating the claim guard document must propagate
    unchanged, exactly like the equivalent approval-consumption/revocation
    guard branches."""
    store = _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    original_create_item = container.create_item

    def _always_fail(body: dict[str, Any]) -> dict[str, Any]:
        if body["documentType"] == "idempotency_record":
            raise CosmosHttpResponseError(status_code=500, message="simulated service error")  # type: ignore[no-untyped-call]
        return original_create_item(body)

    container.create_item = _always_fail  # type: ignore[method-assign]

    with pytest.raises(CosmosHttpResponseError):
        store.claim_idempotency(SCOPE, _idempotency_key(), actor_id=USER_ID, release_id="release-1", lease_seconds=300)


def test_claim_idempotency_is_race_free_across_concurrent_store_instances(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Simulate multiple app instances racing to claim the exact same
    idempotency key. Exactly one distinct claim_token/``ACQUIRED`` winner
    must result; every other concurrent attempt must observe the same
    resulting record via a non-``ACQUIRED`` disposition, never raise, and
    never itself receive a second independent claim_token."""
    key = _idempotency_key(caller_key="race-key")
    thread_count = 12
    claims: list[IdempotencyClaimDisposition] = []
    records_by_worker: dict[int, Any] = {}
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def _attempt(worker_index: int) -> None:
        store = _new_store(fake_client_factory)
        barrier.wait()
        try:
            claim = store.claim_idempotency(
                SCOPE, key, actor_id=f"user-{worker_index}", release_id="release-1", lease_seconds=300
            )
        except BaseException as exc:  # capture every failure mode for the assertion below
            with lock:
                errors.append(exc)
            return
        with lock:
            claims.append(claim.disposition)
            records_by_worker[worker_index] = (claim.record, claim.claim_token)

    threads = [threading.Thread(target=_attempt, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(claims) == thread_count
    acquired = [disposition for disposition in claims if disposition is IdempotencyClaimDisposition.ACQUIRED]
    assert len(acquired) == 1
    distinct_records = {record.key.digest for record, _ in records_by_worker.values()}
    assert distinct_records == {key.digest}
    # Exactly one worker received a real claim_token.
    tokens = [token for _, token in records_by_worker.values() if token is not None]
    assert len(tokens) == 1

    final_store = _new_store(fake_client_factory)
    final_record = final_store.get_idempotency_record(SCOPE, key)
    assert final_record is not None
    assert final_record.state is IdempotencyState.CLAIMED


def test_transition_idempotency_record_handles_missing_key_and_concurrency_conflicts(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Covers the application-level ``claim_token``/``expected_version``
    ownership check (never touching Cosmos at all for a truly-unclaimed
    key), the Cosmos-native ETag-guarded ``replace_item`` 412 branch, and
    non-conflict error propagation -- mirroring
    ``test_update_deployment_handles_missing_and_conflicts`` exactly."""
    key = _idempotency_key(caller_key="transition-conflict")

    missing_store = _new_store(fake_client_factory)
    with pytest.raises(IdempotencyNotFoundError):
        missing_store.mark_idempotency_in_progress(
            SCOPE, key, claim_token="token", expected_version="1", irreversible=False
        )

    store = _new_store(fake_client_factory)
    claim = store.claim_idempotency(SCOPE, key, actor_id=USER_ID, release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None

    # Wrong claim_token / wrong expected_version -- an application-level
    # ownership mismatch caught *before* any Cosmos replace_item call, so
    # this must raise even with no injected Cosmos failure at all.
    with pytest.raises(IdempotencyConcurrencyError):
        store.mark_idempotency_in_progress(
            SCOPE, key, claim_token="wrong-token", expected_version="1", irreversible=False
        )
    with pytest.raises(IdempotencyConcurrencyError):
        store.mark_idempotency_in_progress(
            SCOPE, key, claim_token=claim.claim_token, expected_version="99", irreversible=False
        )

    container = _metadata_container(fake_client_factory)
    container.fail_replace_status = 412
    with pytest.raises(IdempotencyConcurrencyError, match="modified concurrently"):
        store.mark_idempotency_in_progress(
            SCOPE, key, claim_token=claim.claim_token, expected_version="1", irreversible=False
        )

    container.fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        store.mark_idempotency_in_progress(
            SCOPE, key, claim_token=claim.claim_token, expected_version="1", irreversible=False
        )
    container.fail_replace_status = None

    # The successful path still works after the injected failures are cleared.
    updated = store.mark_idempotency_in_progress(
        SCOPE, key, claim_token=claim.claim_token, expected_version="1", irreversible=True
    )
    assert updated.state is IdempotencyState.IN_PROGRESS
    assert updated.version == "2"


def test_complete_idempotency_writes_record_and_result_via_single_atomic_batch(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """``complete_idempotency`` durably writes two distinct Cosmos documents
    -- the transitioned record and a separate ``idempotency_result``
    document (keyed by ``result_hash``, never the key digest) -- via a
    *single* Cosmos transactional batch (``execute_item_batch``) rather than
    two independent calls, so the two writes can never be observed as
    partially applied. Both documents are independently readable
    afterward."""
    key = _idempotency_key(caller_key="complete-result")
    store = _new_store(fake_client_factory)
    claim = store.claim_idempotency(SCOPE, key, actor_id=USER_ID, release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None

    container = _metadata_container(fake_client_factory)
    original_execute_item_batch = container.execute_item_batch
    batch_calls: list[list[Any]] = []

    def _spy_execute_item_batch(batch_operations: list[Any], partition_key: str) -> list[dict[str, Any]]:
        batch_calls.append(deepcopy(batch_operations))
        return original_execute_item_batch(batch_operations, partition_key=partition_key)

    container.execute_item_batch = _spy_execute_item_batch  # type: ignore[method-assign]

    completed = store.complete_idempotency(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version="1",
        result={"status": "ok", "items": [1, 2, 3]},
        result_hash="f" * 64,
    )
    assert completed.result_ref == "idempotency-result::" + "f" * 64

    # Exactly one atomic batch call, containing both the record replace and
    # the result upsert -- never two independent write calls.
    assert len(batch_calls) == 1
    op_types = [operation[0] for operation in batch_calls[0]]
    assert op_types == ["replace", "upsert"]

    result_documents = [
        document
        for document in container.documents.values()
        if document["scope_key"] == SCOPE.scope_key and document["documentType"] == "idempotency_result"
    ]
    assert len(result_documents) == 1
    assert result_documents[0]["id"] == "idempotency-result::" + "f" * 64
    assert result_documents[0]["payload"] == {"status": "ok", "items": [1, 2, 3]}

    fresh_reader = _new_store(fake_client_factory)
    assert fresh_reader.load_idempotency_result(SCOPE, completed.result_ref) == {
        "status": "ok",
        "items": [1, 2, 3],
    }

    # Completing again with the same claim_token/expected_version is a
    # concurrency conflict, not a silent no-op -- the record already moved
    # past version "1".
    with pytest.raises(IdempotencyConcurrencyError):
        store.complete_idempotency(
            SCOPE,
            key,
            claim_token=claim.claim_token,
            expected_version="1",
            result={"status": "different"},
            result_hash="0" * 64,
        )


def test_complete_idempotency_raises_not_found_for_never_claimed_key(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """``complete_idempotency`` must never touch the atomic batch call at
    all for a key that was never claimed -- there is no record document to
    read, so it must fail with ``IdempotencyNotFoundError`` before any
    Cosmos write is attempted."""
    key = _idempotency_key(caller_key="complete-never-claimed")
    store = _new_store(fake_client_factory)
    with pytest.raises(IdempotencyNotFoundError):
        store.complete_idempotency(
            SCOPE,
            key,
            claim_token="token",
            expected_version="1",
            result={"status": "ok"},
            result_hash="a" * 64,
        )


def test_complete_idempotency_batch_conflict_translates_to_concurrency_error(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A 412 (etag mismatch) failure from the atomic batch call must
    translate to ``IdempotencyConcurrencyError``, exactly like the
    equivalent ``replace_item`` 412 branch for every other transition."""
    key = _idempotency_key(caller_key="complete-batch-conflict")
    store = _new_store(fake_client_factory)
    claim = store.claim_idempotency(SCOPE, key, actor_id=USER_ID, release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None

    container = _metadata_container(fake_client_factory)
    container.fail_batch_status = 412
    with pytest.raises(IdempotencyConcurrencyError, match="modified concurrently"):
        store.complete_idempotency(
            SCOPE,
            key,
            claim_token=claim.claim_token,
            expected_version="1",
            result={"status": "ok"},
            result_hash="a" * 64,
        )
    container.fail_batch_status = None

    # The successful path still works once the injected failure clears.
    completed = store.complete_idempotency(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version="1",
        result={"status": "ok"},
        result_hash="a" * 64,
    )
    assert completed.state is IdempotencyState.COMPLETED


def test_complete_idempotency_batch_failure_leaves_no_partial_write(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A failed batch call must never leave a partially-applied state:
    neither the record transition nor the result document write may be
    observed, closing exactly the hazard a two-separate-writes
    implementation would have (a durably ``COMPLETED`` record whose
    ``result_ref`` points at nothing)."""
    key = _idempotency_key(caller_key="complete-batch-no-partial")
    store = _new_store(fake_client_factory)
    claim = store.claim_idempotency(SCOPE, key, actor_id=USER_ID, release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None

    container = _metadata_container(fake_client_factory)
    container.fail_batch_status = 500
    with pytest.raises(CosmosBatchOperationError):
        store.complete_idempotency(
            SCOPE,
            key,
            claim_token=claim.claim_token,
            expected_version="1",
            result={"status": "ok"},
            result_hash="b" * 64,
        )
    container.fail_batch_status = None

    # Observed from a completely fresh instance (no shared in-process
    # cache): the record must still be exactly its pre-complete CLAIMED
    # state, and no result document may exist anywhere in the container.
    fresh_reader = _new_store(fake_client_factory)
    record = fresh_reader.get_idempotency_record(SCOPE, key)
    assert record is not None
    assert record.state is IdempotencyState.CLAIMED
    assert record.version == "1"
    assert record.result_ref is None
    result_documents = [
        document for document in container.documents.values() if document["documentType"] == "idempotency_result"
    ]
    assert result_documents == []

    # The successful path still works once the injected failure clears.
    completed = store.complete_idempotency(
        SCOPE,
        key,
        claim_token=claim.claim_token,
        expected_version="1",
        result={"status": "ok"},
        result_hash="b" * 64,
    )
    assert completed.state is IdempotencyState.COMPLETED


def test_complete_idempotency_concurrent_completions_yield_exactly_one_winner(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Two concurrent ``complete_idempotency`` calls against the same
    claimed key (both starting from version "1") must resolve to exactly
    one durable winner; every other concurrent attempt must fail with
    ``IdempotencyConcurrencyError``, never silently overwrite the winner's
    result."""
    key = _idempotency_key(caller_key="complete-race")
    claimer = _new_store(fake_client_factory)
    claim = claimer.claim_idempotency(SCOPE, key, actor_id=USER_ID, release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None

    thread_count = 8
    successes: list[Any] = []
    failures: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(thread_count)

    def _attempt(worker_index: int) -> None:
        store = _new_store(fake_client_factory)
        barrier.wait()
        try:
            completed = store.complete_idempotency(
                SCOPE,
                key,
                claim_token=claim.claim_token,
                expected_version="1",
                result={"status": "ok", "worker": worker_index},
                result_hash=f"{worker_index:064d}",
            )
        except IdempotencyConcurrencyError as exc:
            with lock:
                failures.append(exc)
            return
        with lock:
            successes.append(completed)

    threads = [threading.Thread(target=_attempt, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(successes) == 1
    assert len(failures) == thread_count - 1

    final_store = _new_store(fake_client_factory)
    final_record = final_store.get_idempotency_record(SCOPE, key)
    assert final_record is not None
    assert final_record == successes[0]
    assert final_record.result_ref is not None
    assert final_store.load_idempotency_result(SCOPE, final_record.result_ref) == {
        "status": "ok",
        "worker": next(
            worker
            for worker in range(thread_count)
            if final_record.result_ref == "idempotency-result::" + f"{worker:064d}"
        ),
    }


def test_fail_idempotency_marks_reconciliation_required_and_persists(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    key = _idempotency_key(caller_key="fail-persist")
    store = _new_store(fake_client_factory)
    claim = store.claim_idempotency(SCOPE, key, actor_id=USER_ID, release_id="release-1", lease_seconds=300)
    assert claim.claim_token is not None

    failed = store.fail_idempotency(
        SCOPE, key, claim_token=claim.claim_token, expected_version="1", failure_code="downstream-timeout"
    )
    assert failed.state is IdempotencyState.FAILED
    assert failed.failure_code == "downstream-timeout"
    assert failed.reconciliation_required is True

    reloaded = _new_store(fake_client_factory)
    persisted = reloaded.get_idempotency_record(SCOPE, key)
    assert persisted == failed


def test_deployments_create_list_get_update_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    deployment = _deployment()
    other_agent = _deployment(deployment_id="deployment-2", logical_agent_id=OTHER_AGENT_ID)
    assert first.create_deployment(SCOPE, deployment) == deployment
    assert first.create_deployment(SCOPE, other_agent) == other_agent

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_deployments(SCOPE, AGENT_ID) == (deployment,)
    assert reloaded.list_deployments(SCOPE, AGENT_ID) == (deployment,)
    assert reloaded.list_deployments(SCOPE, OTHER_AGENT_ID) == (other_agent,)
    getter = _new_store(fake_client_factory)
    assert getter.get_deployment(SCOPE, deployment.id) == deployment
    assert getter.get_deployment(SCOPE, deployment.id) == deployment
    assert getter.get_deployment(SCOPE, "missing-deployment") is None
    assert getter.get_deployment(SAME_TENANT_OTHER_PROJECT_SCOPE, deployment.id) is None
    assert reloaded.list_deployments(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert getter.get_deployment(OTHER_TENANT_SAME_PROJECT_SCOPE, deployment.id) is None
    assert reloaded.list_deployments(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    updated = deployment.model_copy(update={"trace_ref": "trace-1"})
    assert reloaded.update_deployment(SCOPE, updated) == updated
    assert reloaded.get_deployment(SCOPE, deployment.id) == updated

    container = _metadata_container(fake_client_factory)
    mismatched = _deployment(deployment_id="deployment-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="deployment::deployment-mismatch",
        document_type="deployment",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_deployment(SCOPE, "deployment-mismatch") is None


def test_get_deployment_reflects_cross_replica_health_change_without_cache_shortcut(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Two independent ``CosmosAgentStudioStore`` instances share one Cosmos
    container -- mirroring two API replicas. A status/health change written
    through replica B must be visible on replica A's *next* read, even
    though replica A already cached the deployment from an earlier read.
    A cache-first ``get_deployment`` would keep serving replica A's stale
    ``HEALTHY`` snapshot forever, letting an ACTIVE/health decision succeed
    against data Cosmos no longer reflects.
    """
    replica_a = _new_store(fake_client_factory)
    replica_b = _new_store(fake_client_factory)

    deployment = _deployment(deployment_id="deployment-cross-replica")
    replica_b.create_deployment(SCOPE, deployment)

    # Replica A reads and caches the UNKNOWN-health deployment.
    first_read = replica_a.get_deployment(SCOPE, deployment.id)
    assert first_read is not None
    assert first_read.health.status == HealthStatus.UNKNOWN
    assert deployment.id in replica_a._deployments

    # Replica B independently reports HEALTHY, then DEGRADED.
    healthy = deployment.model_copy(update={"health": DeploymentHealth(status=HealthStatus.HEALTHY)})
    replica_b.update_deployment(SCOPE, healthy)
    degraded = healthy.model_copy(update={"health": DeploymentHealth(status=HealthStatus.DEGRADED)})
    replica_b.update_deployment(SCOPE, degraded)

    # Replica A must observe the fresh DEGRADED state on its next read --
    # not the DEGRADED-masking stale HEALTHY value from its own cache.
    second_read = replica_a.get_deployment(SCOPE, deployment.id)
    assert second_read is not None
    assert second_read.health.status == HealthStatus.DEGRADED

    # list_deployments must refresh the already-cached entry too, not just
    # add newly-discovered documents.
    listed = replica_a.list_deployments(SCOPE, AGENT_ID)
    assert len(listed) == 1
    assert listed[0].health.status == HealthStatus.DEGRADED


def test_update_deployment_handles_missing_and_conflicts(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    missing_store = _new_store(fake_client_factory)
    with pytest.raises(AgentStudioStoreError, match="Deployment 'missing-deployment' not found"):
        missing_store.update_deployment(SCOPE, _deployment(deployment_id="missing-deployment"))

    store = _new_store(fake_client_factory)
    current = _deployment(deployment_id="deployment-conflict")
    store.create_deployment(SCOPE, current)

    container = _metadata_container(fake_client_factory)
    container.fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="changed concurrently"):
        store.update_deployment(SCOPE, current.model_copy(update={"trace_ref": "trace-2"}))

    container.fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        store.update_deployment(SCOPE, current.model_copy(update={"trace_ref": "trace-2"}))
    container.fail_replace_status = None


def test_bindings_and_tool_registrations_round_trip(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    binding = _binding()
    registration = _tool_registration()
    other_registration = _tool_registration(registration_id="reg-2", logical_agent_id=OTHER_AGENT_ID)
    assert first.set_binding(SCOPE, binding) == binding
    assert first.create_tool_registration(SCOPE, registration) == registration
    assert first.create_tool_registration(SCOPE, other_registration) == other_registration

    reloaded = _new_store(fake_client_factory)
    assert reloaded.get_binding(SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) == binding
    assert reloaded.get_binding(SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) == binding
    assert reloaded.get_binding(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None
    assert reloaded.get_binding(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None
    assert reloaded.list_tool_registrations(SCOPE, AGENT_ID) == (registration,)
    assert reloaded.list_tool_registrations(SCOPE, AGENT_ID) == (registration,)
    assert reloaded.list_tool_registrations(SCOPE, OTHER_AGENT_ID) == (other_registration,)
    assert reloaded.list_tool_registrations(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert reloaded.list_tool_registrations(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()


def test_get_binding_reflects_cross_replica_rebind_without_cache_shortcut(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A rollback re-pointing a binding on one replica must be observed by
    another replica's next ``get_binding`` -- a cache-first read would keep
    routing/resolve on a version Cosmos no longer designates as current.
    """
    replica_a = _new_store(fake_client_factory)
    replica_b = _new_store(fake_client_factory)

    original = _binding(version_id="version-1")
    replica_b.set_binding(SCOPE, original)

    first_read = replica_a.get_binding(SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT)
    assert first_read is not None
    assert first_read.resolved_version_id == "version-1"
    assert (SCOPE.scope_key, AGENT_ID, DeploymentEnvironment.DEVELOPMENT.value) in replica_a._bindings

    rolled_back = _binding(version_id="version-0-rollback")
    replica_b.set_binding(SCOPE, rolled_back)

    second_read = replica_a.get_binding(SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT)
    assert second_read is not None
    assert second_read.resolved_version_id == "version-0-rollback"


def test_get_binding_rejects_document_with_mismatched_scope_fields(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """A binding document living in the correct partition but whose payload
    disagrees with the requested scope's tenant/project must never be
    returned -- covers the defense-in-depth field check that runs after the
    fresh Cosmos read, independent of partition-key isolation.
    """
    _new_store(fake_client_factory)
    container = _metadata_container(fake_client_factory)
    mismatched = _binding(project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id=f"binding::{AGENT_ID}::{DeploymentEnvironment.DEVELOPMENT.value}",
        document_type="binding",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_binding(SCOPE, AGENT_ID, DeploymentEnvironment.DEVELOPMENT) is None

def test_builder_proposals_create_list_get_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    proposal = _proposal()
    other_agent = _proposal(proposal_id="proposal-2", logical_agent_id=OTHER_AGENT_ID)
    assert first.create_builder_proposal(SCOPE, proposal) == proposal
    assert first.create_builder_proposal(SCOPE, other_agent) == other_agent

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_builder_proposals(SCOPE, AGENT_ID) == (proposal,)
    assert reloaded.list_builder_proposals(SCOPE, AGENT_ID) == (proposal,)
    assert reloaded.list_builder_proposals(SCOPE, OTHER_AGENT_ID) == (other_agent,)
    getter = _new_store(fake_client_factory)
    assert getter.get_builder_proposal(SCOPE, proposal.id) == proposal
    assert getter.get_builder_proposal(SCOPE, proposal.id) == proposal
    assert getter.get_builder_proposal(SCOPE, "missing-proposal") is None
    assert getter.get_builder_proposal(SAME_TENANT_OTHER_PROJECT_SCOPE, proposal.id) is None
    assert reloaded.list_builder_proposals(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert getter.get_builder_proposal(OTHER_TENANT_SAME_PROJECT_SCOPE, proposal.id) is None
    assert reloaded.list_builder_proposals(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    container = _metadata_container(fake_client_factory)
    mismatched = _proposal(proposal_id="proposal-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="builder_proposal::proposal-mismatch",
        document_type="builder_proposal",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_builder_proposal(SCOPE, "proposal-mismatch") is None


def test_get_builder_proposal_reflects_cross_replica_decision_without_cache_shortcut(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    """Once replica B decides a proposal, replica A's next
    ``get_builder_proposal``/``list_builder_proposals`` must show the
    decided state, not a PENDING snapshot cached from an earlier read.
    """
    replica_a = _new_store(fake_client_factory)
    replica_b = _new_store(fake_client_factory)

    proposal = _proposal(proposal_id="proposal-cross-replica")
    replica_b.create_builder_proposal(SCOPE, proposal)

    first_read = replica_a.get_builder_proposal(SCOPE, proposal.id)
    assert first_read is not None
    assert first_read.state == BuilderProposalState.PENDING
    assert proposal.id in replica_a._builder_proposals

    decided = proposal.model_copy(update={"state": BuilderProposalState.APPLIED, "decided_by": "approver-1"})
    replica_b.save_builder_proposal_decision(SCOPE, decided)

    second_read = replica_a.get_builder_proposal(SCOPE, proposal.id)
    assert second_read is not None
    assert second_read.state == BuilderProposalState.APPLIED

    listed = replica_a.list_builder_proposals(SCOPE, AGENT_ID)
    assert len(listed) == 1
    assert listed[0].state == BuilderProposalState.APPLIED


def test_save_builder_proposal_decision_handles_success_missing_decided_and_conflicts(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    store = _new_store(fake_client_factory)
    pending = _proposal()
    store.create_builder_proposal(SCOPE, pending)

    applied = pending.model_copy(update={"state": BuilderProposalState.APPLIED, "decided_by": "approver-1"})
    assert store.save_builder_proposal_decision(SCOPE, applied) == applied
    assert store.get_builder_proposal(SCOPE, pending.id) == applied

    with pytest.raises(AgentStudioStoreError, match="already been decided"):
        store.save_builder_proposal_decision(SCOPE, applied)

    missing_store = _new_store(fake_client_factory)
    with pytest.raises(AgentStudioStoreError, match="Proposal 'missing-proposal' not found"):
        missing_store.save_builder_proposal_decision(SCOPE, _proposal(proposal_id="missing-proposal"))

    conflict_store = _new_store(fake_client_factory)
    current = _proposal(proposal_id="proposal-conflict")
    conflict_store.create_builder_proposal(SCOPE, current)
    container = _metadata_container(fake_client_factory)
    container.fail_replace_status = 412
    with pytest.raises(AgentStudioStoreError, match="was decided concurrently"):
        conflict_store.save_builder_proposal_decision(
            SCOPE,
            current.model_copy(update={"state": BuilderProposalState.APPLIED}),
        )

    container.fail_replace_status = 500
    with pytest.raises(CosmosHttpResponseError):
        conflict_store.save_builder_proposal_decision(
            SCOPE,
            current.model_copy(update={"state": BuilderProposalState.APPLIED}),
        )
    container.fail_replace_status = None


def test_evaluation_suites_create_list_get_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    suite = _evaluation_suite()
    other_agent = _evaluation_suite(suite_id="eval-suite-2", logical_agent_id=OTHER_AGENT_ID)
    assert first.create_evaluation_suite(SCOPE, suite) == suite
    assert first.create_evaluation_suite(SCOPE, other_agent) == other_agent

    reloaded = _new_store(fake_client_factory)
    assert reloaded.list_evaluation_suites(SCOPE, AGENT_ID) == (suite,)
    assert reloaded.list_evaluation_suites(SCOPE, AGENT_ID) == (suite,)
    assert reloaded.list_evaluation_suites(SCOPE, OTHER_AGENT_ID) == (other_agent,)

    getter = _new_store(fake_client_factory)
    assert getter.get_evaluation_suite(SCOPE, suite.id) == suite
    assert getter.get_evaluation_suite(SCOPE, suite.id) == suite
    assert getter.get_evaluation_suite(SCOPE, "missing-suite") is None
    assert getter.get_evaluation_suite(SAME_TENANT_OTHER_PROJECT_SCOPE, suite.id) is None
    assert reloaded.list_evaluation_suites(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert getter.get_evaluation_suite(OTHER_TENANT_SAME_PROJECT_SCOPE, suite.id) is None
    assert reloaded.list_evaluation_suites(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    container = _metadata_container(fake_client_factory)
    mismatched = _evaluation_suite(suite_id="eval-suite-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="evaluation_suite::eval-suite-mismatch",
        document_type="evaluation_suite",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_evaluation_suite(SCOPE, "eval-suite-mismatch") is None


def test_evaluation_runs_create_list_get_filter_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    run = _evaluation_run()
    other_suite_run = _evaluation_run(run_id="eval-run-2", suite_id="eval-suite-2")
    other_agent_run = _evaluation_run(
        run_id="eval-run-3", suite_id="eval-suite-3", logical_agent_id=OTHER_AGENT_ID
    )
    assert first.create_evaluation_run(SCOPE, run) == run
    assert first.create_evaluation_run(SCOPE, other_suite_run) == other_suite_run
    assert first.create_evaluation_run(SCOPE, other_agent_run) == other_agent_run

    reloaded = _new_store(fake_client_factory)
    assert set(reloaded.list_evaluation_runs(SCOPE, AGENT_ID)) == {run, other_suite_run}
    assert reloaded.list_evaluation_runs(SCOPE, AGENT_ID, suite_id=run.suite_id) == (run,)
    assert reloaded.list_evaluation_runs(SCOPE, OTHER_AGENT_ID) == (other_agent_run,)

    getter = _new_store(fake_client_factory)
    assert getter.get_evaluation_run(SCOPE, run.id) == run
    assert getter.get_evaluation_run(SCOPE, run.id) == run
    assert getter.get_evaluation_run(SCOPE, "missing-run") is None
    assert getter.get_evaluation_run(SAME_TENANT_OTHER_PROJECT_SCOPE, run.id) is None
    assert reloaded.list_evaluation_runs(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert getter.get_evaluation_run(OTHER_TENANT_SAME_PROJECT_SCOPE, run.id) is None
    assert reloaded.list_evaluation_runs(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    container = _metadata_container(fake_client_factory)
    mismatched = _evaluation_run(run_id="eval-run-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="evaluation_run::eval-run-mismatch",
        document_type="evaluation_run",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_evaluation_run(SCOPE, "eval-run-mismatch") is None


def test_test_runs_create_list_get_filter_and_scope_guards(
    fake_client_factory: FakeCosmosClientFactory,
) -> None:
    first = _new_store(fake_client_factory)
    draft_run = _test_run()
    version_run = _test_run(run_id="test-run-2", version_id="version-1")
    other_agent_run = _test_run(run_id="test-run-3", logical_agent_id=OTHER_AGENT_ID)
    assert first.create_test_run(SCOPE, draft_run) == draft_run
    assert first.create_test_run(SCOPE, version_run) == version_run
    assert first.create_test_run(SCOPE, other_agent_run) == other_agent_run

    reloaded = _new_store(fake_client_factory)
    assert set(reloaded.list_test_runs(SCOPE, AGENT_ID)) == {draft_run, version_run}
    assert reloaded.list_test_runs(SCOPE, AGENT_ID, version_id="version-1") == (version_run,)
    assert reloaded.list_test_runs(SCOPE, OTHER_AGENT_ID) == (other_agent_run,)

    getter = _new_store(fake_client_factory)
    assert getter.get_test_run(SCOPE, draft_run.id) == draft_run
    assert getter.get_test_run(SCOPE, draft_run.id) == draft_run
    assert getter.get_test_run(SCOPE, "missing-run") is None
    assert getter.get_test_run(SAME_TENANT_OTHER_PROJECT_SCOPE, draft_run.id) is None
    assert reloaded.list_test_runs(SAME_TENANT_OTHER_PROJECT_SCOPE, AGENT_ID) == ()
    assert getter.get_test_run(OTHER_TENANT_SAME_PROJECT_SCOPE, draft_run.id) is None
    assert reloaded.list_test_runs(OTHER_TENANT_SAME_PROJECT_SCOPE, AGENT_ID) == ()

    container = _metadata_container(fake_client_factory)
    mismatched = _test_run(run_id="test-run-mismatch", project_id=OTHER_PROJECT)
    container.inject_document(
        scope_key=SCOPE.scope_key,
        document_id="test_run::test-run-mismatch",
        document_type="test_run",
        payload=mismatched.model_dump(mode="json"),
    )
    assert _new_store(fake_client_factory).get_test_run(SCOPE, "test-run-mismatch") is None


def test_build_agent_studio_store_raises_without_endpoint() -> None:
    with pytest.raises(AgentStudioStoreError, match="metadata persistence is unavailable"):
        cosmos_store.build_agent_studio_store(Settings(cosmos_endpoint=None))


def test_build_agent_studio_store_uses_default_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    import azure.cosmos
    import azure.identity

    captured: dict[str, Any] = {}

    class CapturingClient:
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential
            self.database = FakeDatabase()
            captured["database"] = self.database

        def get_database_client(self, name: str) -> FakeDatabase:
            captured["database_name"] = name
            return self.database

    with monkeypatch.context() as patch:
        patch.setattr(azure.cosmos, "CosmosClient", CapturingClient)
        patch.setattr(azure.identity, "DefaultAzureCredential", lambda: "default-credential")
        reloaded = importlib.reload(cosmos_store)
        store = reloaded.build_agent_studio_store(
            Settings(
                cosmos_endpoint="https://cosmos.example.test",
                agent_studio_cosmos_database="custom-db",
                agent_studio_metadata_container="custom-container",
            )
        )

        assert isinstance(store, reloaded.CosmosAgentStudioStore)
        assert captured == {
            "endpoint": "https://cosmos.example.test",
            "credential": "default-credential",
            "database": captured["database"],
            "database_name": "custom-db",
        }
        assert cast(FakeDatabase, captured["database"]).requested_container_names == ["custom-container"]

    importlib.reload(cosmos_store)


def test_build_agent_studio_store_uses_managed_identity_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import azure.cosmos
    import azure.identity

    captured: dict[str, Any] = {}

    class CapturingClient:
        def __init__(self, endpoint: str, credential: Any) -> None:
            captured["endpoint"] = endpoint
            captured["credential"] = credential
            self.database = FakeDatabase()
            captured["database"] = self.database

        def get_database_client(self, name: str) -> FakeDatabase:
            captured["database_name"] = name
            return self.database

    with monkeypatch.context() as patch:
        patch.setattr(azure.cosmos, "CosmosClient", CapturingClient)
        patch.setattr(azure.identity, "ManagedIdentityCredential", lambda client_id: f"managed:{client_id}")
        reloaded = importlib.reload(cosmos_store)
        store = reloaded.build_agent_studio_store(
            Settings(
                cosmos_endpoint="https://cosmos.example.test",
                managed_identity_client_id="client-123",
                agent_studio_cosmos_database="agent-studio-db",
                agent_studio_metadata_container="agentStudioMetadataV1",
            )
        )

        assert isinstance(store, reloaded.CosmosAgentStudioStore)
        assert captured == {
            "endpoint": "https://cosmos.example.test",
            "credential": "managed:client-123",
            "database": captured["database"],
            "database_name": "agent-studio-db",
        }
        assert cast(FakeDatabase, captured["database"]).requested_container_names == ["agentStudioMetadataV1"]

    importlib.reload(cosmos_store)
