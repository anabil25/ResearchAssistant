from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, cast

import httpx
import pytest
from agent_framework import (
    AgentContext,
    AgentResponse,
    AgentResponseUpdate,
    Content,
    FunctionInvocationContext,
    Message,
    ResponseStream,
    WorkflowAgent,
)
from agent_framework_foundry_hosting import ResponsesHostServer  # type: ignore[import-untyped]
from openai import APIStatusError
from pydantic import ValidationError
from shared.approvals import (
    ApprovalConsumptionDisposition,
    ApprovalConsumptionRequest,
    ApprovalConsumptionResult,
    ApprovalGrant,
    ApprovalGrantState,
    ApprovalReceipt,
    InMemoryApprovalBackend,
    InMemoryApprovalConsumptionAdapter,
    approval_contract_schema_digest,
)
from shared.capabilities import (
    PROVIDER_CONTRACT_ARTIFACT_DIGEST,
    PROVIDER_CONTRACT_SCHEMA_DIGEST,
    PROVIDER_CONTRACT_VERSION,
    ApprovalMode,
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityExecutor,
    CapabilityPolicy,
    CapabilityRegistry,
    ConfigurationReference,
    ConnectionReference,
    DescriptorReference,
    DestinationConstraints,
    IdempotencyMode,
    InstanceReference,
    InvocationContext,
    OperationClass,
    OperationReference,
    PolicyReference,
    ProviderInstanceAttestation,
    RetryPolicy,
    ToolRegistration,
    attach_provider_binding,
    runtime_attested_registration,
)
from shared.catalog import capabilities_for_manifest
from shared.contracts import (
    AUXILIARY_CONTRACTS,
    INPUT_CONTRACTS,
    OUTPUT_CONTRACTS,
    SCHEMA_REFERENCES,
    AgentManifest,
    Claim,
    CoordinatorRequest,
    CoordinatorResponse,
    DatasetRequest,
    EvidenceRef,
    GrantResponse,
    LiteratureRequest,
    LiteratureResponse,
    MemoryPolicy,
    MemoryScope,
    MemoryScopePolicy,
    ObjectiveGate,
    PublicGrantRequest,
    PublicLiteratureRequest,
    PublicMatchingRequest,
    ResearchResponse,
    RuntimeRequirements,
    Sensitivity,
    SpecialistCapability,
    SpecialistRequest,
    SpecialistResult,
    SupportStatus,
    bind_contracts,
    canonical_digest,
    resolve_authorized_evidence,
)
from shared.errors import (
    ApprovalAlreadyConsumedError,
    ApprovalConsumptionUncertainError,
    ApprovalDeniedError,
    ApprovalExpiredError,
    ApprovalMismatchError,
    ApprovalRequiredError,
    ApprovalResultInvalidError,
    ApprovalRevokedError,
    ApprovalStoreUnavailableError,
    AuthorizationError,
    CapabilityNotFoundError,
    ConfigurationError,
    ContractError,
    DeadlineExceededError,
    DestinationDeniedError,
    HarnessError,
    IdempotencyConcurrencyError,
    IdempotencyInProgressError,
    IdempotencyReconciliationRequiredError,
    IdempotencyReplayDeniedError,
    IdempotencyRequiredError,
    IdempotencyResultMismatchError,
    IdempotencyStoreUnavailableError,
    InvocationError,
    IsolationError,
    ReleaseAttestationError,
    RetryableInvocationError,
    StaleCapabilityBindingError,
    error_from_exception,
    error_response,
)
from shared.factory import GovernedAgentFactory, get_factory
from shared.idempotency import (
    ClaimDisposition,
    CompletedReplayMode,
    IdempotencyApprovalProvenance,
    IdempotencyClaim,
    IdempotencyKey,
    IdempotencyPolicy,
    IdempotencyRecord,
    IdempotencyState,
    InMemoryIdempotencyBackend,
    InMemoryIdempotencyStore,
    canonical_idempotency_digest,
    idempotency_contract_schema_digest,
)
from shared.invocation import HostedAgentReply, HostedInvocationPolicy, RetryingResponsesInvoker
from shared.local_harness import LocalHarness, LocalInvocation
from shared.middleware import (
    ContractMiddleware,
    GovernedFunctionMiddleware,
    middleware_for_manifest,
)
from shared.profiles import get_manifest, get_profile, list_manifests
from shared.release import (
    InMemoryReleaseAttestor,
    ReleaseAttestationStatus,
    build_release_metadata,
    manifest_digest,
    release_attestation_contract_schema_digest,
    source_bundle_digest,
    validate_release_attestation,
)
from shared.settings import HarnessSettings
from shared.state import (
    ConversationRecord,
    InMemoryConversationStore,
    InMemoryLongTermMemory,
    MemoryRecord,
    from_agent_session,
    to_agent_session,
)
from shared.telemetry import (
    GovernanceAuditEvent,
    OpenTelemetryGovernanceAuditSink,
    redact_attributes,
    telemetry_identity_digest,
)
from shared.tools import _invoke_specialist, delegated_agent_name, tools_for_profile
from shared.workflows import (
    CoordinatorRouter,
    FoundrySpecialistInvoker,
    _specialist_manifest,
    _specialist_payload,
    build_coordinator_workflow,
    specialist_handler_resolver,
)


def _request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": "Compare supplied evidence",
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "principal_id": "principal-a",
        "session_id": "session-a",
        "sensitivity": "internal",
        "evidence": [],
    }
    payload.update(overrides)
    return payload


def _evidence_response(summary: str = "Supported result") -> LiteratureResponse:
    evidence = EvidenceRef(evidence_id="ev-1", source_uri="https://example.test/source")
    return LiteratureResponse(
        summary=summary,
        claims=(
            Claim(
                text="A supported claim",
                support=SupportStatus.SUPPORTED,
                evidence_ids=("ev-1",),
            ),
        ),
        evidence=(evidence,),
    )


def _settings(**overrides: Any) -> HarnessSettings:
    values: dict[str, Any] = {
        "foundry_project_endpoint": "https://example.services.ai.azure.com/api/projects/p",
        "model_deployment_name": "gpt-5.4-mini",
        "model_deployment_version": "2026-03-17",
    }
    values.update(overrides)
    return HarnessSettings.model_validate(values)


class _DurableTestReleaseAttestor(InMemoryReleaseAttestor):
    is_durable = True


def _release_attestor(manifest: AgentManifest) -> _DurableTestReleaseAttestor:
    return _DurableTestReleaseAttestor(manifest.evaluation.objective_hard_gates)


class _TrustedScope(TypedDict, total=False):
    trusted_tenant_id: str
    trusted_project_id: str


def _trusted_scope(manifest: AgentManifest) -> _TrustedScope:
    tenant_ids = {binding.tenant_scope for binding in manifest.capability_bindings}
    project_ids = {binding.project_scope for binding in manifest.capability_bindings}
    if len(tenant_ids) != 1 or len(project_ids) != 1:
        raise ValueError("Test manifests must have one capability tenant and project scope")
    return {
        "trusted_tenant_id": tenant_ids.pop(),
        "trusted_project_id": project_ids.pop(),
    }


def _binding(capability_id: str) -> CapabilityBinding:
    destinations = ("app://tests/local",)
    operation_ref = OperationReference(
        id=f"local.{capability_id}",
        version="1.0.0",
        input_schema_digest=SCHEMA_REFERENCES["LiteratureRequestV2"].sha256,
        output_schema_digest=SCHEMA_REFERENCES["LiteratureSynthesisV2"].sha256,
    )
    configuration = {"mode": "test"}
    connection_scopes = ("https://ai.azure.com/.default",)
    return CapabilityBinding(
        binding_id=f"{capability_id}.local",
        provider_contract_version=PROVIDER_CONTRACT_VERSION,
        provider_contract_schema_digest=PROVIDER_CONTRACT_SCHEMA_DIGEST,
        descriptor_ref=DescriptorReference(
            id=capability_id,
            version="1.0.0",
            digest=canonical_digest({"id": capability_id, "version": "1.0.0"}),
        ),
        operations_digest=canonical_digest((operation_ref.model_dump(mode="json"),)),
        operation_ref=operation_ref,
        instance_ref=InstanceReference(
            provider_id="test-provider",
            instance_id=f"test:{capability_id}",
            provider_resource_id=f"app://tests/providers/{capability_id}",
            discovered_provider_version=PROVIDER_CONTRACT_VERSION,
            discovered_resource_version="1.0.0",
            fingerprint="1" * 64,
        ),
        configuration_ref=ConfigurationReference(
            id="app://config/tests",
            canonical_json=json.dumps(configuration, sort_keys=True, separators=(",", ":")),
            digest=canonical_digest(configuration),
        ),
        connection_ref=ConnectionReference(
            id="app://connections/tests",
            auth_mode="managed_identity",
            scopes=connection_scopes,
            authorization_digest=canonical_digest(
                {
                    "id": "app://connections/tests",
                    "auth_mode": "managed_identity",
                    "scopes": connection_scopes,
                }
            ),
        ),
        policy_ref=PolicyReference(
            id="app://policy/tests",
            version="1.0.0",
            digest="3" * 64,
        ),
        allowed_destinations=DestinationConstraints(
            constraints=destinations,
            digest=canonical_digest(destinations),
        ),
        tenant_scope="tenant-a",
        project_scope="project-a",
    )


class _ManifestProviderAdapter:
    def __init__(self, bindings: tuple[CapabilityBinding, ...]) -> None:
        versions = {binding.provider_contract_version for binding in bindings}
        if len(versions) != 1:
            raise ValueError("Test adapter requires one provider contract version")
        self.contract_version = versions.pop()
        self.contract_schema_digest = PROVIDER_CONTRACT_SCHEMA_DIGEST
        self.attestations = {
            (
                binding.instance_ref.provider_id,
                binding.instance_ref.instance_id,
            ): ProviderInstanceAttestation(
                binding_id=binding.binding_id,
                provider_contract_version=binding.provider_contract_version,
                provider_contract_schema_digest=binding.provider_contract_schema_digest,
                descriptor_ref=binding.descriptor_ref,
                operations_digest=binding.operations_digest,
                operation_ref=binding.operation_ref,
                instance_ref=binding.instance_ref,
                configuration_ref=binding.configuration_ref,
                connection_ref=binding.connection_ref,
                policy_ref=binding.policy_ref,
                allowed_destinations=binding.allowed_destinations,
                tenant_id=binding.tenant_scope,
                project_id=binding.project_scope,
                readiness="READY",
                health="READY",
                auth_ready=True,
                configuration_validated=True,
                maturity="GA",
                lifecycle="ACTIVE",
                approval_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
            )
            for binding in bindings
        }
        models = {
            **INPUT_CONTRACTS,
            **OUTPUT_CONTRACTS,
            **AUXILIARY_CONTRACTS,
        }
        self.schemas = {
            canonical_digest(model.model_json_schema()): model.model_json_schema() for model in models.values()
        }
        self.handler_resolutions = 0
        self.handler_calls = 0

    def discover_instance(
        self,
        provider_id: str,
        instance_id: str,
    ) -> ProviderInstanceAttestation:
        return self.attestations[(provider_id, instance_id)]

    def load_schema(self, schema_digest: str) -> dict[str, Any]:
        return self.schemas[schema_digest]

    def resolve_handler(self, _attestation: ProviderInstanceAttestation) -> Any:
        self.handler_resolutions += 1

        async def invoke(payload: dict[str, Any]) -> dict[str, Any]:
            self.handler_calls += 1
            return await GovernedFunctionMiddleware._invoke_framework_function(payload)

        return invoke


def _runtime_registration(
    binding: CapabilityBinding,
) -> tuple[ToolRegistration, _ManifestProviderAdapter]:
    adapter = _ManifestProviderAdapter((binding,))
    return (
        runtime_attested_registration(
            binding,
            adapter,
            tenant_id=binding.tenant_scope,
            project_id=binding.project_scope,
            clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
        ),
        adapter,
    )


def _coordinator_registration(invoker: Any) -> ToolRegistration:
    manifest = get_manifest("coordinator")
    return (
        GovernedAgentFactory(manifest)
        .prepare(
            _settings(),
            provider_adapter=_ManifestProviderAdapter(manifest.capability_bindings),
            handler_resolver=specialist_handler_resolver(invoker),
            **_trusted_scope(manifest),
        )
        .registrations[0]
    )


def _register(
    registry: CapabilityRegistry,
    descriptor: CapabilityDescriptor,
    handler: Any,
) -> None:
    registry.add_descriptor(descriptor)
    registry.register_tool(
        ToolRegistration(
            binding=_binding(descriptor.id),
            tool_name=descriptor.id.rsplit(".", 1)[-1],
            handler=handler,
            current_instance_fingerprint="1" * 64,
        )
    )


def test_contracts_reject_false_support_and_unresolved_citations() -> None:
    with pytest.raises(ValidationError, match="supported and conflicting claims require"):
        Claim.model_validate({"text": "claim", "support": "supported"})
    with pytest.raises(ValidationError, match="supported and conflicting claims require"):
        Claim.model_validate({"text": "claim", "support": "conflicting"})
    with pytest.raises(ValidationError, match="unsupported claims cannot"):
        Claim.model_validate({"text": "claim", "support": "unsupported", "evidence_ids": ("ev-1",)})
    normalized = ResearchResponse(
        summary="result",
        claims=(
            Claim(
                text="claim",
                support=SupportStatus.SUPPORTED,
                evidence_ids=("missing",),
            ),
        ),
    )
    assert normalized.claims[0].support == SupportStatus.UNSUPPORTED
    assert normalized.claims[0].evidence_ids == ()
    fabricated = LiteratureResponse(
        summary="fabricated",
        claims=(
            Claim(
                text="claim",
                support=SupportStatus.SUPPORTED,
                evidence_ids=("fabricated",),
            ),
        ),
        evidence=(
            EvidenceRef(
                evidence_id="fabricated",
                source_uri="https://attacker.example",
            ),
        ),
    )
    resolved = resolve_authorized_evidence(fabricated, ())
    assert resolved.claims[0].support == SupportStatus.UNSUPPORTED
    assert resolved.evidence == ()
    invalid_conflict = Claim.model_construct(
        text="unresolved conflict",
        support=SupportStatus.CONFLICTING,
        evidence_ids=(),
    )
    reconciled_conflict = resolve_authorized_evidence(
        LiteratureResponse.model_construct(
            summary="conflict",
            claims=(invalid_conflict,),
            limitations=(),
            evidence=(),
            consensus=(),
            disagreements=(),
        ),
        (),
    )
    assert reconciled_conflict.claims[0].support == SupportStatus.UNSUPPORTED
    canonical = EvidenceRef(
        evidence_id="ev-1",
        source_uri="app://authorized/source",
    )
    spoofed = fabricated.model_copy(
        update={
            "claims": (
                Claim(
                    text="claim",
                    support=SupportStatus.SUPPORTED,
                    evidence_ids=("ev-1",),
                ),
            ),
            "evidence": (
                EvidenceRef(
                    evidence_id="ev-1",
                    source_uri="https://attacker.example",
                ),
            ),
        }
    )
    canonicalized = resolve_authorized_evidence(spoofed, (canonical,))
    assert canonicalized.claims[0].support == SupportStatus.SUPPORTED
    assert canonicalized.evidence == (canonical,)
    coordinator = CoordinatorResponse(
        summary="coordinated",
        specialist_results=(
            SpecialistResult(
                request_id="nested",
                capability=SpecialistCapability.LITERATURE,
                agent_name="literature-agent",
                response=fabricated,
            ),
        ),
    )
    resolved_coordinator = cast(
        CoordinatorResponse,
        resolve_authorized_evidence(coordinator, ()),
    )
    nested = resolved_coordinator.specialist_results[0].response
    assert nested is not None
    assert nested.claims[0].support == SupportStatus.UNSUPPORTED


def test_public_and_specialist_contracts_are_strict() -> None:
    request = PublicLiteratureRequest.model_validate(_request(sensitivity="public"))
    assert request.sensitivity == Sensitivity.PUBLIC
    with pytest.raises(ValidationError):
        PublicLiteratureRequest.model_validate(_request(sensitivity="internal"))
    for public_contract in (
        PublicLiteratureRequest,
        PublicGrantRequest,
        PublicMatchingRequest,
    ):
        with pytest.raises(ValidationError, match="caller-supplied evidence"):
            public_contract.model_validate(
                _request(
                    sensitivity="public",
                    evidence=[{"evidence_id": "internal", "title": "private"}],
                )
            )
    with pytest.raises(ValidationError, match="exactly one"):
        SpecialistResult.model_validate(
            {
                "request_id": "r",
                "capability": "literature",
                "agent_name": "literature-agent",
            }
        )
    with pytest.raises(ValidationError, match="exactly one"):
        SpecialistResult.model_validate(
            {
                "request_id": "r",
                "capability": "literature",
                "agent_name": "literature-agent",
                "response": _evidence_response(),
                "error_code": "failed",
            }
        )
    result = SpecialistResult(
        request_id="r",
        capability=SpecialistCapability.LITERATURE,
        agent_name="literature-agent",
        response=_evidence_response().model_copy(update={"consensus": ("preserved",)}),
    )
    assert result.response is not None
    assert result.model_dump()["response"]["consensus"] == ("preserved",)
    grant_result = SpecialistResult.model_validate(
        {
            "request_id": "grant",
            "capability": "grant",
            "agent_name": "grant-agent",
            "response": {"summary": "grant response"},
        }
    )
    assert type(grant_result.response) is GrantResponse
    parse_specialist_result = cast(
        Any,
        SpecialistResult.parse_pinned_response_contract,
    )
    assert parse_specialist_result("invalid") == "invalid"
    missing_result_identity = {"response": {"summary": "missing identity"}}
    assert parse_specialist_result(missing_result_identity) == missing_result_identity
    with pytest.raises(ValidationError, match="pinned capability"):
        SpecialistResult.model_validate(
            {
                "request_id": "wrong-agent",
                "capability": "grant",
                "agent_name": "literature-agent",
                "error_code": "failed",
            }
        )
    with pytest.raises(ValidationError, match="pinned output contract"):
        SpecialistResult(
            request_id="wrong-response",
            capability=SpecialistCapability.GRANT,
            agent_name="grant-agent",
            response=_evidence_response(),
        )
    invalid_constructed_result = SpecialistResult.model_construct(
        request_id="invalid",
        capability=SpecialistCapability.GRANT,
        agent_name="unknown-agent",
        error_code="failed",
    )
    with pytest.raises(ValueError, match="pinned capability"):
        cast(Any, invalid_constructed_result.exactly_one_result)()
    dataset_request = DatasetRequest.model_validate(
        _request(
            dataset_id="dataset.csv",
            approved_compute=False,
            approval_decision_id="approval-a",
            invocation_id="invocation-a",
            idempotency_key="stable-key",
        )
    )
    specialist_request = SpecialistRequest(
        request_id="dataset-request",
        capability=SpecialistCapability.DATASET,
        request=dataset_request,
        target_agent="dataset-agent",
    )
    assert specialist_request.model_dump()["request"]["dataset_id"] == "dataset.csv"
    with pytest.raises(ValidationError, match="does not match"):
        SpecialistRequest(
            request_id="mismatch",
            capability=SpecialistCapability.GRANT,
            request=dataset_request,
            target_agent="grant-agent",
        )
    with pytest.raises(ValidationError, match="Input should be False"):
        DatasetRequest.model_validate(_request(dataset_id="dataset.csv", approved_compute=True))
    with pytest.raises(ValidationError, match="supplied together"):
        DatasetRequest.model_validate(
            _request(dataset_id="dataset.csv", approval_decision_id="approval-a")
        )
    with pytest.raises(ValidationError, match="caller-supplied evidence"):
        SpecialistRequest.model_validate(
            {
                "request_id": "public-evidence",
                "capability": "literature",
                "request": _request(
                    sensitivity="public",
                    evidence=[{"evidence_id": "private"}],
                ),
                "target_agent": "literature-online-agent",
            }
        )
    parsed_public = SpecialistRequest.model_validate(
        {
            "request_id": "public",
            "capability": "literature",
            "request": _request(sensitivity="public"),
            "target_agent": "literature-online-agent",
        }
    )
    assert isinstance(parsed_public.request, PublicLiteratureRequest)
    parse_specialist_request = cast(
        Any,
        SpecialistRequest.parse_pinned_request_contract,
    )
    assert parse_specialist_request("invalid") == "invalid"
    assert parse_specialist_request({"capability": "literature", "request": "invalid"}) == {
        "capability": "literature",
        "request": "invalid",
    }
    invalid_discriminator = {
        "capability": "unknown",
        "request": _request(),
    }
    assert parse_specialist_request(invalid_discriminator) == invalid_discriminator
    missing_discriminator = {"request": _request()}
    assert parse_specialist_request(missing_discriminator) == missing_discriminator


def test_manifest_validation_and_contract_resolution() -> None:
    manifest = get_manifest("literature")
    contracts = bind_contracts(manifest)
    assert contracts.input_model is LiteratureRequest
    assert contracts.output_model is LiteratureResponse
    assert manifest.enable_web_search is False
    assert manifest.version == manifest.behavior_version
    assert manifest.model_tier == manifest.model_policy.performance_class
    assert manifest.evidence_kinds == manifest.evidence_policy.allowed_evidence_kinds
    assert manifest.connector_sources == ()
    assert get_profile("literature") is manifest
    assert len(list_manifests()) == 9
    with pytest.raises(ValueError, match="Unknown"):
        get_manifest("missing")
    persisted = manifest.model_dump(mode="json")
    assert "input_model" not in persisted
    assert persisted["input_schema"]["sha256"]
    assert AgentManifest.model_validate_json(manifest.model_dump_json()) == manifest
    payload = manifest.model_dump()
    payload["input_schema"] = {
        "schema_id": "MissingInput",
        "uri": "urn:research-assistant:schema:MissingInput",
        "sha256": "0" * 64,
    }
    unbound = AgentManifest.model_validate(payload)
    with pytest.raises(ContractError, match="unknown contract"):
        bind_contracts(unbound)
    payload = manifest.model_dump()
    payload["output_schema"] = {
        **payload["output_schema"],
        "sha256": "0" * 64,
    }
    payload["artifact_policy"]["output_schema"] = payload["output_schema"]
    payload["evidence_policy"]["output_schema"] = payload["output_schema"]
    with pytest.raises(ContractError, match="digest"):
        bind_contracts(AgentManifest.model_validate(payload))
    payload = get_manifest("literature_online").model_dump()
    payload["instructions"] = "No boundary stated."
    with pytest.raises(ValidationError, match="public-data boundary"):
        AgentManifest.model_validate(payload)
    payload = get_manifest("dataset").model_dump()
    payload["capability_bindings"] = (
        payload["capability_bindings"][0],
        payload["capability_bindings"][0],
    )
    with pytest.raises(ValidationError, match="unique"):
        AgentManifest.model_validate(payload)
    assert (
        RuntimeRequirements(
            custom_middleware=False,
            custom_code=False,
        ).selected_runtime
        == "managed"
    )
    with pytest.raises(ValidationError, match="specialist policy"):
        AgentManifest.model_validate(
            {
                **get_manifest("coordinator").model_dump(),
                "id": "dataset",
            }
        )
    for policy_name in ("artifact_policy", "evidence_policy"):
        payload = manifest.model_dump()
        payload[policy_name]["output_schema"] = SCHEMA_REFERENCES["DatasetAnalysisV2"]
        with pytest.raises(ValidationError, match="output schemas"):
            AgentManifest.model_validate(payload)
    specialist_policy = get_manifest("coordinator").specialist_policy
    assert specialist_policy is not None
    with pytest.raises(ValidationError, match="specialist capability"):
        type(specialist_policy).model_validate(
            {
                **specialist_policy.model_dump(),
                "specialists": (
                    specialist_policy.specialists[0],
                    specialist_policy.specialists[0],
                ),
            }
        )
    with pytest.raises(ValidationError, match="unique"):
        CoordinatorRequest.model_validate(_request(requested_capabilities=["literature", "literature"]))
    with pytest.raises(ValidationError, match="target a requested"):
        CoordinatorRequest.model_validate(
            _request(
                requested_capabilities=["literature"],
                specialist_inputs={"grant": {"opportunity_id": "grant-1"}},
            )
        )


def test_memory_scopes_and_objective_release_gates_are_explicit() -> None:
    policy = MemoryPolicy()
    assert policy.for_scope(MemoryScope.CONVERSATION).enabled is True
    assert policy.for_scope(MemoryScope.USER).persistent is False
    with pytest.raises(ValidationError, match="application-owned"):
        MemoryScopePolicy(
            scope=MemoryScope.PROJECT,
            enabled=True,
            persistent=True,
        )
    persistent = MemoryScopePolicy(
        scope=MemoryScope.PROJECT,
        enabled=True,
        persistent=True,
        provider_ref="app://memory/project",
        retention_days=365,
        ttl_seconds=31_536_000,
        read_roles=("researchers",),
        write_roles=("research-admins",),
    )
    assert persistent.provider_ref == "app://memory/project"
    assert persistent.user_can_forget is True
    with pytest.raises(ValidationError, match="retention"):
        MemoryScopePolicy(
            scope=MemoryScope.PROJECT,
            enabled=True,
            persistent=True,
            provider_ref="app://memory/project",
        )
    with pytest.raises(ValidationError, match="non-persistent"):
        MemoryScopePolicy(
            scope=MemoryScope.USER,
            retention_days=30,
        )
    with pytest.raises(ValidationError, match="unique"):
        MemoryPolicy(scopes=(*policy.scopes[:-1], policy.scopes[0]))
    with pytest.raises(ValidationError, match="every supported"):
        MemoryPolicy(scopes=policy.scopes[:-1])
    evaluation = get_manifest("literature").evaluation
    assert evaluation.evaluator_results_advisory is True
    assert set(evaluation.objective_hard_gates) == set(ObjectiveGate)
    with pytest.raises(ValidationError, match="objective"):
        type(evaluation).model_validate(
            {
                **evaluation.model_dump(),
                "objective_hard_gates": (ObjectiveGate.TEST,),
            }
        )


def test_settings_validate_environment_and_readiness() -> None:
    settings = HarnessSettings.from_environment(
        {
            "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/p",
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "model",
            "AZURE_AI_MODEL_DEPLOYMENT_VERSION": "2026-01-01",
            "AZURE_CLIENT_ID": "client",
            "TOOLBOX_ENDPOINT": "https://example.services.ai.azure.com/toolboxes/t/mcp",
            "AGENT_DEFAULT_TIMEOUT_SECONDS": "30",
            "AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED": "TRUE",
            "AZURE_ENV_NAME": "test",
        }
    )
    assert settings.telemetry_content_recording is True
    assert settings.model_deployment_version == "2026-01-01"
    assert settings.readiness() == {
        "ready": True,
        "environment": "test",
        "managed_identity": True,
        "toolbox_configured": True,
    }
    assert _settings().readiness()["managed_identity"] is False
    with pytest.raises(ConfigurationError):
        HarnessSettings.from_environment({})
    with pytest.raises(ConfigurationError):
        HarnessSettings.from_environment(
            {
                "FOUNDRY_PROJECT_ENDPOINT": "https://example.test",
                "AZURE_AI_MODEL_DEPLOYMENT_NAME": "model",
                "AGENT_DEFAULT_TIMEOUT_SECONDS": "not-a-number",
            }
        )


def test_structured_errors_never_leak_unknown_exception_messages() -> None:
    error = HarnessError("safe", context={"agent": "test"})
    assert error.detail().message == "safe"
    assert error_response(error)["error"]["code"] == "harness_error"
    unknown = error_from_exception(ValueError("sensitive value"))
    assert unknown.message == "ValueError"
    assert unknown.code == "internal_error"


def test_retry_and_write_capability_contract_validation() -> None:
    with pytest.raises(ValidationError, match="cover every retry"):
        RetryPolicy(max_attempts=2)
    with pytest.raises(ValidationError, match="between 0 and 60"):
        RetryPolicy(max_attempts=2, delays_seconds=(61,))
    with pytest.raises(ValidationError, match="approval"):
        CapabilityDescriptor.model_validate(
            {
                "id": "write.action",
                "operation": "write_irreversible",
                "idempotency": "required",
                "side_effect_destinations": ["approved.example"],
            }
        )
    with pytest.raises(ValidationError, match="idempotency"):
        CapabilityDescriptor.model_validate(
            {
                "id": "write.action",
                "operation": "write_irreversible",
                "approval": "required",
                "side_effect_destinations": ["approved.example"],
            }
        )
    with pytest.raises(ValidationError, match="explicit destinations"):
        CapabilityDescriptor.model_validate(
            {
                "id": "write.action",
                "operation": "privileged",
                "approval": "required",
                "idempotency": "required",
            }
        )
    binding = _binding("read.action")
    legacy_fields: dict[str, Any] = {
        "descriptor_id": "read.action",
        "operation_id": "local.read.action",
        "instance_fingerprint": "a" * 64,
        "provider_version": "ambiguous",
        "pinned_provider_version": "1.0.0",
        "config": {"limit": 1},
        "config_ref": "app://config/read-action",
    }
    for field, value in legacy_fields.items():
        with pytest.raises(ValidationError, match="Extra inputs"):
            CapabilityBinding.model_validate(
                {
                    **binding.model_dump(),
                    field: value,
                }
            )
    with pytest.raises(ValidationError, match="destination digest"):
        DestinationConstraints(
            constraints=("app://allowed",),
            digest="0" * 64,
        )
    with pytest.raises(ValidationError, match="canonical JSON"):
        ConfigurationReference(
            canonical_json="{]",
            digest=canonical_digest({}),
        )
    with pytest.raises(ValidationError, match="not canonical"):
        ConfigurationReference(
            canonical_json='{"mode": "test" }',
            digest=canonical_digest({"mode": "test"}),
        )
    with pytest.raises(ValidationError, match="digest does not match"):
        ConfigurationReference(
            canonical_json="{}",
            digest="0" * 64,
        )
    connection = binding.connection_ref.model_dump()
    with pytest.raises(ValidationError, match="sorted, and unique"):
        ConnectionReference.model_validate(
            {**connection, "scopes": ("scope-b", "scope-a")}
        )
    with pytest.raises(ValidationError, match="sorted, and unique"):
        ConnectionReference.model_validate({**connection, "scopes": ("",)})
    with pytest.raises(ValidationError, match="authorization digest"):
        ConnectionReference.model_validate(
            {**connection, "authorization_digest": "0" * 64}
        )
    with pytest.raises(ValidationError, match="canonical provider v6"):
        CapabilityBinding.model_validate(
            {
                **binding.model_dump(),
                "provider_contract_version": "research-assistant.integration-provider.v3",
            }
        )
    with pytest.raises(ValidationError, match="schema digest does not match"):
        CapabilityBinding.model_validate(
            {
                **binding.model_dump(),
                "provider_contract_schema_digest": "0" * 64,
            }
        )


def test_policy_enforces_scope_destination_approval_and_idempotency() -> None:
    capability = CapabilityDescriptor(
        id="write.action",
        operation=OperationClass.WRITE_IRREVERSIBLE,
        required_scopes=frozenset({"write"}),
        allowed_destinations=("approved.example",),
        side_effect_destinations=("approved.example",),
        approval=ApprovalMode.REQUIRED,
        idempotency=IdempotencyMode.REQUIRED,
    )
    policy = CapabilityPolicy()
    base = {
        "tenant_id": "tenant",
        "project_id": "project-a",
        "principal_id": "principal",
        "scopes": frozenset({"write"}),
        "destination": "approved.example",
        "approval_decision_id": "approval-a",
        "invocation_id": "invocation-a",
        "idempotency_key": "key",
        "operation_fingerprint": "a" * 64,
    }
    policy.authorize(capability, InvocationContext.model_validate(base))
    with pytest.raises(AuthorizationError):
        policy.authorize(
            capability,
            InvocationContext.model_validate({**base, "scopes": frozenset()}),
        )
    with pytest.raises(DestinationDeniedError):
        policy.authorize(
            capability,
            InvocationContext.model_validate({**base, "destination": "evil.example"}),
        )
    with pytest.raises(ApprovalRequiredError):
        policy.authorize(
            capability,
            InvocationContext.model_validate({**base, "approval_decision_id": None}),
        )
    with pytest.raises(IdempotencyRequiredError):
        policy.authorize(
            capability,
            InvocationContext.model_validate({**base, "idempotency_key": None}),
        )
    with pytest.raises(IdempotencyRequiredError):
        policy.authorize(
            capability,
            InvocationContext.model_validate({**base, "operation_fingerprint": None}),
        )
    side_effect_only = capability.model_copy(update={"allowed_destinations": ()})
    with pytest.raises(DestinationDeniedError, match="side-effect"):
        policy.authorize(
            side_effect_only,
            InvocationContext.model_validate({**base, "destination": "evil.example"}),
        )
    always = CapabilityDescriptor(
        id="read.approved",
        operation=OperationClass.READ,
        approval=ApprovalMode.ALWAYS,
    )
    with pytest.raises(ApprovalRequiredError):
        policy.authorize(
            always,
            InvocationContext(
                tenant_id="tenant",
                project_id="project-a",
                principal_id="principal",
            ),
        )


def test_registry_is_explicit_and_duplicate_safe() -> None:
    registry = CapabilityRegistry()
    capability = CapabilityDescriptor(
        id="read.action",
        operation=OperationClass.READ,
    )
    _register(registry, capability, lambda payload: payload)
    assert registry.resolve("read.action")[0] is capability
    assert registry.resolve_operation("action")[0] is capability
    assert registry.definitions() == (capability,)
    with pytest.raises(ValueError, match="already registered"):
        registry.add_descriptor(capability)
    with pytest.raises(ValueError, match="operation already"):
        registry.register_tool(
            ToolRegistration(
                binding=_binding("read.action"),
                tool_name="action",
                handler=lambda payload: payload,
                current_instance_fingerprint="1" * 64,
            )
        )
    second_binding = _binding("read.action")
    registry.register_tool(
        ToolRegistration(
            binding=second_binding.model_copy(
                update={"operation_ref": second_binding.operation_ref.model_copy(update={"id": "local.secondAction"})}
            ),
            tool_name="secondAction",
            handler=lambda payload: payload,
            current_instance_fingerprint="1" * 64,
        )
    )
    assert registry.resolve_operation("secondAction")[0] is capability
    with pytest.raises(CapabilityNotFoundError, match="multiple operations"):
        registry.resolve("read.action")
    missing = CapabilityRegistry()
    with pytest.raises(CapabilityNotFoundError, match="unknown capability"):
        missing.register_tool(
            ToolRegistration(
                binding=_binding("missing"),
                tool_name="missing",
                handler=lambda payload: payload,
                current_instance_fingerprint="1" * 64,
            )
        )
    with pytest.raises(CapabilityNotFoundError):
        registry.resolve("missing")
    with pytest.raises(CapabilityNotFoundError, match="operation"):
        registry.resolve_operation("missing")
    other = CapabilityDescriptor(id="read.other", operation=OperationClass.READ)
    registry.add_descriptor(other)
    with pytest.raises(ValueError, match="operation already"):
        registry.register_tool(
            ToolRegistration(
                binding=_binding(other.id),
                tool_name="action",
                handler=lambda payload: payload,
                current_instance_fingerprint="1" * 64,
            )
        )
    stale = CapabilityRegistry()
    stale.add_descriptor(other)
    stale.register_tool(
        ToolRegistration(
            binding=_binding(other.id),
            tool_name="other",
            handler=lambda payload: payload,
            current_instance_fingerprint="2" * 64,
        )
    )
    with pytest.raises(StaleCapabilityBindingError) as captured:
        stale.resolve(other.id)
    assert captured.value.retryable is False
    assert captured.value.context == {
        "capability": other.id,
        "instance_ref": "test:read.other",
        "expected_fingerprint": "1" * 64,
        "current_fingerprint": "2" * 64,
    }
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        ToolRegistration(
            binding=_binding(other.id),
            tool_name="invalid",
            handler=lambda payload: payload,
            current_instance_fingerprint="A" * 64,
        )


def test_provider_adapter_attests_every_pin_before_handler_resolution() -> None:
    initial_binding = _binding("read.action")
    binding = initial_binding.model_copy(
        update={
            "operation_ref": initial_binding.operation_ref.model_copy(update={"id": "provider.read"}),
            "instance_ref": initial_binding.instance_ref.model_copy(update={"fingerprint": "3" * 64}),
        },
    )
    valid = ProviderInstanceAttestation(
        binding_id=binding.binding_id,
        provider_contract_version=binding.provider_contract_version,
        provider_contract_schema_digest=binding.provider_contract_schema_digest,
        descriptor_ref=binding.descriptor_ref,
        operations_digest=binding.operations_digest,
        operation_ref=binding.operation_ref,
        instance_ref=binding.instance_ref,
        configuration_ref=binding.configuration_ref,
        connection_ref=binding.connection_ref,
        policy_ref=binding.policy_ref,
        allowed_destinations=binding.allowed_destinations,
        tenant_id="tenant-a",
        project_id="project-a",
        readiness="READY",
        health="READY",
        auth_ready=True,
        configuration_validated=True,
        maturity="GA",
        lifecycle="ACTIVE",
        approval_expires_at=datetime(2026, 7, 24, tzinfo=UTC),
    )

    class Adapter:
        contract_version = binding.provider_contract_version
        contract_schema_digest = binding.provider_contract_schema_digest

        def __init__(self, attestation: ProviderInstanceAttestation) -> None:
            self.attestation = attestation
            self.handler_resolutions = 0

        def discover_instance(
            self,
            provider_id: str,
            instance_id: str,
        ) -> ProviderInstanceAttestation:
            assert provider_id == binding.instance_ref.provider_id
            assert instance_id == binding.instance_ref.instance_id
            return self.attestation

        def resolve_handler(self, _attestation: ProviderInstanceAttestation) -> Any:
            self.handler_resolutions += 1
            return lambda payload: payload

        def load_schema(self, schema_digest: str) -> dict[str, Any]:
            schemas = {
                binding.operation_ref.input_schema_digest: LiteratureRequest.model_json_schema(),
                binding.operation_ref.output_schema_digest: LiteratureResponse.model_json_schema(),
            }
            return schemas[schema_digest]

    adapter = Adapter(valid)
    registration = attach_provider_binding(
        binding,
        adapter,
        tenant_id="tenant-a",
        project_id="project-a",
        now=datetime(2026, 7, 23, tzinfo=UTC),
    )
    assert registration.current_instance_fingerprint == binding.instance_ref.fingerprint
    assert adapter.handler_resolutions == 1

    legacy_binding = binding.model_copy(
        update={
            "provider_contract_version": "research-assistant.integration-provider.v3",
            "provider_contract_schema_digest": "9" * 64,
        }
    )

    class LegacyAdapter:
        contract_version = "research-assistant.integration-provider.v3"
        contract_schema_digest = "9" * 64

        def __init__(self) -> None:
            self.discoveries = 0
            self.handler_resolutions = 0

        def discover_instance(
            self,
            _provider_id: str,
            _instance_id: str,
        ) -> ProviderInstanceAttestation:
            self.discoveries += 1
            return valid.model_copy(
                update={
                    "provider_contract_version": self.contract_version,
                    "provider_contract_schema_digest": self.contract_schema_digest,
                }
            )

        def resolve_handler(self, _attestation: ProviderInstanceAttestation) -> Any:
            self.handler_resolutions += 1
            return lambda payload: payload

        def load_schema(self, _schema_digest: str) -> dict[str, Any]:
            raise AssertionError("Legacy provider schemas must not be loaded")

    legacy_adapter = LegacyAdapter()
    with pytest.raises(ConfigurationError, match="canonical provider v6 binding"):
        attach_provider_binding(
            legacy_binding,
            legacy_adapter,
            tenant_id="tenant-a",
            project_id="project-a",
        )
    assert legacy_adapter.discoveries == 0
    assert legacy_adapter.handler_resolutions == 0

    failures = (
        (
            valid.model_copy(
                update={"operation_ref": valid.operation_ref.model_copy(update={"input_schema_digest": "4" * 64})}
            ),
            ConfigurationError,
        ),
        (
            valid.model_copy(
                update={"provider_contract_version": "research-assistant.integration-provider.v3"}
            ),
            ConfigurationError,
        ),
        (
            valid.model_copy(update={"provider_contract_schema_digest": "4" * 64}),
            ConfigurationError,
        ),
        (
            valid.model_copy(
                update={
                    "descriptor_ref": valid.descriptor_ref.model_copy(
                        update={"digest": "4" * 64}
                    )
                }
            ),
            ConfigurationError,
        ),
        (
            valid.model_copy(update={"instance_ref": valid.instance_ref.model_copy(update={"fingerprint": "5" * 64})}),
            StaleCapabilityBindingError,
        ),
        (
            valid.model_copy(
                update={
                    "instance_ref": valid.instance_ref.model_copy(
                        update={"provider_resource_id": "app://tests/providers/other"}
                    )
                }
            ),
            StaleCapabilityBindingError,
        ),
        (
            valid.model_copy(
                update={
                    "instance_ref": valid.instance_ref.model_copy(
                        update={"discovered_provider_version": "2.0.0"}
                    )
                }
            ),
            StaleCapabilityBindingError,
        ),
        (
            valid.model_copy(
                update={
                    "instance_ref": valid.instance_ref.model_copy(
                        update={"discovered_resource_version": "2.0.0"}
                    )
                }
            ),
            StaleCapabilityBindingError,
        ),
        (valid.model_copy(update={"tenant_id": "other-tenant"}), AuthorizationError),
        (valid.model_copy(update={"project_id": "other-project"}), AuthorizationError),
        (valid.model_copy(update={"binding_id": "read.other"}), ConfigurationError),
        (valid.model_copy(update={"operations_digest": "4" * 64}), ConfigurationError),
        (valid.model_copy(update={"readiness": "DEGRADED"}), ConfigurationError),
        (valid.model_copy(update={"readiness": "UNAVAILABLE"}), ConfigurationError),
        (valid.model_copy(update={"health": "DEGRADED"}), ConfigurationError),
        (valid.model_copy(update={"auth_ready": False}), AuthorizationError),
        (valid.model_copy(update={"configuration_validated": False}), ConfigurationError),
        (valid.model_copy(update={"maturity": "UNKNOWN"}), ConfigurationError),
        (valid.model_copy(update={"maturity": "PREVIEW"}), ConfigurationError),
        (valid.model_copy(update={"maturity": "bogus"}), ConfigurationError),
        (valid.model_copy(update={"lifecycle": "DEPRECATED"}), ConfigurationError),
        (valid.model_copy(update={"lifecycle": "RETIRED"}), ConfigurationError),
        (
            valid.model_copy(
                update={
                    "approval_expires_at": datetime(
                        2026,
                        7,
                        22,
                        tzinfo=UTC,
                    )
                }
            ),
            ApprovalRequiredError,
        ),
        (
            valid.model_copy(
                update={"connection_ref": valid.connection_ref.model_copy(update={"id": "app://connections/other"})}
            ),
            ConfigurationError,
        ),
        (
            valid.model_copy(
                update={
                    "connection_ref": valid.connection_ref.model_copy(
                        update={"auth_mode": "api_key"}
                    )
                }
            ),
            ConfigurationError,
        ),
        (
            valid.model_copy(
                update={
                    "connection_ref": valid.connection_ref.model_copy(
                        update={"scopes": ("https://graph.microsoft.com/.default",)}
                    )
                }
            ),
            ConfigurationError,
        ),
        (
            valid.model_copy(update={"policy_ref": valid.policy_ref.model_copy(update={"id": "app://policy/other"})}),
            ConfigurationError,
        ),
        (
            valid.model_copy(
                update={"configuration_ref": valid.configuration_ref.model_copy(update={"digest": "7" * 64})}
            ),
            ConfigurationError,
        ),
        (
            valid.model_copy(
                update={
                    "allowed_destinations": DestinationConstraints(
                        constraints=("app://other",),
                        digest=canonical_digest(("app://other",)),
                    )
                }
            ),
            ConfigurationError,
        ),
    )
    for attestation, error_type in failures:
        failing = Adapter(attestation)
        with pytest.raises(error_type):
            attach_provider_binding(
                binding,
                failing,
                tenant_id="tenant-a",
                project_id="project-a",
                now=datetime(2026, 7, 23, tzinfo=UTC),
            )
        assert failing.handler_resolutions == 0

    digest_mismatch = Adapter(valid)
    digest_mismatch.contract_version = "provider.v999"
    with pytest.raises(ConfigurationError, match="pinned provider v6 artifact"):
        attach_provider_binding(
            binding,
            digest_mismatch,
            tenant_id="tenant-a",
            project_id="project-a",
        )
    assert digest_mismatch.handler_resolutions == 0

    schema_pin_mismatch = Adapter(valid)
    schema_pin_mismatch.contract_schema_digest = "9" * 64
    with pytest.raises(ConfigurationError, match="pinned provider v6 artifact"):
        attach_provider_binding(
            binding,
            schema_pin_mismatch,
            tenant_id="tenant-a",
            project_id="project-a",
        )
    assert schema_pin_mismatch.handler_resolutions == 0

    class BadSchemaAdapter(Adapter):
        def load_schema(self, _schema_digest: str) -> dict[str, Any]:
            return {"type": "invalid"}

    schema_mismatch = BadSchemaAdapter(valid)
    with pytest.raises(ConfigurationError, match="schema content"):
        attach_provider_binding(
            binding,
            schema_mismatch,
            tenant_id="tenant-a",
            project_id="project-a",
        )
    assert schema_mismatch.handler_resolutions == 0

    legacy_binding = binding.model_copy(update={"provider_contract_version": "integration-provider.v1"})
    legacy = Adapter(valid)
    with pytest.raises(ConfigurationError, match="canonical provider v6 binding"):
        attach_provider_binding(
            legacy_binding,
            legacy,
            tenant_id="tenant-a",
            project_id="project-a",
        )
    assert legacy.handler_resolutions == 0
    with pytest.raises(ValidationError):
        ProviderInstanceAttestation.model_validate(
            {**valid.model_dump(), "readiness": "ready"}
        )
    with pytest.raises(ValidationError):
        ProviderInstanceAttestation.model_validate(
            {**valid.model_dump(), "maturity": "ga"}
        )
    with pytest.raises(ValidationError):
        ProviderInstanceAttestation.model_validate(
            {**valid.model_dump(), "lifecycle": "active"}
        )


@pytest.mark.asyncio
async def test_runtime_registration_reattests_before_every_handler_call() -> None:
    binding = _binding("read.action")
    adapter = _ManifestProviderAdapter((binding,))
    registration = runtime_attested_registration(
        binding,
        adapter,
        tenant_id=binding.tenant_scope,
        project_id=binding.project_scope,
        clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
    )
    context = FunctionInvocationContext(
        cast(Any, SimpleNamespace(name=registration.tool_name)),
        {},
    )

    async def call_next() -> None:
        context.result = {"ok": True}

    result = registration.handler(
        {
            "context": context,
            "call_next": call_next,
        }
    )
    assert inspect.isawaitable(result)
    assert await result == {"value": {"ok": True}}
    assert adapter.handler_resolutions == 2
    assert adapter.handler_calls == 1

    class SyncAdapter(_ManifestProviderAdapter):
        def resolve_handler(
            self,
            _attestation: ProviderInstanceAttestation,
        ) -> Any:
            self.handler_resolutions += 1
            return lambda payload: {"sync": payload["value"]}

    sync_adapter = SyncAdapter((binding,))
    sync_registration = runtime_attested_registration(
        binding,
        sync_adapter,
        tenant_id=binding.tenant_scope,
        project_id=binding.project_scope,
        clock=lambda: datetime(2026, 7, 23, tzinfo=UTC),
    )
    sync_result = sync_registration.handler({"value": 1})
    assert inspect.isawaitable(sync_result)
    assert await sync_result == {"sync": 1}

    key = (binding.instance_ref.provider_id, binding.instance_ref.instance_id)
    adapter.attestations[key] = adapter.attestations[key].model_copy(
        update={"instance_ref": binding.instance_ref.model_copy(update={"fingerprint": "e" * 64})}
    )
    stale = registration.handler(
        {
            "context": context,
            "call_next": call_next,
        }
    )
    assert inspect.isawaitable(stale)
    with pytest.raises(StaleCapabilityBindingError):
        await stale
    assert adapter.handler_resolutions == 2
    assert adapter.handler_calls == 1


@pytest.mark.asyncio
async def test_capability_executor_retries_caches_and_isolates_idempotency() -> None:
    attempts = 0
    sleeps: list[float] = []
    registry = CapabilityRegistry()
    capability = CapabilityDescriptor(
        id="read.action",
        operation=OperationClass.READ,
        idempotency=IdempotencyMode.OPTIONAL,
        retry=RetryPolicy(max_attempts=2, delays_seconds=(0.25,)),
    )

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableInvocationError("retry")
        await asyncio.sleep(0)
        return payload

    _register(registry, capability, handler)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    executor = CapabilityExecutor(registry, sleep=sleep)
    context = InvocationContext(
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal",
        idempotency_key="same",
        operation_fingerprint="a" * 64,
    )
    first, second = await asyncio.gather(
        executor.invoke("read.action", {"value": 1}, context),
        executor.invoke("read.action", {"value": 2}, context),
    )
    assert first == second == {"value": 1}
    assert attempts == 2
    assert sleeps == [0.25]
    other_tenant = context.model_copy(update={"tenant_id": "other"})
    with pytest.raises(IsolationError):
        await executor.invoke("read.action", {"value": 3}, other_tenant)
    other_project = context.model_copy(update={"project_id": "other"})
    with pytest.raises(IsolationError):
        await executor.invoke_operation("action", {"value": 4}, other_project)
    assert attempts == 2


@pytest.mark.asyncio
async def test_capability_cache_is_bounded() -> None:
    registry = CapabilityRegistry()
    calls = 0

    def handler(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    capability = CapabilityDescriptor(
        id="read.bounded",
        operation=OperationClass.READ,
        idempotency=IdempotencyMode.OPTIONAL,
    )
    _register(registry, capability, handler)
    with pytest.raises(ValueError, match="at least 1"):
        CapabilityExecutor(registry, max_cached_results=0)
    executor = CapabilityExecutor(registry, max_cached_results=1)
    base = InvocationContext(
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal",
        idempotency_key="key",
        operation_fingerprint="a" * 64,
    )
    assert await executor.invoke(capability.id, {"call": 1}, base) == {"call": 1}
    second = base.model_copy(update={"operation_fingerprint": "b" * 64})
    assert await executor.invoke(capability.id, {"call": 2}, second) == {"call": 2}
    assert await executor.invoke(capability.id, {"call": 3}, base) == {"call": 3}
    assert calls == 3


@pytest.mark.asyncio
async def test_capability_executor_sync_no_cache_and_failure_paths() -> None:
    registry = CapabilityRegistry()
    _register(
        registry,
        CapabilityDescriptor(id="read.sync", operation=OperationClass.PURE),
        lambda payload: payload,
    )
    executor = CapabilityExecutor(registry)
    context = InvocationContext(
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal",
    )
    assert await executor.invoke("read.sync", {"ok": True}, context) == {"ok": True}

    expired = context.model_copy(update={"deadline_monotonic": 1.0})
    expired_executor = CapabilityExecutor(registry, monotonic=lambda: 2.0)
    with pytest.raises(DeadlineExceededError, match="expired"):
        await expired_executor.invoke("read.sync", {}, expired)
    bounded = context.model_copy(update={"deadline_monotonic": 100.0})
    assert await CapabilityExecutor(registry, monotonic=lambda: 50.0).invoke(
        "read.sync", {"bounded": True}, bounded
    ) == {"bounded": True}

    generic = CapabilityRegistry()
    _register(
        generic,
        CapabilityDescriptor(id="read.generic", operation=OperationClass.READ),
        lambda _payload: (_ for _ in ()).throw(ValueError("secret")),
    )
    with pytest.raises(InvocationError) as captured:
        await CapabilityExecutor(generic).invoke("read.generic", {}, context)
    assert captured.value.context["exception"] == "ValueError"

    non_retry = CapabilityRegistry()
    _register(
        non_retry,
        CapabilityDescriptor(id="read.fail", operation=OperationClass.READ),
        lambda _payload: (_ for _ in ()).throw(ContractError("bad input")),
    )
    with pytest.raises(ContractError):
        await CapabilityExecutor(non_retry).invoke("read.fail", {}, context)

    retry_registry = CapabilityRegistry()
    _register(
        retry_registry,
        CapabilityDescriptor(
            id="read.retry-deadline",
            operation=OperationClass.READ,
            retry=RetryPolicy(max_attempts=2, delays_seconds=(2,)),
        ),
        lambda _payload: (_ for _ in ()).throw(RetryableInvocationError("retry")),
    )
    times = iter((0.0, 9.0))
    with pytest.raises(DeadlineExceededError, match="retry"):
        await CapabilityExecutor(
            retry_registry,
            monotonic=lambda: next(times),
        ).invoke(
            "read.retry-deadline",
            {},
            context.model_copy(update={"deadline_monotonic": 10.0}),
        )


@pytest.mark.asyncio
async def test_capability_executor_timeout_and_cancellation() -> None:
    timeout_registry = CapabilityRegistry()

    async def slow(_payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.1)
        return {}

    _register(
        timeout_registry,
        CapabilityDescriptor(
            id="read.slow",
            operation=OperationClass.READ,
            timeout_seconds=0.001,
            retry=RetryPolicy(max_attempts=2, delays_seconds=(0,)),
        ),
        slow,
    )
    with pytest.raises(DeadlineExceededError, match="exceeded"):
        await CapabilityExecutor(timeout_registry).invoke(
            "read.slow",
            {},
            InvocationContext(tenant_id="tenant-a", project_id="project-a", principal_id="p"),
        )
    blocking_registry = CapabilityRegistry()

    def blocking(_payload: dict[str, Any]) -> dict[str, Any]:
        time.sleep(0.05)
        return {}

    _register(
        blocking_registry,
        CapabilityDescriptor(
            id="read.blocking",
            operation=OperationClass.READ,
            timeout_seconds=0.001,
        ),
        blocking,
    )
    with pytest.raises(DeadlineExceededError, match="exceeded"):
        await CapabilityExecutor(blocking_registry).invoke(
            "read.blocking",
            {},
            InvocationContext(tenant_id="tenant-a", project_id="project-a", principal_id="p"),
        )

    cancelled_registry = CapabilityRegistry()

    async def cancelled(_payload: dict[str, Any]) -> dict[str, Any]:
        raise asyncio.CancelledError

    _register(
        cancelled_registry,
        CapabilityDescriptor(id="read.cancel", operation=OperationClass.READ),
        cancelled,
    )
    with pytest.raises(asyncio.CancelledError):
        await CapabilityExecutor(cancelled_registry).invoke(
            "read.cancel",
            {},
            InvocationContext(tenant_id="tenant-a", project_id="project-a", principal_id="p"),
        )


def _external_capability(
    *,
    operation: OperationClass = OperationClass.WRITE_REVERSIBLE,
    replay: CompletedReplayMode = CompletedReplayMode.DENY,
    timeout_seconds: float = 30,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id="write.external",
        operation=operation,
        required_scopes=frozenset({"write"}),
        allowed_destinations=("destination-a", "destination-b"),
        side_effect_destinations=("destination-a", "destination-b"),
        approval=ApprovalMode.REQUIRED,
        timeout_seconds=timeout_seconds,
        idempotency=IdempotencyMode.REQUIRED,
        idempotency_policy=IdempotencyPolicy(
            lease_seconds=max(60, timeout_seconds),
            completed_replay=replay,
        ),
        retry=RetryPolicy(max_attempts=3, delays_seconds=(0, 0)),
    )


def _external_registry(
    capability: CapabilityDescriptor,
    handler: Any,
    *,
    project_id: str = "project-a",
) -> tuple[CapabilityRegistry, CapabilityBinding]:
    registry = CapabilityRegistry()
    registry.add_descriptor(capability)
    binding = _binding(capability.id).model_copy(update={"project_scope": project_id})
    registry.register_tool(
        ToolRegistration(
            binding=binding,
            tool_name="external",
            handler=handler,
            current_instance_fingerprint=binding.instance_ref.fingerprint,
        )
    )
    return registry, binding


def _external_context(**updates: Any) -> InvocationContext:
    values: dict[str, Any] = {
        "tenant_id": "tenant-a",
        "project_id": "project-a",
        "principal_id": "actor-a",
        "scopes": frozenset({"write"}),
        "destination": "destination-a",
        "approval_decision_id": "approval-a",
        "invocation_id": "invocation-a",
        "idempotency_key": "caller-key",
        "operation_fingerprint": "a" * 64,
    }
    values.update(updates)
    return InvocationContext.model_validate(values)


def _durable_key(
    binding: CapabilityBinding,
    context: InvocationContext,
) -> IdempotencyKey:
    assert context.destination is not None
    assert context.idempotency_key is not None
    assert context.operation_fingerprint is not None
    return IdempotencyKey(
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        binding_digest=canonical_digest(binding.model_dump(mode="json")),
        operation_id=binding.operation_ref.id,
        destination=context.destination,
        caller_key=context.idempotency_key,
        argument_hash=context.operation_fingerprint,
    )


def _approval_request(
    binding: CapabilityBinding,
    context: InvocationContext,
    release_id: str,
) -> ApprovalConsumptionRequest:
    key = _durable_key(binding, context)
    assert context.approval_decision_id is not None
    assert context.invocation_id is not None
    return ApprovalConsumptionRequest(
        approval_decision_id=context.approval_decision_id,
        binding_id=binding.binding_id,
        tenant_id=context.tenant_id,
        project_id=binding.project_scope,
        actor_id=context.principal_id,
        scopes=tuple(sorted(context.scopes)),
        binding_digest=key.binding_digest,
        instance_fingerprint=binding.instance_ref.fingerprint,
        operation_id=binding.operation_ref.id,
        operation_version=binding.operation_ref.version,
        argument_hash=key.argument_hash,
        destination=key.destination,
        policy_id=binding.policy_ref.id,
        policy_version=binding.policy_ref.version,
        policy_digest=binding.policy_ref.digest,
        release_id=release_id,
        invocation_id=context.invocation_id,
        idempotency_key_digest=key.digest,
    )


def _approval_adapter(
    binding: CapabilityBinding,
    context: InvocationContext,
    release_id: str,
    *,
    backend: InMemoryApprovalBackend | None = None,
) -> InMemoryApprovalConsumptionAdapter:
    effective_backend = backend or InMemoryApprovalBackend()
    request = _approval_request(binding, context, release_id)
    now = datetime(2000, 1, 1, tzinfo=UTC)
    effective_backend.grants[request.approval_decision_id] = ApprovalGrant(
        request=request,
        request_digest=request.digest,
        version="1",
        approver_id="approver-a",
        approved_at=now - timedelta(minutes=1),
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    return InMemoryApprovalConsumptionAdapter(effective_backend)


def _approval_provenance() -> IdempotencyApprovalProvenance:
    now = datetime.now(UTC)
    return IdempotencyApprovalProvenance(
        approval_decision_id="approval-a",
        request_digest="a" * 64,
        receipt_digest="b" * 64,
        approval_version="1",
        consumption_id="consumption-a",
        consumption_version="2",
        approver_id="approver-a",
        consumed_at=now,
    )


class _AutoApprovalAdapter:
    is_durable = True

    def __init__(self) -> None:
        self.requests: dict[str, str] = {}
        self.calls = 0

    async def consume(
        self,
        request: ApprovalConsumptionRequest,
    ) -> ApprovalConsumptionResult:
        self.calls += 1
        prior = self.requests.get(request.approval_decision_id)
        if prior is not None:
            disposition = (
                ApprovalConsumptionDisposition.ALREADY_CONSUMED
                if prior == request.digest
                else ApprovalConsumptionDisposition.MISMATCH
            )
            return ApprovalConsumptionResult(
                disposition=disposition,
                approval_decision_id=request.approval_decision_id,
                request_digest=request.digest,
                approval_version="1",
                reason_code=disposition.value,
            )
        self.requests[request.approval_decision_id] = request.digest
        now = datetime(2000, 1, 1, tzinfo=UTC)
        receipt = ApprovalReceipt(
            approval_decision_id=request.approval_decision_id,
            request_digest=request.digest,
            approval_version="1",
            consumption_id=f"consumption-{self.calls}",
            consumption_version="2",
            approver_id="approver-a",
            consumed_at=now,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        return ApprovalConsumptionResult(
            disposition=ApprovalConsumptionDisposition.CONSUMED,
            approval_decision_id=request.approval_decision_id,
            request_digest=request.digest,
            approval_version="1",
            receipt=receipt,
        )


def _approval_grant(
    request: ApprovalConsumptionRequest,
    *,
    state: ApprovalGrantState = ApprovalGrantState.APPROVED,
    expires_at: datetime | None = None,
    denial_reason: str | None = None,
) -> ApprovalGrant:
    now = datetime(2026, 7, 24, tzinfo=UTC)
    return ApprovalGrant(
        request=request,
        request_digest=request.digest,
        version="1",
        state=state,
        approver_id="approver-a",
        approved_at=now - timedelta(minutes=1),
        expires_at=expires_at or now + timedelta(minutes=5),
        denial_reason=denial_reason,
    )


def test_approval_contracts_are_canonical_and_strict() -> None:
    capability = _external_capability()
    _, binding = _external_registry(capability, lambda payload: payload)
    request = _approval_request(
        binding,
        _external_context(),
        f"sha256:{'a' * 64}",
    )
    assert request.digest == canonical_idempotency_digest(request.model_dump(mode="json"))
    assert len(approval_contract_schema_digest()) == 64
    with pytest.raises(ValidationError, match="sorted and unique"):
        ApprovalConsumptionRequest.model_validate(
            {**request.model_dump(), "scopes": ("write", "alpha")}
        )
    with pytest.raises(ValidationError, match="empty"):
        ApprovalConsumptionRequest.model_validate(
            {**request.model_dump(), "scopes": ("",)}
        )

    now = datetime(2026, 7, 24, tzinfo=UTC)
    receipt = ApprovalReceipt(
        approval_decision_id=request.approval_decision_id,
        request_digest=request.digest,
        approval_version="1",
        consumption_id="consumption-a",
        consumption_version="2",
        approver_id="approver-a",
        consumed_at=now,
        expires_at=now + timedelta(minutes=1),
    )
    assert len(receipt.digest) == 64
    with pytest.raises(ValidationError, match="one-time"):
        ApprovalReceipt.model_validate({**receipt.model_dump(), "one_time": False})
    with pytest.raises(ValidationError, match="timezone-aware"):
        ApprovalReceipt.model_validate(
            {**receipt.model_dump(), "consumed_at": now.replace(tzinfo=None)}
        )
    with pytest.raises(ValidationError, match="after expiry"):
        ApprovalReceipt.model_validate(
            {
                **receipt.model_dump(),
                "consumed_at": now + timedelta(minutes=2),
            }
        )
    with pytest.raises(ValidationError, match="only consumed"):
        ApprovalConsumptionResult(
            disposition=ApprovalConsumptionDisposition.DENIED,
            approval_decision_id=request.approval_decision_id,
            request_digest=request.digest,
            receipt=receipt,
            reason_code="denied",
        )
    with pytest.raises(ValidationError, match="carry a receipt"):
        ApprovalConsumptionResult(
            disposition=ApprovalConsumptionDisposition.CONSUMED,
            approval_decision_id=request.approval_decision_id,
            request_digest=request.digest,
        )
    with pytest.raises(ValidationError, match="require a reason code"):
        ApprovalConsumptionResult(
            disposition=ApprovalConsumptionDisposition.DENIED,
            approval_decision_id=request.approval_decision_id,
            request_digest=request.digest,
        )
    with pytest.raises(ValidationError, match="denial reason"):
        ApprovalConsumptionResult(
            disposition=ApprovalConsumptionDisposition.CONSUMED,
            approval_decision_id=request.approval_decision_id,
            request_digest=request.digest,
            receipt=receipt,
            reason_code="invalid",
        )
    with pytest.raises(ValidationError, match="request digest"):
        ApprovalGrant.model_validate(
            {**_approval_grant(request).model_dump(), "request_digest": "0" * 64}
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        ApprovalGrant.model_validate(
            {
                **_approval_grant(request).model_dump(),
                "approved_at": now.replace(tzinfo=None),
            }
        )
    with pytest.raises(ValidationError, match="expires before"):
        ApprovalGrant.model_validate(
            {
                **_approval_grant(request).model_dump(),
                "approved_at": now,
                "expires_at": now - timedelta(seconds=1),
            }
        )
    with pytest.raises(ValidationError, match="denial reason"):
        ApprovalGrant.model_validate(
            {**_approval_grant(request).model_dump(), "state": "denied"}
        )
    with pytest.raises(ValidationError, match="carry a receipt"):
        ApprovalGrant.model_validate(
            {**_approval_grant(request).model_dump(), "state": "consumed"}
        )


@pytest.mark.asyncio
async def test_in_memory_approval_consumption_is_atomic_and_exact() -> None:
    capability = _external_capability()
    _, binding = _external_registry(capability, lambda payload: payload)
    request = _approval_request(
        binding,
        _external_context(),
        f"sha256:{'b' * 64}",
    )
    now = datetime(2026, 7, 24, tzinfo=UTC)
    backend = InMemoryApprovalBackend()
    first = InMemoryApprovalConsumptionAdapter(backend, clock=lambda: now)
    second = InMemoryApprovalConsumptionAdapter(backend, clock=lambda: now)
    await first.issue(_approval_grant(request))
    with pytest.raises(ValueError, match="already exists"):
        await second.issue(_approval_grant(request))
    results = await asyncio.gather(first.consume(request), second.consume(request))
    assert {result.disposition for result in results} == {
        ApprovalConsumptionDisposition.CONSUMED,
        ApprovalConsumptionDisposition.ALREADY_CONSUMED,
    }
    consumed = next(
        result
        for result in results
        if result.disposition == ApprovalConsumptionDisposition.CONSUMED
    )
    assert consumed.receipt is not None
    assert consumed.receipt.request_digest == request.digest

    missing = request.model_copy(update={"approval_decision_id": "missing"})
    assert (await first.consume(missing)).disposition == ApprovalConsumptionDisposition.NOT_FOUND
    mismatched = request.model_copy(update={"actor_id": "other-actor"})
    assert (await first.consume(mismatched)).disposition == ApprovalConsumptionDisposition.MISMATCH
    binding_scoped = request.model_copy(update={"approval_decision_id": "binding-scoped"})
    await first.issue(_approval_grant(binding_scoped))
    wrong_binding = binding_scoped.model_copy(update={"binding_id": "write.other"})
    assert (await first.consume(wrong_binding)).disposition == ApprovalConsumptionDisposition.MISMATCH

    denied = request.model_copy(update={"approval_decision_id": "denied"})
    await first.issue(
        _approval_grant(
            denied,
            state=ApprovalGrantState.DENIED,
            denial_reason="policy_denied",
        )
    )
    denial = await first.consume(denied)
    assert denial.disposition == ApprovalConsumptionDisposition.DENIED
    assert denial.reason_code == "policy_denied"

    expired = request.model_copy(update={"approval_decision_id": "expired"})
    await first.issue(
        _approval_grant(
            expired,
            expires_at=now - timedelta(seconds=1),
        )
    )
    assert (await first.consume(expired)).disposition == ApprovalConsumptionDisposition.EXPIRED
    assert (await first.consume(expired)).disposition == ApprovalConsumptionDisposition.EXPIRED
    revoked = request.model_copy(update={"approval_decision_id": "revoked"})
    await first.issue(
        _approval_grant(
            revoked,
            state=ApprovalGrantState.REVOKED,
        )
    )
    assert (await first.consume(revoked)).disposition == ApprovalConsumptionDisposition.REVOKED


@pytest.mark.asyncio
async def test_executor_consumes_exact_approval_and_persists_receipt() -> None:
    release_id = f"sha256:{'c' * 64}"
    context = _external_context()
    capability = _external_capability(replay=CompletedReplayMode.RETURN_RESULT)
    registry, binding = _external_registry(capability, lambda payload: payload)
    backend = InMemoryApprovalBackend()
    approval = _approval_adapter(binding, context, release_id, backend=backend)
    idempotency = InMemoryIdempotencyStore()
    executor = CapabilityExecutor(
        registry,
        idempotency_store=idempotency,
        approval_adapter=approval,
        release_id=release_id,
        allow_test_idempotency_store=True,
        allow_test_approval_adapter=True,
    )
    assert await executor.invoke(capability.id, {"value": 1}, context) == {"value": 1}
    record = idempotency.record_for(_durable_key(binding, context))
    assert record is not None and record.approval is not None
    grant = backend.grants[cast(str, context.approval_decision_id)]
    assert grant.receipt is not None
    assert record.approval.request_digest == grant.receipt.request_digest
    assert record.approval.receipt_digest == grant.receipt.digest
    assert await executor.invoke(capability.id, {"value": 999}, context) == {"value": 1}
    assert grant == backend.grants[cast(str, context.approval_decision_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "error_type"),
    (
        (ApprovalConsumptionDisposition.DENIED, ApprovalDeniedError),
        (ApprovalConsumptionDisposition.EXPIRED, ApprovalExpiredError),
        (ApprovalConsumptionDisposition.NOT_FOUND, ApprovalRequiredError),
        (ApprovalConsumptionDisposition.MISMATCH, ApprovalMismatchError),
        (ApprovalConsumptionDisposition.ALREADY_CONSUMED, ApprovalAlreadyConsumedError),
        (ApprovalConsumptionDisposition.REVOKED, ApprovalRevokedError),
    ),
)
async def test_executor_maps_terminal_approval_results_without_calling_handler(
    disposition: ApprovalConsumptionDisposition,
    error_type: type[HarnessError],
) -> None:
    calls = 0

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    class TerminalAdapter:
        is_durable = True

        async def consume(
            self,
            request: ApprovalConsumptionRequest,
        ) -> ApprovalConsumptionResult:
            return ApprovalConsumptionResult(
                disposition=disposition,
                approval_decision_id=request.approval_decision_id,
                request_digest=request.digest,
                approval_version="1",
                reason_code=disposition.value,
            )

    capability = _external_capability()
    registry, _ = _external_registry(capability, handler)
    with pytest.raises(error_type):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=TerminalAdapter(),
            release_id=f"sha256:{'d' * 64}",
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, _external_context())
    assert calls == 0


@pytest.mark.asyncio
async def test_executor_fails_closed_for_missing_unavailable_or_invalid_approval() -> None:
    calls = 0

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    capability = _external_capability()
    registry, _ = _external_registry(capability, handler)
    release_id = f"sha256:{'e' * 64}"
    context = _external_context()
    store = InMemoryIdempotencyStore()
    with pytest.raises(ApprovalStoreUnavailableError):
        await CapabilityExecutor(
            registry,
            idempotency_store=store,
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)
    with pytest.raises(ApprovalStoreUnavailableError):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=InMemoryApprovalConsumptionAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)

    class UnavailableAdapter:
        is_durable = True

        async def consume(self, request: ApprovalConsumptionRequest) -> ApprovalConsumptionResult:
            raise RuntimeError(request.approval_decision_id)

    with pytest.raises(ApprovalStoreUnavailableError):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=UnavailableAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)

    class InvalidAdapter:
        is_durable = True

        async def consume(self, _request: ApprovalConsumptionRequest) -> ApprovalConsumptionResult:
            return cast(ApprovalConsumptionResult, {})

    with pytest.raises(ApprovalResultInvalidError):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=InvalidAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)

    class WrongIdentityAdapter:
        is_durable = True

        async def consume(
            self,
            request: ApprovalConsumptionRequest,
        ) -> ApprovalConsumptionResult:
            return ApprovalConsumptionResult(
                disposition=ApprovalConsumptionDisposition.DENIED,
                approval_decision_id="other-approval",
                request_digest=request.digest,
                approval_version="1",
                reason_code="denied",
            )

    with pytest.raises(ApprovalResultInvalidError):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=WrongIdentityAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)

    class InvalidContractAdapter:
        is_durable = True

        async def consume(
            self,
            request: ApprovalConsumptionRequest,
        ) -> ApprovalConsumptionResult:
            receipt = ApprovalReceipt.model_construct(
                approval_decision_id=request.approval_decision_id,
                request_digest=request.digest,
                approval_version="1",
                consumption_id="consumption-invalid-contract",
                consumption_version="2",
                approver_id="approver-a",
                consumed_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=2),
                one_time=False,
            )
            return ApprovalConsumptionResult.model_construct(
                disposition=ApprovalConsumptionDisposition.CONSUMED,
                approval_decision_id=request.approval_decision_id,
                request_digest=request.digest,
                approval_version="1",
                receipt=receipt,
                reason_code=None,
            )

    with pytest.raises(ApprovalResultInvalidError):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=InvalidContractAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)

    class InvalidReceiptAdapter:
        is_durable = True

        async def consume(
            self,
            request: ApprovalConsumptionRequest,
        ) -> ApprovalConsumptionResult:
            now = datetime.now(UTC)
            receipt = ApprovalReceipt.model_construct(
                approval_decision_id=request.approval_decision_id,
                request_digest=request.digest,
                approval_version="1",
                consumption_id="consumption-invalid",
                consumption_version="2",
                approver_id="approver-a",
                consumed_at=now + timedelta(minutes=1),
                expires_at=now + timedelta(minutes=2),
                one_time=True,
            )
            return ApprovalConsumptionResult.model_construct(
                disposition=ApprovalConsumptionDisposition.CONSUMED,
                approval_decision_id=request.approval_decision_id,
                request_digest=request.digest,
                approval_version="1",
                receipt=receipt,
                reason_code=None,
            )

    with pytest.raises(ApprovalResultInvalidError, match="receipt"):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=InvalidReceiptAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)

    class StructuredFailureAdapter:
        is_durable = True

        async def consume(
            self,
            _request: ApprovalConsumptionRequest,
        ) -> ApprovalConsumptionResult:
            raise ApprovalDeniedError("provider denied")

    with pytest.raises(ApprovalDeniedError, match="provider denied"):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=StructuredFailureAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)
    assert calls == 0


@pytest.mark.asyncio
async def test_uncertain_approval_consumption_blocks_fresh_approval_and_handler() -> None:
    calls = 0
    adapter_calls = 0
    entered = asyncio.Event()

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    class BlockingAdapter:
        is_durable = True

        async def consume(
            self,
            request: ApprovalConsumptionRequest,
        ) -> ApprovalConsumptionResult:
            nonlocal adapter_calls
            adapter_calls += 1
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError(request.approval_decision_id)

    capability = _external_capability(timeout_seconds=0.01)
    registry, _ = _external_registry(capability, handler)
    store = InMemoryIdempotencyStore()
    executor = CapabilityExecutor(
        registry,
        idempotency_store=store,
        approval_adapter=BlockingAdapter(),
        release_id=f"sha256:{'f' * 64}",
        allow_test_idempotency_store=True,
    )
    with pytest.raises(ApprovalConsumptionUncertainError):
        await executor.invoke(capability.id, {}, _external_context())
    assert entered.is_set()
    with pytest.raises(IdempotencyReconciliationRequiredError):
        await executor.invoke(capability.id, {}, _external_context())
    assert adapter_calls == 1
    assert calls == 0


@pytest.mark.asyncio
async def test_cancelled_approval_consumption_is_terminal_for_idempotency() -> None:
    adapter_calls = 0
    entered = asyncio.Event()

    class BlockingAdapter:
        is_durable = True

        async def consume(
            self,
            request: ApprovalConsumptionRequest,
        ) -> ApprovalConsumptionResult:
            nonlocal adapter_calls
            adapter_calls += 1
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError(request.approval_decision_id)

    capability = _external_capability()
    registry, _ = _external_registry(capability, lambda payload: payload)
    store = InMemoryIdempotencyStore()
    executor = CapabilityExecutor(
        registry,
        idempotency_store=store,
        approval_adapter=BlockingAdapter(),
        release_id=f"sha256:{'1' * 64}",
        allow_test_idempotency_store=True,
    )
    context = _external_context()
    task = asyncio.create_task(executor.invoke(capability.id, {}, context))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(IdempotencyReconciliationRequiredError):
        await executor.invoke(capability.id, {}, context)
    assert adapter_calls == 1


def test_idempotency_contracts_are_canonical_and_fail_closed() -> None:
    key = IdempotencyKey(
        tenant_id="tenant-a",
        project_id="project-a",
        binding_digest="a" * 64,
        operation_id="provider.write",
        destination="destination-a",
        caller_key="caller-key",
        argument_hash="b" * 64,
    )
    assert key.digest == canonical_idempotency_digest(key.model_dump(mode="json"))
    assert len(idempotency_contract_schema_digest()) == 64
    now = datetime(2026, 7, 24, tzinfo=UTC)
    claimed = IdempotencyRecord(
        key=key,
        state=IdempotencyState.CLAIMED,
        version="1",
        claim_token_hash="c" * 64,
        lease_expires_at=now + timedelta(seconds=60),
        actor_id="actor-a",
        release_id=f"sha256:{'d' * 64}",
        claimed_at=now,
    )
    assert IdempotencyClaim(
        disposition=ClaimDisposition.ACQUIRED,
        record=claimed,
        claim_token="e" * 32,
    ).claim_token
    with pytest.raises(ValidationError, match="only acquired"):
        IdempotencyClaim(
            disposition=ClaimDisposition.IN_PROGRESS,
            record=claimed,
            claim_token="e" * 32,
        )
    with pytest.raises(ValidationError, match="cannot have started"):
        claimed.model_copy(update={"started_at": now}).model_validate(
            {**claimed.model_dump(), "started_at": now}
        )
    with pytest.raises(ValidationError, match="require started_at"):
        IdempotencyRecord.model_validate(
            {**claimed.model_dump(), "state": IdempotencyState.IN_PROGRESS}
        )
    with pytest.raises(ValidationError, match="result provenance"):
        IdempotencyRecord.model_validate(
            {**claimed.model_dump(), "state": IdempotencyState.COMPLETED}
        )
    with pytest.raises(ValidationError, match="deterministic reconciliation"):
        IdempotencyRecord.model_validate(
            {**claimed.model_dump(), "state": IdempotencyState.FAILED}
        )
    with pytest.raises(ValidationError, match="marked started"):
        IdempotencyRecord.model_validate(
            {**claimed.model_dump(), "irreversible_started": True}
        )
    with pytest.raises(ValidationError, match="lease must cover"):
        CapabilityDescriptor(
            id="write.short-lease",
            operation=OperationClass.WRITE_REVERSIBLE,
            side_effect_destinations=("destination-a",),
            approval=ApprovalMode.REQUIRED,
            timeout_seconds=61,
            idempotency=IdempotencyMode.REQUIRED,
            idempotency_policy=IdempotencyPolicy(lease_seconds=60),
        )
    with pytest.raises(ValidationError, match="cannot replay"):
        _external_capability(
            operation=OperationClass.WRITE_IRREVERSIBLE,
            replay=CompletedReplayMode.RETURN_RESULT,
        )
    with pytest.raises(ValueError, match="release_id"):
        CapabilityExecutor(CapabilityRegistry(), release_id="mutable")


@pytest.mark.asyncio
async def test_in_memory_store_models_cross_instance_claims_and_transitions() -> None:
    now = [datetime(2026, 7, 24, tzinfo=UTC)]
    backend = InMemoryIdempotencyBackend()
    first = InMemoryIdempotencyStore(backend, clock=lambda: now[0])
    second = InMemoryIdempotencyStore(backend, clock=lambda: now[0])
    context = _external_context()
    _, binding = _external_registry(_external_capability(), lambda payload: payload)
    key = _durable_key(binding, context)
    release_id = f"sha256:{'a' * 64}"
    claims = await asyncio.gather(
        first.claim(key, actor_id="actor-a", release_id=release_id, lease_seconds=60),
        second.claim(key, actor_id="actor-a", release_id=release_id, lease_seconds=60),
    )
    acquired = next(claim for claim in claims if claim.disposition == ClaimDisposition.ACQUIRED)
    assert {claim.disposition for claim in claims} == {
        ClaimDisposition.ACQUIRED,
        ClaimDisposition.IN_PROGRESS,
    }
    assert acquired.claim_token is not None
    with pytest.raises(IdempotencyConcurrencyError):
        await first.mark_in_progress(
            key,
            claim_token=acquired.claim_token,
            expected_version="wrong",
            irreversible=False,
        )
    started = await first.mark_in_progress(
        key,
        claim_token=acquired.claim_token,
        expected_version=acquired.record.version,
        irreversible=False,
    )
    assert started.state == IdempotencyState.IN_PROGRESS
    assert (await second.claim(key, actor_id="actor-a", release_id=release_id, lease_seconds=60)).disposition == (
        ClaimDisposition.IN_PROGRESS
    )
    with pytest.raises(IdempotencyResultMismatchError):
        await first.complete(
            key,
            claim_token=acquired.claim_token,
            expected_version=started.version,
            result={"value": 1},
            result_hash="f" * 64,
        )
    result = {"value": 1}
    completed = await first.complete(
        key,
        claim_token=acquired.claim_token,
        expected_version=started.version,
        result=result,
        result_hash=canonical_idempotency_digest(result),
    )
    assert completed.state == IdempotencyState.COMPLETED
    assert completed.result_ref is not None
    assert await second.load_result(completed.result_ref) == result
    assert (await second.claim(key, actor_id="actor-b", release_id=release_id, lease_seconds=60)).disposition == (
        ClaimDisposition.COMPLETED
    )
    assert first.record_for(key) == completed

    failed_key = key.model_copy(update={"caller_key": "failed"})
    failed_claim = await first.claim(
        failed_key,
        actor_id="actor-a",
        release_id=release_id,
        lease_seconds=60,
    )
    assert failed_claim.claim_token is not None
    failed = await first.fail(
        failed_key,
        claim_token=failed_claim.claim_token,
        expected_version=failed_claim.record.version,
        failure_code="handler_failed",
    )
    assert failed.reconciliation_required is True
    assert (
        await second.claim(
            failed_key,
            actor_id="actor-a",
            release_id=release_id,
            lease_seconds=60,
        )
    ).disposition == ClaimDisposition.RECONCILIATION_REQUIRED
    with pytest.raises(IdempotencyConcurrencyError):
        await first.fail(
            failed_key,
            claim_token=failed_claim.claim_token,
            expected_version=failed_claim.record.version,
            failure_code="again",
        )

    stale_key = key.model_copy(update={"caller_key": "stale"})
    await first.claim(stale_key, actor_id="actor-a", release_id=release_id, lease_seconds=10)
    now[0] += timedelta(seconds=11)
    stale = await second.claim(
        stale_key,
        actor_id="actor-a",
        release_id=release_id,
        lease_seconds=10,
    )
    assert stale.disposition == ClaimDisposition.RECONCILIATION_REQUIRED
    assert await first.load_result("memory://missing") is None


@pytest.mark.asyncio
async def test_durable_executor_serializes_replicas_and_isolates_keys() -> None:
    calls: list[dict[str, Any]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        entered.set()
        await release.wait()
        return {"value": payload["value"]}

    capability = _external_capability(replay=CompletedReplayMode.RETURN_RESULT)
    registry, _ = _external_registry(capability, handler)
    backend = InMemoryIdempotencyBackend()
    approvals = _AutoApprovalAdapter()
    first = CapabilityExecutor(
        registry,
        idempotency_store=InMemoryIdempotencyStore(backend),
        approval_adapter=approvals,
        release_id=f"sha256:{'1' * 64}",
        allow_test_idempotency_store=True,
    )
    second = CapabilityExecutor(
        registry,
        idempotency_store=InMemoryIdempotencyStore(backend),
        approval_adapter=approvals,
        release_id=f"sha256:{'1' * 64}",
        allow_test_idempotency_store=True,
    )
    context = _external_context()
    first_task = asyncio.create_task(first.invoke(capability.id, {"value": 1}, context))
    await entered.wait()
    with pytest.raises(IdempotencyInProgressError) as captured:
        await second.invoke(capability.id, {"value": 1}, context)
    assert captured.value.retryable is True
    release.set()
    assert await first_task == {"value": 1}
    assert await second.invoke(capability.id, {"value": 999}, context) == {"value": 1}
    assert len(calls) == 1

    other_tenant = context.model_copy(
        update={
            "tenant_id": "tenant-b",
            "approval_decision_id": "approval-tenant-b",
            "invocation_id": "invocation-tenant-b",
        }
    )
    with pytest.raises(IsolationError):
        await second.invoke(capability.id, {"value": 2}, other_tenant)
    assert len(calls) == 1
    other_argument = context.model_copy(
        update={
            "operation_fingerprint": "b" * 64,
            "approval_decision_id": "approval-argument-b",
            "invocation_id": "invocation-argument-b",
        }
    )
    assert await second.invoke(capability.id, {"value": 3}, other_argument) == {"value": 3}
    other_destination = context.model_copy(
        update={
            "destination": "destination-b",
            "approval_decision_id": "approval-destination-b",
            "invocation_id": "invocation-destination-b",
        }
    )
    assert await second.invoke(capability.id, {"value": 4}, other_destination) == {"value": 4}
    project_registry, _ = _external_registry(capability, handler, project_id="project-b")
    project_executor = CapabilityExecutor(
        project_registry,
        idempotency_store=InMemoryIdempotencyStore(backend),
        approval_adapter=_AutoApprovalAdapter(),
        release_id=f"sha256:{'1' * 64}",
        allow_test_idempotency_store=True,
    )
    with pytest.raises(IsolationError):
        await project_executor.invoke(capability.id, {"value": 5}, context)
    project_context = context.model_copy(
        update={
            "project_id": "project-b",
            "approval_decision_id": "approval-project-b",
            "invocation_id": "invocation-project-b",
        }
    )
    assert await project_executor.invoke(capability.id, {"value": 5}, project_context) == {"value": 5}
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_durable_executor_replay_policies_and_result_integrity() -> None:
    calls = 0

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    context = _external_context()
    release_id = f"sha256:{'2' * 64}"
    denied = _external_capability()
    denied_registry, _ = _external_registry(denied, handler)
    denied_executor = CapabilityExecutor(
        denied_registry,
        idempotency_store=InMemoryIdempotencyStore(),
        approval_adapter=_AutoApprovalAdapter(),
        release_id=release_id,
        allow_test_idempotency_store=True,
    )
    assert await denied_executor.invoke(denied.id, {"value": "denied"}, context) == {
        "value": "denied"
    }
    with pytest.raises(IdempotencyReplayDeniedError):
        await denied_executor.invoke(denied.id, {"value": "duplicate"}, context)

    referenced = _external_capability(replay=CompletedReplayMode.RETURN_REFERENCE)
    referenced_registry, _ = _external_registry(referenced, handler)
    referenced_executor = CapabilityExecutor(
        referenced_registry,
        idempotency_store=InMemoryIdempotencyStore(),
        approval_adapter=_AutoApprovalAdapter(),
        release_id=release_id,
        allow_test_idempotency_store=True,
    )
    assert await referenced_executor.invoke(referenced.id, {"value": "reference"}, context) == {
        "value": "reference"
    }
    reference = await referenced_executor.invoke(referenced.id, {"value": "duplicate"}, context)
    assert reference["idempotency"]["result_ref"].startswith("memory://")

    replayed = _external_capability(replay=CompletedReplayMode.RETURN_RESULT)
    replayed_registry, binding = _external_registry(replayed, handler)
    replay_store = InMemoryIdempotencyStore()
    replay_executor = CapabilityExecutor(
        replayed_registry,
        idempotency_store=replay_store,
        approval_adapter=_AutoApprovalAdapter(),
        release_id=release_id,
        allow_test_idempotency_store=True,
    )
    assert await replay_executor.invoke(replayed.id, {"value": "original"}, context) == {
        "value": "original"
    }
    record = replay_store.record_for(_durable_key(binding, context))
    assert record is not None and record.result_ref is not None
    replay_store.replace_result(record.result_ref, {"value": "tampered"})
    with pytest.raises(IdempotencyResultMismatchError):
        await replay_executor.invoke(replayed.id, {"value": "duplicate"}, context)
    assert calls == 3


@pytest.mark.asyncio
async def test_external_execution_requires_durable_available_store_and_release() -> None:
    handler_calls = 0

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal handler_calls
        handler_calls += 1
        return payload

    capability = _external_capability()
    registry, _ = _external_registry(capability, handler)
    context = _external_context()
    with pytest.raises(IdempotencyStoreUnavailableError):
        await CapabilityExecutor(registry).invoke(capability.id, {}, context)
    with pytest.raises(IdempotencyStoreUnavailableError):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=f"sha256:{'3' * 64}",
        ).invoke(capability.id, {}, context)
    with pytest.raises(IdempotencyStoreUnavailableError, match="release provenance"):
        await CapabilityExecutor(
            registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=_AutoApprovalAdapter(),
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)

    class UnavailableStore(InMemoryIdempotencyStore):
        is_durable = True

        async def claim(self, *args: Any, **kwargs: Any) -> IdempotencyClaim:
            raise RuntimeError("unavailable")

    with pytest.raises(IdempotencyStoreUnavailableError, match="claim"):
        await CapabilityExecutor(
            registry,
            idempotency_store=UnavailableStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=f"sha256:{'3' * 64}",
        ).invoke(capability.id, {}, context)

    class StartUnavailableStore(InMemoryIdempotencyStore):
        is_durable = True

        async def mark_in_progress(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            raise RuntimeError("unavailable")

    start_store = StartUnavailableStore()
    start_approval = _AutoApprovalAdapter()
    start_executor = CapabilityExecutor(
        registry,
        idempotency_store=start_store,
        approval_adapter=start_approval,
        release_id=f"sha256:{'3' * 64}",
    )
    with pytest.raises(IdempotencyStoreUnavailableError, match="start"):
        await start_executor.invoke(capability.id, {}, context)
    start_record = next(iter(start_store._backend.records.values()))
    assert start_record.state == IdempotencyState.FAILED
    assert start_record.failure_code == "idempotency_store_unavailable"
    with pytest.raises(IdempotencyReconciliationRequiredError):
        await start_executor.invoke(capability.id, {}, context)
    assert start_approval.calls == 1
    assert handler_calls == 0


@pytest.mark.asyncio
async def test_durable_provider_failures_are_structured_before_or_after_effect() -> None:
    context = _external_context()
    release_id = f"sha256:{'6' * 64}"
    capability = _external_capability(replay=CompletedReplayMode.RETURN_RESULT)

    class ClaimConflictStore(InMemoryIdempotencyStore):
        is_durable = True

        async def claim(self, *args: Any, **kwargs: Any) -> IdempotencyClaim:
            raise IdempotencyConcurrencyError("claim conflict")

    registry, _ = _external_registry(capability, lambda payload: payload)
    with pytest.raises(IdempotencyConcurrencyError):
        await CapabilityExecutor(
            registry,
            idempotency_store=ClaimConflictStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        ).invoke(capability.id, {}, context)

    class MissingTokenStore(InMemoryIdempotencyStore):
        is_durable = True

        async def claim(self, *args: Any, **kwargs: Any) -> IdempotencyClaim:
            acquired = await super().claim(*args, **kwargs)
            return IdempotencyClaim.model_construct(
                disposition=ClaimDisposition.ACQUIRED,
                record=acquired.record,
                claim_token=None,
            )

    with pytest.raises(IdempotencyReconciliationRequiredError, match="ownership token"):
        await CapabilityExecutor(
            registry,
            idempotency_store=MissingTokenStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        ).invoke(capability.id, {}, context)

    class StartConflictStore(InMemoryIdempotencyStore):
        is_durable = True

        async def mark_in_progress(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            raise IdempotencyConcurrencyError("start conflict")

    start_conflict_store = StartConflictStore()
    with pytest.raises(IdempotencyConcurrencyError):
        await CapabilityExecutor(
            registry,
            idempotency_store=start_conflict_store,
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        ).invoke(capability.id, {}, context)
    assert next(iter(start_conflict_store._backend.records.values())).state == IdempotencyState.FAILED

    class StartAndFailureConflictStore(StartConflictStore):
        async def fail(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            raise IdempotencyConcurrencyError("failure conflict")

    with pytest.raises(IdempotencyReconciliationRequiredError) as unresolved:
        await CapabilityExecutor(
            registry,
            idempotency_store=StartAndFailureConflictStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        ).invoke(
            capability.id,
            {},
            context.model_copy(update={"idempotency_key": "start-fail-conflict"}),
        )
    assert isinstance(unresolved.value.__cause__, IdempotencyConcurrencyError)

    class StartUnavailableAndFailureConflictStore(InMemoryIdempotencyStore):
        is_durable = True

        async def mark_in_progress(
            self,
            *args: Any,
            **kwargs: Any,
        ) -> IdempotencyRecord:
            raise RuntimeError("unavailable")

        async def fail(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            raise IdempotencyConcurrencyError("failure conflict")

    with pytest.raises(IdempotencyReconciliationRequiredError) as unavailable:
        await CapabilityExecutor(
            registry,
            idempotency_store=StartUnavailableAndFailureConflictStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        ).invoke(
            capability.id,
            {},
            context.model_copy(update={"idempotency_key": "start-unavailable-fail-conflict"}),
        )
    assert isinstance(unavailable.value.__cause__, IdempotencyStoreUnavailableError)

    class CancelledStartStore(InMemoryIdempotencyStore):
        is_durable = True

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()

        async def mark_in_progress(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            self.entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    cancelled_start_store = CancelledStartStore()
    cancelled_start_executor = CapabilityExecutor(
        registry,
        idempotency_store=cancelled_start_store,
        approval_adapter=_AutoApprovalAdapter(),
        release_id=release_id,
    )
    cancelled_start = asyncio.create_task(
        cancelled_start_executor.invoke(
            capability.id,
            {},
            context.model_copy(update={"idempotency_key": "cancelled-start"}),
        )
    )
    await cancelled_start_store.entered.wait()
    cancelled_start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_start
    cancelled_start_record = next(iter(cancelled_start_store._backend.records.values()))
    assert cancelled_start_record.state == IdempotencyState.FAILED
    assert cancelled_start_record.failure_code == "start_transition_cancelled"

    timeout_capability = _external_capability(timeout_seconds=0.001)

    async def slow(_payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.1)
        return {}

    timeout_registry, _ = _external_registry(timeout_capability, slow)
    with pytest.raises(DeadlineExceededError):
        await CapabilityExecutor(
            timeout_registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(timeout_capability.id, {}, context)

    generic_registry, _ = _external_registry(
        capability,
        lambda _payload: (_ for _ in ()).throw(ValueError("secret")),
    )
    with pytest.raises(InvocationError) as invocation:
        await CapabilityExecutor(
            generic_registry,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)
    assert invocation.value.context["exception"] == "ValueError"

    class FailureCommitStore(InMemoryIdempotencyStore):
        is_durable = True

        async def fail(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            raise RuntimeError("failure commit unavailable")

    failure_registry, _ = _external_registry(
        capability,
        lambda _payload: (_ for _ in ()).throw(ContractError("rejected")),
    )
    with pytest.raises(IdempotencyReconciliationRequiredError, match="could not be durably recorded"):
        await CapabilityExecutor(
            failure_registry,
            idempotency_store=FailureCommitStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        ).invoke(capability.id, {}, context)

    class WrongCompletionStore(InMemoryIdempotencyStore):
        is_durable = True

        async def complete(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            completed = await super().complete(*args, **kwargs)
            return completed.model_copy(update={"result_hash": "f" * 64})

    with pytest.raises(IdempotencyResultMismatchError, match="completion record"):
        await CapabilityExecutor(
            registry,
            idempotency_store=WrongCompletionStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        ).invoke(capability.id, {}, context)


@pytest.mark.asyncio
async def test_executor_revalidates_every_durable_store_response() -> None:
    now = datetime(2026, 7, 24, tzinfo=UTC)
    release_id = f"sha256:{'9' * 64}"
    capability = _external_capability()
    registry, binding = _external_registry(capability, lambda payload: payload)
    context = _external_context()

    class NonClaimStore(InMemoryIdempotencyStore):
        is_durable = True

        async def claim(self, *args: Any, **kwargs: Any) -> IdempotencyClaim:
            return cast(IdempotencyClaim, {})

    with pytest.raises(IdempotencyReconciliationRequiredError, match="claim contract"):
        await CapabilityExecutor(
            registry,
            idempotency_store=NonClaimStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
            utcnow=lambda: now,
        ).invoke(capability.id, {}, context)

    class InvalidRecordStore(InMemoryIdempotencyStore):
        is_durable = True

        async def claim(self, *args: Any, **kwargs: Any) -> IdempotencyClaim:
            claim = await super().claim(*args, **kwargs)
            return claim.model_copy(update={"record": {}})

    with pytest.raises(IdempotencyReconciliationRequiredError, match="record contract"):
        await CapabilityExecutor(
            registry,
            idempotency_store=InvalidRecordStore(clock=lambda: now),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
            utcnow=lambda: now,
        ).invoke(capability.id, {}, context)

    class ClaimMutationStore(InMemoryIdempotencyStore):
        is_durable = True

        def __init__(
            self,
            disposition: ClaimDisposition,
            updates: dict[str, Any],
            *,
            token: str | None | object = ...,
        ) -> None:
            super().__init__(clock=lambda: now)
            self.disposition = disposition
            self.updates = updates
            self.token = token

        async def claim(self, *args: Any, **kwargs: Any) -> IdempotencyClaim:
            claim = await super().claim(*args, **kwargs)
            token = claim.claim_token if self.token is ... else cast(str | None, self.token)
            return claim.model_copy(
                update={
                    "disposition": self.disposition,
                    "record": claim.record.model_copy(update=self.updates),
                    "claim_token": token,
                }
            )

    wrong_key = _durable_key(binding, context).model_copy(update={"tenant_id": "tenant-b"})
    cases = (
        ClaimMutationStore(cast(ClaimDisposition, "invalid"), {}),
        ClaimMutationStore(ClaimDisposition.ACQUIRED, {"key": wrong_key}),
        ClaimMutationStore(ClaimDisposition.COMPLETED, {}, token=None),
        ClaimMutationStore(
            ClaimDisposition.ACQUIRED,
            {"lease_expires_at": now - timedelta(seconds=1)},
        ),
        ClaimMutationStore(
            ClaimDisposition.RECONCILIATION_REQUIRED,
            {},
            token=None,
        ),
        ClaimMutationStore(ClaimDisposition.ACQUIRED, {"actor_id": "actor-b"}),
        ClaimMutationStore(
            ClaimDisposition.ACQUIRED,
            {"lease_expires_at": now.replace(tzinfo=None)},
        ),
        ClaimMutationStore(ClaimDisposition.ACQUIRED, {"claim_token_hash": "f" * 64}),
        ClaimMutationStore(ClaimDisposition.IN_PROGRESS, {}),
    )
    for store in cases:
        with pytest.raises(IdempotencyReconciliationRequiredError):
            await CapabilityExecutor(
                registry,
                idempotency_store=store,
                approval_adapter=_AutoApprovalAdapter(),
                release_id=release_id,
                utcnow=lambda: now,
            ).invoke(capability.id, {}, context)

    class StartMutationStore(InMemoryIdempotencyStore):
        is_durable = True

        def __init__(self, updates: dict[str, Any]) -> None:
            super().__init__(clock=lambda: now)
            self.updates = updates

        async def mark_in_progress(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            started = await super().mark_in_progress(*args, **kwargs)
            return started.model_copy(update=self.updates)

    class InvalidStartStore(InMemoryIdempotencyStore):
        is_durable = True

        async def mark_in_progress(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            await super().mark_in_progress(*args, **kwargs)
            return cast(IdempotencyRecord, {})

    with pytest.raises(IdempotencyReconciliationRequiredError) as invalid_start:
        await CapabilityExecutor(
            registry,
            idempotency_store=InvalidStartStore(clock=lambda: now),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
            utcnow=lambda: now,
        ).invoke(capability.id, {}, context)
    assert isinstance(invalid_start.value.__cause__, IdempotencyReconciliationRequiredError)
    assert "transition contract" in str(invalid_start.value.__cause__)

    for start_updates in (
        {"actor_id": "actor-b"},
        {"lease_expires_at": now - timedelta(seconds=1)},
        {"reconciliation_required": True},
    ):
        with pytest.raises(IdempotencyReconciliationRequiredError):
            await CapabilityExecutor(
                registry,
                idempotency_store=StartMutationStore(start_updates),
                approval_adapter=_AutoApprovalAdapter(),
                release_id=release_id,
                utcnow=lambda: now,
            ).invoke(capability.id, {}, context)

    class CompletionMutationStore(InMemoryIdempotencyStore):
        is_durable = True

        def __init__(self, updates: dict[str, Any]) -> None:
            super().__init__(clock=lambda: now)
            self.updates = updates

        async def complete(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            completed = await super().complete(*args, **kwargs)
            return completed.model_copy(update=self.updates)

    for completion_updates in (
        {"release_id": f"sha256:{'0' * 64}"},
        {"completed_at": None},
        {"result_ref": None},
    ):
        with pytest.raises(IdempotencyReconciliationRequiredError):
            await CapabilityExecutor(
                registry,
                idempotency_store=CompletionMutationStore(completion_updates),
                approval_adapter=_AutoApprovalAdapter(),
                release_id=release_id,
                utcnow=lambda: now,
            ).invoke(capability.id, {}, context)

    class FailureMutationStore(InMemoryIdempotencyStore):
        is_durable = True

        def __init__(self, updates: dict[str, Any]) -> None:
            super().__init__(clock=lambda: now)
            self.updates = updates

        async def fail(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            failed = await super().fail(*args, **kwargs)
            return failed.model_copy(update=self.updates)

    class InvalidFailureStore(InMemoryIdempotencyStore):
        is_durable = True

        async def fail(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            await super().fail(*args, **kwargs)
            return cast(IdempotencyRecord, {})

    failure_registry, _ = _external_registry(
        capability,
        lambda _payload: (_ for _ in ()).throw(ContractError("rejected")),
    )
    with pytest.raises(IdempotencyReconciliationRequiredError, match="could not be durably recorded"):
        await CapabilityExecutor(
            failure_registry,
            idempotency_store=InvalidFailureStore(clock=lambda: now),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
            utcnow=lambda: now,
        ).invoke(capability.id, {}, context)
    for failure_updates in (
        {"reconciliation_required": False},
        {"failure_code": "wrong"},
        {"started_at": now + timedelta(seconds=1)},
        {"irreversible_started": True},
    ):
        with pytest.raises(IdempotencyReconciliationRequiredError, match="could not be durably recorded"):
            await CapabilityExecutor(
                failure_registry,
                idempotency_store=FailureMutationStore(failure_updates),
                approval_adapter=_AutoApprovalAdapter(),
                release_id=release_id,
                utcnow=lambda: now,
            ).invoke(capability.id, {}, context)


@pytest.mark.asyncio
async def test_durable_replay_rejects_corrupt_or_unavailable_results() -> None:
    context = _external_context()
    release_id = f"sha256:{'7' * 64}"
    capability = _external_capability(replay=CompletedReplayMode.RETURN_RESULT)
    registry, binding = _external_registry(capability, lambda payload: payload)
    key = _durable_key(binding, context)
    now = datetime(2026, 7, 24, tzinfo=UTC)
    malformed = IdempotencyRecord.model_construct(
        key=key,
        state=IdempotencyState.COMPLETED,
        version="3",
        claim_token_hash="a" * 64,
        lease_expires_at=now,
        actor_id="actor-a",
        release_id=release_id,
        claimed_at=now,
        started_at=now,
        completed_at=now,
        irreversible_started=False,
        result_hash=None,
        result_ref=None,
        failure_code=None,
        reconciliation_required=False,
    )

    class MalformedCompletedStore(InMemoryIdempotencyStore):
        is_durable = True

        async def claim(self, *args: Any, **kwargs: Any) -> IdempotencyClaim:
            return IdempotencyClaim.model_construct(
                disposition=ClaimDisposition.COMPLETED,
                record=malformed,
                claim_token=None,
            )

    with pytest.raises(IdempotencyReconciliationRequiredError, match="state invariants"):
        await CapabilityExecutor(
            registry,
            idempotency_store=MalformedCompletedStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        ).invoke(capability.id, {}, context)

    irreversible_store = InMemoryIdempotencyStore()
    irreversible_claim = await irreversible_store.claim(
        key,
        actor_id=context.principal_id,
        release_id=release_id,
        lease_seconds=60,
    )
    assert irreversible_claim.claim_token is not None
    irreversible_started = await irreversible_store.mark_in_progress(
        key,
        claim_token=irreversible_claim.claim_token,
        expected_version=irreversible_claim.record.version,
        irreversible=True,
        approval=_approval_provenance(),
    )
    irreversible_result = {"value": "must-not-replay"}
    await irreversible_store.complete(
        key,
        claim_token=irreversible_claim.claim_token,
        expected_version=irreversible_started.version,
        result=irreversible_result,
        result_hash=canonical_idempotency_digest(irreversible_result),
    )
    with pytest.raises(IdempotencyReconciliationRequiredError, match="irreversible"):
        await CapabilityExecutor(
            registry,
            idempotency_store=irreversible_store,
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
            allow_test_idempotency_store=True,
        ).invoke(capability.id, {}, context)

    class ResultStore(InMemoryIdempotencyStore):
        is_durable = True

        def __init__(self, *, structured: bool) -> None:
            super().__init__()
            self.structured = structured

        async def load_result(self, result_ref: str) -> dict[str, Any] | None:
            if self.structured:
                raise IdempotencyConcurrencyError("lookup conflict")
            raise RuntimeError("lookup unavailable")

    for structured, error_type in (
        (True, IdempotencyConcurrencyError),
        (False, IdempotencyStoreUnavailableError),
    ):
        store = ResultStore(structured=structured)
        executor = CapabilityExecutor(
            registry,
            idempotency_store=store,
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        )
        assert await executor.invoke(capability.id, {"value": 1}, context) == {"value": 1}
        with pytest.raises(error_type):
            await executor.invoke(capability.id, {"value": 2}, context)


@pytest.mark.asyncio
async def test_in_memory_store_rejects_invalid_state_transitions() -> None:
    now = datetime(2026, 7, 24, tzinfo=UTC)
    backend = InMemoryIdempotencyBackend()
    store = InMemoryIdempotencyStore(backend, clock=lambda: now)
    context = _external_context()
    _, binding = _external_registry(_external_capability(), lambda payload: payload)
    release_id = f"sha256:{'8' * 64}"

    mark_key = _durable_key(binding, context.model_copy(update={"idempotency_key": "mark"}))
    mark_claim = await store.claim(
        mark_key,
        actor_id="actor-a",
        release_id=release_id,
        lease_seconds=60,
    )
    assert mark_claim.claim_token is not None
    backend.records[mark_key.digest] = mark_claim.record.model_copy(
        update={"state": IdempotencyState.IN_PROGRESS, "started_at": now}
    )
    with pytest.raises(IdempotencyConcurrencyError, match="no longer claimable"):
        await store.mark_in_progress(
            mark_key,
            claim_token=mark_claim.claim_token,
            expected_version=mark_claim.record.version,
            irreversible=False,
        )

    complete_key = _durable_key(
        binding,
        context.model_copy(update={"idempotency_key": "complete"}),
    )
    complete_claim = await store.claim(
        complete_key,
        actor_id="actor-a",
        release_id=release_id,
        lease_seconds=60,
    )
    assert complete_claim.claim_token is not None
    with pytest.raises(IdempotencyConcurrencyError, match="not in progress"):
        await store.complete(
            complete_key,
            claim_token=complete_claim.claim_token,
            expected_version=complete_claim.record.version,
            result={},
            result_hash=canonical_idempotency_digest({}),
        )

    fail_key = _durable_key(binding, context.model_copy(update={"idempotency_key": "fail"}))
    fail_claim = await store.claim(
        fail_key,
        actor_id="actor-a",
        release_id=release_id,
        lease_seconds=60,
    )
    assert fail_claim.claim_token is not None
    backend.records[fail_key.digest] = fail_claim.record.model_copy(
        update={
            "state": IdempotencyState.COMPLETED,
            "started_at": now,
            "completed_at": now,
            "result_hash": "a" * 64,
            "result_ref": "memory://completed",
        }
    )
    with pytest.raises(IdempotencyConcurrencyError, match="transition to failed"):
        await store.fail(
            fail_key,
            claim_token=fail_claim.claim_token,
            expected_version=fail_claim.record.version,
            failure_code="late",
        )


@pytest.mark.asyncio
async def test_external_failure_cancellation_and_uncertain_commit_never_replay() -> None:
    context = _external_context()
    release_id = f"sha256:{'4' * 64}"
    retry_calls = 0

    async def retryable(_payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal retry_calls
        retry_calls += 1
        raise RetryableInvocationError("uncertain write")

    irreversible = _external_capability(operation=OperationClass.WRITE_IRREVERSIBLE)
    retry_registry, _ = _external_registry(irreversible, retryable)
    retry_store = InMemoryIdempotencyStore()
    retry_executor = CapabilityExecutor(
        retry_registry,
        idempotency_store=retry_store,
        approval_adapter=_AutoApprovalAdapter(),
        release_id=release_id,
        allow_test_idempotency_store=True,
    )
    with pytest.raises(RetryableInvocationError):
        await retry_executor.invoke(irreversible.id, {}, context)
    assert retry_calls == 1
    with pytest.raises(IdempotencyReconciliationRequiredError):
        await retry_executor.invoke(irreversible.id, {}, context)
    assert retry_calls == 1

    entered = asyncio.Event()

    async def cancelled(_payload: dict[str, Any]) -> dict[str, Any]:
        entered.set()
        await asyncio.Event().wait()
        return {}

    cancelled_registry, _ = _external_registry(_external_capability(), cancelled)
    cancelled_store = InMemoryIdempotencyStore()
    cancelled_executor = CapabilityExecutor(
        cancelled_registry,
        idempotency_store=cancelled_store,
        approval_adapter=_AutoApprovalAdapter(),
        release_id=release_id,
        allow_test_idempotency_store=True,
    )
    task = asyncio.create_task(
        cancelled_executor.invoke("write.external", {}, context.model_copy(update={"idempotency_key": "cancelled"}))
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    cancelled_record = next(iter(cancelled_store._backend.records.values()))
    assert cancelled_record.state == IdempotencyState.FAILED
    with pytest.raises(IdempotencyReconciliationRequiredError):
        await cancelled_executor.invoke(
            "write.external",
            {},
            context.model_copy(update={"idempotency_key": "cancelled"}),
        )

    class CancellationFailureStore(InMemoryIdempotencyStore):
        is_durable = True

        async def fail(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            raise RuntimeError("cannot persist cancellation")

    entered.clear()
    cancellation_failure_executor = CapabilityExecutor(
        cancelled_registry,
        idempotency_store=CancellationFailureStore(),
        approval_adapter=_AutoApprovalAdapter(),
        release_id=release_id,
    )
    failed_cancellation = asyncio.create_task(
        cancellation_failure_executor.invoke(
            "write.external",
            {},
            context.model_copy(update={"idempotency_key": "cancel-failure"}),
        )
    )
    await entered.wait()
    failed_cancellation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await failed_cancellation

    class CompleteUnavailableStore(InMemoryIdempotencyStore):
        is_durable = True

        async def complete(self, *args: Any, **kwargs: Any) -> IdempotencyRecord:
            raise RuntimeError("commit unavailable")

    complete_registry, _ = _external_registry(_external_capability(), handler=lambda payload: payload)
    with pytest.raises(IdempotencyReconciliationRequiredError, match="commit is uncertain"):
        await CapabilityExecutor(
            complete_registry,
            idempotency_store=CompleteUnavailableStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_id=release_id,
        ).invoke("write.external", {}, context.model_copy(update={"idempotency_key": "commit"}))


@pytest.mark.asyncio
async def test_stale_leases_require_reconciliation_and_local_harness_is_explicit() -> None:
    now = [datetime(2026, 7, 24, tzinfo=UTC)]
    backend = InMemoryIdempotencyBackend()
    store = InMemoryIdempotencyStore(backend, clock=lambda: now[0])
    capability = _external_capability(operation=OperationClass.WRITE_IRREVERSIBLE)
    calls = 0

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return payload

    registry, binding = _external_registry(capability, handler)
    context = _external_context()
    key = _durable_key(binding, context)
    release_id = f"sha256:{'5' * 64}"
    claim = await store.claim(
        key,
        actor_id=context.principal_id,
        release_id=release_id,
        lease_seconds=60,
    )
    assert claim.claim_token is not None
    started = await store.mark_in_progress(
        key,
        claim_token=claim.claim_token,
        expected_version=claim.record.version,
        irreversible=True,
        approval=_approval_provenance(),
    )
    assert started.irreversible_started is True
    executor = CapabilityExecutor(
        registry,
        idempotency_store=InMemoryIdempotencyStore(backend, clock=lambda: now[0]),
        approval_adapter=_AutoApprovalAdapter(),
        release_id=release_id,
        allow_test_idempotency_store=True,
    )
    with pytest.raises(IdempotencyInProgressError):
        await executor.invoke(capability.id, {}, context)
    now[0] += timedelta(seconds=61)
    with pytest.raises(IdempotencyReconciliationRequiredError):
        await executor.invoke(capability.id, {}, context)
    assert calls == 0

    local = LocalHarness(
        get_manifest("literature"),
        lambda _request: _evidence_response(),
        idempotency_store=store,
    )
    local_executor = local.capability_executor(registry)
    assert isinstance(local_executor, CapabilityExecutor)


@pytest.mark.asyncio
async def test_conversation_and_long_term_memory_enforce_tenant_boundary() -> None:
    store = InMemoryConversationStore()
    assert store.is_durable is False
    assert store.is_test_only is True
    record = ConversationRecord(tenant_id="tenant-a", session_id="session", state={"turn": 1})
    assert await store.load("tenant-a", "missing") is None
    await store.save(record)
    assert await store.load("tenant-a", "session") == record
    with pytest.raises(IsolationError):
        await store.load("tenant-b", "session")
    with pytest.raises(IsolationError):
        await store.save(ConversationRecord(tenant_id="tenant-b", session_id="session"))

    with pytest.raises(ValueError):
        InMemoryLongTermMemory(max_records=0)
    memory = InMemoryLongTermMemory(max_records=1)
    assert memory.is_durable is False
    assert memory.is_test_only is True
    confidential = MemoryRecord(
        tenant_id="tenant-a",
        principal_id="principal",
        memory_id="m0",
        content="secret",
        sensitivity=Sensitivity.CONFIDENTIAL,
    )
    with pytest.raises(IsolationError):
        await memory.remember(confidential, allowed_sensitivities=(Sensitivity.PUBLIC,))
    for memory_id in ("m1", "m2"):
        await memory.remember(
            MemoryRecord(
                tenant_id="tenant-a",
                principal_id="principal",
                memory_id=memory_id,
                content="safe",
                sensitivity=Sensitivity.INTERNAL,
            ),
            allowed_sensitivities=(Sensitivity.INTERNAL,),
        )
    recalled = await memory.recall("tenant-a", "principal")
    assert [item.memory_id for item in recalled] == ["m2"]
    assert await memory.recall("tenant-b", "principal") == ()


def _manifest_with_persistent_scope(scope: MemoryScope) -> AgentManifest:
    manifest = get_manifest("literature")
    scopes = tuple(
        item.model_copy(
            update={
                "enabled": True,
                "persistent": True,
                "provider_ref": f"app://memory/{scope.value}",
                "retention_days": 30,
                "ttl_seconds": 86_400,
                "read_roles": ("researchers",),
                "write_roles": ("researchers",),
            }
        )
        if item.scope == scope
        else item
        for item in manifest.memory.scopes
    )
    return manifest.model_copy(update={"memory": MemoryPolicy(scopes=scopes)})


def test_persistent_memory_requires_app_owned_durable_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DurableConversationStore(InMemoryConversationStore):
        is_durable = True

    class DurableLongTermMemory(InMemoryLongTermMemory):
        is_durable = True

    settings = _settings(
        model_deployment_name="gpt-5.6-sol",
        model_deployment_version="2026-07-09",
    )
    conversation_manifest = _manifest_with_persistent_scope(MemoryScope.CONVERSATION)
    conversation_factory = GovernedAgentFactory(conversation_manifest)
    with pytest.raises(ConfigurationError, match="conversation memory"):
        conversation_factory.build(
            client=_FakeChatClient(),
            settings=settings,
        )
    with pytest.raises(ConfigurationError, match="conversation memory"):
        conversation_factory.build(
            client=_FakeChatClient(),
            settings=settings,
            conversation_store=InMemoryConversationStore(),
        )
    assert (
        conversation_factory.readiness(
            settings,
            release_attestor=_release_attestor(conversation_manifest),
        )["persistent_memory"]
        is False
    )
    assert (
        conversation_factory.build(
            client=_FakeChatClient(),
            settings=settings,
            conversation_store=DurableConversationStore(),
            release_attestor=_release_attestor(conversation_manifest),
        ).name
        == conversation_manifest.name
    )

    user_manifest = _manifest_with_persistent_scope(MemoryScope.USER)
    user_factory = GovernedAgentFactory(user_manifest)
    with pytest.raises(ConfigurationError, match="long-term memory"):
        user_factory.build(
            client=_FakeChatClient(),
            settings=settings,
            release_attestor=_release_attestor(user_manifest),
        )
    assert (
        user_factory.build(
            client=_FakeChatClient(),
            settings=settings,
            long_term_memory_store=DurableLongTermMemory(),
            release_attestor=_release_attestor(user_manifest),
        ).name
        == user_manifest.name
    )

    coordinator_factory = importlib.import_module("coordinator.factory")
    monkeypatch.setattr(
        coordinator_factory,
        "FACTORY",
        GovernedAgentFactory(conversation_manifest),
    )
    with pytest.raises(ConfigurationError, match="conversation memory"):
        coordinator_factory.build_agent(
            settings=settings,
            invoker=cast(Any, lambda request: request),
        )
    monkeypatch.setattr(
        coordinator_factory,
        "FACTORY",
        GovernedAgentFactory(user_manifest),
    )
    with pytest.raises(ConfigurationError, match="long-term memory"):
        coordinator_factory.build_agent(
            settings=settings,
            invoker=cast(Any, lambda request: request),
        )


@pytest.mark.asyncio
async def test_contract_middleware_loads_and_saves_persistent_conversation() -> None:
    class DurableConversationStore(InMemoryConversationStore):
        is_durable = True

    manifest = _manifest_with_persistent_scope(MemoryScope.CONVERSATION)
    store = DurableConversationStore()
    await store.save(
        ConversationRecord(
            tenant_id="tenant-a",
            session_id="session-a",
            state={"turn": 1},
        )
    )
    request = LiteratureRequest.model_validate(_request())
    context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
    )

    async def call_next() -> None:
        assert context.session is not None
        assert context.session.state["turn"] == 1
        context.session.state["turn"] = 2

    await ContractMiddleware(
        manifest,
        None,
        conversation_store=store,
    ).process(context, call_next)
    saved = await store.load("tenant-a", "session-a")
    assert saved is not None and saved.state["turn"] == 2

    mismatched = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[
            Message(
                role="user",
                contents=[
                    LiteratureRequest.model_validate(
                        _request(session_id="session-new")
                    ).model_dump_json()
                ],
            )
        ],
        session=to_agent_session(
            ConversationRecord(
                tenant_id="tenant-a",
                session_id="other-session",
            )
        ),
    )
    with pytest.raises(ContractError, match="session"):
        await ContractMiddleware(
            manifest,
            None,
            conversation_store=store,
        ).process(mismatched, call_next)

    missing_store_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
    )
    with pytest.raises(ConfigurationError, match="not configured"):
        await ContractMiddleware(manifest, None).process(
            missing_store_context,
            call_next,
        )

    empty_store = DurableConversationStore()
    new_request = LiteratureRequest.model_validate(
        _request(session_id="session-new")
    )
    new_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[new_request.model_dump_json()])],
    )

    async def initialize_session() -> None:
        assert new_context.session is not None
        new_context.session.state["created"] = True

    await ContractMiddleware(
        manifest,
        None,
        conversation_store=empty_store,
    ).process(new_context, initialize_session)
    initialized = await empty_store.load("tenant-a", "session-new")
    assert initialized is not None and initialized.state["created"] is True

    existing_session_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[new_request.model_dump_json()])],
        session=to_agent_session(
            ConversationRecord(
                tenant_id="tenant-a",
                session_id="session-new",
            )
        ),
    )

    async def preserve_existing_session() -> None:
        assert existing_session_context.session is not None

    await ContractMiddleware(
        manifest,
        None,
        conversation_store=DurableConversationStore(),
    ).process(existing_session_context, preserve_existing_session)

    stream_store = DurableConversationStore()
    stream_request = LiteratureRequest.model_validate(
        _request(session_id="stream-session")
    )
    stream_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[stream_request.model_dump_json()])],
        stream=True,
    )

    async def stream_next() -> None:
        assert stream_context.session is not None
        stream_context.session.state["streamed"] = True

        async def updates() -> Any:
            yield AgentResponseUpdate(
                contents=[
                    Content.from_text(
                        text=LiteratureResponse(summary="streamed").model_dump_json()
                    )
                ],
                role="assistant",
            )

        stream_context.result = ResponseStream(
            updates(),
            finalizer=cast(
                Any,
                lambda items: AgentResponse.from_updates(
                    items,
                    output_format_type=LiteratureResponse,
                ),
            ),
        )

    await ContractMiddleware(
        manifest,
        None,
        conversation_store=stream_store,
    ).process(stream_context, stream_next)
    assert await stream_store.load("tenant-a", "stream-session") is None
    assert isinstance(stream_context.result, ResponseStream)
    _ = [update async for update in stream_context.result]
    streamed = await stream_store.load("tenant-a", "stream-session")
    assert streamed is not None and streamed.state["streamed"] is True

    failed_stream_store = DurableConversationStore()
    failed_stream_request = LiteratureRequest.model_validate(
        _request(session_id="failed-stream")
    )
    failed_stream_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[
            Message(
                role="user",
                contents=[failed_stream_request.model_dump_json()],
            )
        ],
        stream=True,
    )

    async def failed_stream_next() -> None:
        assert failed_stream_context.session is not None
        failed_stream_context.session.state["must_not_persist"] = True

        async def updates() -> Any:
            yield AgentResponseUpdate(
                contents=[Content.from_text(text='{"invalid":true}')],
                role="assistant",
            )

        failed_stream_context.result = ResponseStream(
            updates(),
            finalizer=lambda items: AgentResponse.from_updates(items),
        )

    await ContractMiddleware(
        manifest,
        None,
        conversation_store=failed_stream_store,
    ).process(failed_stream_context, failed_stream_next)
    assert isinstance(failed_stream_context.result, ResponseStream)
    with pytest.raises(ValidationError):
        _ = [update async for update in failed_stream_context.result]
    assert await failed_stream_store.load("tenant-a", "failed-stream") is None


def test_agent_session_round_trip_uses_ga_session_state() -> None:
    record = ConversationRecord(
        tenant_id="tenant",
        session_id="session",
        service_session_id="service",
        state={"history": ["message"]},
    )
    session = to_agent_session(record)
    assert session.session_id == "session"
    assert from_agent_session("tenant", session).state == record.state


def test_telemetry_redacts_secrets_content_and_complex_values() -> None:
    redacted = redact_attributes(
        {
            "authorization": "Bearer token",
            "user.query": "private query",
            "safe": "visible",
            "count": 2,
            "complex": {"not": "recorded"},
        },
        extra_fields=frozenset({"count"}),
    )
    assert redacted["authorization"].startswith("[REDACTED:")
    assert redacted["user.query"].startswith("[REDACTED:")
    assert redacted["count"].startswith("[REDACTED:")
    assert redacted["safe"] == "visible"
    assert redacted["complex"] == "dict"

    with pytest.raises(ValueError, match="immutable release"):
        ContractMiddleware(
            get_manifest("literature"),
            None,
            audit_sink=_RecordingAuditSink(),
        )


def test_opentelemetry_audit_sink_records_redacted_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attributes: dict[str, Any] = {}
    span_names: list[str] = []

    class Span:
        def set_attribute(self, key: str, value: Any) -> None:
            attributes[key] = value

    class SpanContext:
        def __enter__(self) -> Span:
            return Span()

        def __exit__(self, *_args: Any) -> None:
            return None

    class Tracer:
        def start_as_current_span(self, name: str) -> SpanContext:
            span_names.append(name)
            return SpanContext()

    monkeypatch.setattr("shared.telemetry.trace.get_tracer", lambda _name: Tracer())
    sink = OpenTelemetryGovernanceAuditSink()
    sink.emit(
        GovernanceAuditEvent(
            event_name="agent.invocation",
            outcome="completed",
            agent_id="literature",
            release_id=f"sha256:{'8' * 64}",
            tenant_digest="a" * 64,
            principal_digest="b" * 64,
        )
    )
    assert span_names == ["agent.invocation"]
    assert attributes["governance.agent_id"] == "literature"
    assert "governance.capability_id" not in attributes
    assert "governance.occurred_at" in attributes


class _RecordingAuditSink:
    def __init__(self) -> None:
        self.events: list[GovernanceAuditEvent] = []

    def emit(self, event: GovernanceAuditEvent) -> None:
        self.events.append(event)


@pytest.mark.asyncio
async def test_governance_audit_emits_only_hashed_structured_metadata() -> None:
    release_id = f"sha256:{'7' * 64}"
    request = LiteratureRequest.model_validate(
        _request(query="private research question")
    )
    sink = _RecordingAuditSink()
    context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
    )
    middleware = ContractMiddleware(
        get_manifest("literature"),
        None,
        release_id=release_id,
        audit_sink=sink,
    )

    async def call_next() -> None:
        return None

    await middleware.process(context, call_next)
    assert [event.outcome for event in sink.events] == ["accepted", "completed"]
    serialized = json.dumps(
        [event.model_dump(mode="json") for event in sink.events],
        sort_keys=True,
    )
    for raw in (
        "tenant-a",
        "principal-a",
        "private research question",
        request.model_dump_json(),
    ):
        assert raw not in serialized
    assert sink.events[0].tenant_digest == telemetry_identity_digest("tenant-a")
    assert sink.events[0].principal_digest == telemetry_identity_digest("principal-a")

    failed_sink = _RecordingAuditSink()
    failed_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
    )

    async def fail() -> None:
        raise RuntimeError("sensitive failure")

    with pytest.raises(RuntimeError, match="sensitive failure"):
        await ContractMiddleware(
            get_manifest("literature"),
            None,
            release_id=release_id,
            audit_sink=failed_sink,
        ).process(failed_context, fail)
    assert [event.outcome for event in failed_sink.events] == ["accepted", "failed"]
    assert failed_sink.events[-1].error_code is not None
    assert "sensitive failure" not in json.dumps(
        [event.model_dump(mode="json") for event in failed_sink.events]
    )

    stream_sink = _RecordingAuditSink()
    stream_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
        stream=True,
    )

    async def stream_next() -> None:
        async def failed_updates() -> Any:
            if False:
                yield AgentResponseUpdate()
            raise RuntimeError("sensitive stream failure")

        stream_context.result = ResponseStream(
            failed_updates(),
            finalizer=lambda updates: AgentResponse.from_updates(updates),
        )

    await ContractMiddleware(
        get_manifest("literature"),
        None,
        release_id=release_id,
        audit_sink=stream_sink,
    ).process(stream_context, stream_next)
    assert isinstance(stream_context.result, ResponseStream)
    with pytest.raises(RuntimeError, match="sensitive stream failure"):
        _ = [update async for update in stream_context.result]
    assert [event.outcome for event in stream_sink.events] == ["accepted", "failed"]

    capability = CapabilityDescriptor(
        id="public.audit",
        operation=OperationClass.READ,
    )
    registration, _ = _runtime_registration(_binding(capability.id))
    function_sink = _RecordingAuditSink()
    function_middleware = GovernedFunctionMiddleware(
        capability,
        registration,
        release_id=release_id,
        agent_id="literature",
        audit_sink=function_sink,
    )
    governance = InvocationContext(
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal-a",
    )
    function_context = FunctionInvocationContext(
        cast(Any, SimpleNamespace(name=registration.tool_name)),
        {},
        kwargs={
            "governance_context": governance.model_dump(mode="json"),
            "authorized_tool_evidence": [],
        },
    )

    async def function_call() -> None:
        function_context.result = {"ok": True}

    await function_middleware.process(function_context, function_call)
    assert [event.outcome for event in function_sink.events] == [
        "started",
        "completed",
    ]
    failing_function_context = FunctionInvocationContext(
        cast(Any, SimpleNamespace(name=registration.tool_name)),
        {},
        kwargs={
            "governance_context": governance.model_dump(mode="json"),
            "authorized_tool_evidence": [],
        },
    )

    async def fail_function() -> None:
        raise RuntimeError("sensitive handler failure")

    with pytest.raises(InvocationError):
        await function_middleware.process(failing_function_context, fail_function)
    assert function_sink.events[-1].outcome == "failed"
    assert function_sink.events[-1].error_code == InvocationError.code


def test_release_metadata_is_immutable_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = get_manifest("dataset")
    assert manifest_digest(manifest) == manifest_digest(manifest.model_copy())
    provider_adapter = _ManifestProviderAdapter(manifest.capability_bindings)
    registrations = (
        GovernedAgentFactory(manifest)
        .prepare(
            _settings(),
            provider_adapter=provider_adapter,
            **_trusted_scope(manifest),
        )
        .registrations
    )
    versions = {
        "agent-framework-core": "1.12.0",
        "agent-framework-foundry": "1.10.2",
        "agent-framework-foundry-hosting": "1.0.0b260721",
        "azure-ai-projects": "2.3.0",
        "openai": "2.46.0",
        "pydantic": "2.13.4",
    }

    def version(package: str) -> str:
        if package == "pydantic":
            raise importlib.metadata.PackageNotFoundError
        return versions[package]

    monkeypatch.setattr("shared.release.importlib.metadata.version", version)
    monkeypatch.setenv("GIT_COMMIT_SHA", "abc123")
    built_at = datetime(2026, 7, 22, tzinfo=UTC)
    release = build_release_metadata(
        manifest,
        model_deployment="gpt-5.4-mini",
        source_bundle_hash="a" * 64,
        parent_release_id=f"sha256:{'b' * 64}",
        built_at=built_at,
        registrations=registrations,
    )
    assert release.source_revision == "abc123"
    assert release.dependencies[-1] == ("pydantic", "not-installed")
    assert release.built_at == built_at
    assert release.release_id.startswith("sha256:")
    assert release.model_deployment == "gpt-5.4-mini"
    assert release.capability_versions == (("dataset.compute", "1.0.0"),)
    assert release.toolbox_versions == (("foundry.toolbox.code_interpreter", "1.0.0"),)
    assert len(release.contract_schema_digest) == 64
    assert release.idempotency_contract_schema_digest == idempotency_contract_schema_digest()
    assert release.approval_contract_schema_digest == approval_contract_schema_digest()
    assert release.dependency_risks[0].package == "agent-framework-foundry-hosting"
    assert release.dependency_risks[0].maturity == "beta"
    assert release.dependency_risks[0].version == "1.0.0b260721"
    assert release.dependency_risks[1].package == "agent-framework-core"
    assert release.dependency_risks[1].maturity == "preview"
    assert release.provider_contracts == (
        (
            "microsoft-foundry-toolbox",
            PROVIDER_CONTRACT_VERSION,
            PROVIDER_CONTRACT_SCHEMA_DIGEST,
        ),
    )
    assert release.provider_artifacts == (
        (
            "microsoft-foundry-toolbox",
            PROVIDER_CONTRACT_VERSION,
            PROVIDER_CONTRACT_ARTIFACT_DIGEST,
        ),
    )
    assert release.knowledge_versions == (("dataset.knowledge", "evidence-v2"),)
    assert release.runtime_kind == "custom"
    assert release.input_schema_hash == manifest.input_schema.sha256
    with pytest.raises(ValidationError):
        release.agent_name = "changed"
    explicit = build_release_metadata(
        manifest,
        model_deployment="gpt-5.4-mini",
        source_revision="explicit",
        source_bundle_hash="a" * 64,
        built_at=built_at,
        registrations=registrations,
    )
    assert explicit.source_revision == "explicit"
    same_content = build_release_metadata(
        manifest,
        model_deployment="gpt-5.4-mini",
        source_bundle_hash="a" * 64,
        parent_release_id=f"sha256:{'b' * 64}",
        built_at=datetime(2026, 7, 23, tzinfo=UTC),
        registrations=registrations,
    )
    assert same_content.release_id == release.release_id
    with pytest.raises(ValueError, match="model policy"):
        build_release_metadata(manifest, model_deployment="unapproved-model")
    with pytest.raises(ConfigurationError, match="attested provider registrations"):
        build_release_metadata(
            manifest,
            model_deployment=manifest.model_policy.deployment_name,
        )
    unattested = tuple(
        ToolRegistration(
            binding=binding,
            tool_name=binding.operation_ref.id.rsplit(".", 1)[-1],
            handler=lambda payload: payload,
            current_instance_fingerprint=binding.instance_ref.fingerprint,
        )
        for binding in manifest.capability_bindings
    )
    with pytest.raises(ConfigurationError, match="attested provider registrations"):
        build_release_metadata(
            manifest,
            model_deployment=manifest.model_policy.deployment_name,
            registrations=unattested,
        )
    versions["agent-framework-foundry-hosting"] = "1.0.0"
    with pytest.raises(ConfigurationError, match="reviewed beta package pin"):
        build_release_metadata(
            manifest,
            model_deployment=manifest.model_policy.deployment_name,
            registrations=registrations,
        )
    versions["agent-framework-foundry-hosting"] = "1.0.0b260721"

    root = tmp_path / "bundle"
    root.mkdir()
    (root / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "ignored.md").write_text("ignored", encoding="utf-8")
    first_hash = source_bundle_digest(root)
    (root / "agent.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert source_bundle_digest(root) != first_hash


def test_release_attestation_is_exact_objective_and_fail_closed() -> None:
    manifest = get_manifest("literature")
    release = GovernedAgentFactory(manifest).release(
        _settings(
            model_deployment_name=manifest.model_policy.deployment_name,
            model_deployment_version=manifest.model_policy.pinned_model_version,
        )
    )
    local_attestor = InMemoryReleaseAttestor(
        manifest.evaluation.objective_hard_gates
    )
    with pytest.raises(ReleaseAttestationError, match="app-owned durable"):
        validate_release_attestation(release, manifest, None)
    with pytest.raises(ReleaseAttestationError, match="app-owned durable"):
        validate_release_attestation(release, manifest, local_attestor)

    valid = validate_release_attestation(
        release,
        manifest,
        local_attestor,
        allow_test_attestor=True,
    )
    assert valid.release_id == release.release_id
    assert tuple(item.gate for item in valid.objective_gates) == tuple(
        sorted(manifest.evaluation.objective_hard_gates)
    )
    assert manifest.evaluation.evaluator_results_advisory is True
    assert (
        valid.release_attestation_contract_schema_digest
        == release_attestation_contract_schema_digest()
        == release.release_attestation_contract_schema_digest
    )
    valid_payload = valid.model_dump(mode="json")
    with pytest.raises(ValidationError, match="sorted and unique"):
        type(valid).model_validate(
            {
                **valid_payload,
                "objective_gates": (
                    valid_payload["objective_gates"][0],
                    valid_payload["objective_gates"][0],
                ),
            }
        )
    with pytest.raises(ValidationError, match="provider contracts"):
        type(valid).model_validate(
            {
                **valid_payload,
                "provider_contracts": (("z", "1", "4" * 64), ("a", "1", "5" * 64)),
            }
        )
    with pytest.raises(ValidationError, match="provider artifacts"):
        type(valid).model_validate(
            {
                **valid_payload,
                "provider_artifacts": (
                    ("z", "1", "4" * 64),
                    ("a", "1", "5" * 64),
                ),
            }
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        type(valid).model_validate(
            {
                **valid_payload,
                "issued_at": valid.issued_at.replace(tzinfo=None),
            }
        )
    with pytest.raises(ValidationError, match="expires before"):
        type(valid).model_validate(
            {
                **valid_payload,
                "expires_at": valid.issued_at - timedelta(seconds=1),
            }
        )

    class Attestor:
        is_durable = True

        def __init__(self, result: Any) -> None:
            self.result = result

        def attest(self, _release: Any) -> Any:
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

    mismatches = (
        valid.model_copy(update={"release_id": f"sha256:{'0' * 64}"}),
        valid.model_copy(update={"manifest_digest": "0" * 64}),
        valid.model_copy(update={"contract_schema_digest": "0" * 64}),
        valid.model_copy(update={"idempotency_contract_schema_digest": "0" * 64}),
        valid.model_copy(update={"approval_contract_schema_digest": "0" * 64}),
        valid.model_copy(update={"release_attestation_contract_schema_digest": "0" * 64}),
        valid.model_copy(update={"source_bundle_hash": "0" * 64}),
        valid.model_copy(update={"model_deployment_ref": "app://model/other"}),
        valid.model_copy(update={"model_version": "other"}),
        valid.model_copy(update={"provider_contracts": (("other", "v1", "6" * 64),)}),
        valid.model_copy(
            update={
                "provider_artifacts": (
                    ("other", "provider-v2", "7" * 64),
                )
            }
        ),
        valid.model_copy(update={"objective_gates": ()}),
    )
    for mismatch in mismatches:
        with pytest.raises(ReleaseAttestationError, match="immutable release"):
            validate_release_attestation(release, manifest, Attestor(mismatch))

    now = datetime.now(UTC)
    for inactive in (
        valid.model_copy(update={"status": ReleaseAttestationStatus.REVOKED}),
        valid.model_copy(
            update={
                "issued_at": now - timedelta(seconds=2),
                "expires_at": now - timedelta(seconds=1),
            }
        ),
        valid.model_copy(
            update={
                "issued_at": now + timedelta(minutes=2),
                "expires_at": now + timedelta(minutes=3),
            }
        ),
    ):
        with pytest.raises(ReleaseAttestationError, match="revoked or expired"):
            validate_release_attestation(
                release,
                manifest,
                Attestor(inactive),
                now=now,
            )
    with pytest.raises(ReleaseAttestationError, match="invalid attestation"):
        validate_release_attestation(release, manifest, Attestor({"invalid": True}))
    with pytest.raises(ReleaseAttestationError, match="invalid attestation"):
        validate_release_attestation(
            release,
            manifest,
            Attestor(RuntimeError("backend unavailable")),
        )
    attestor_error = ReleaseAttestationError("attestor policy denied")
    with pytest.raises(ReleaseAttestationError, match="attestor policy denied"):
        validate_release_attestation(
            release,
            manifest,
            Attestor(attestor_error),
        )


def test_capability_catalog_has_deterministic_risk_boundaries() -> None:
    toolbox = _settings(toolbox_endpoint="https://toolbox.example/toolboxes/research/mcp")
    dataset = capabilities_for_manifest(get_manifest("dataset"), toolbox)
    assert dataset[0].operation == OperationClass.PRIVILEGED
    assert dataset[0].approval == ApprovalMode.REQUIRED
    assert dataset[0].allowed_destinations == ("toolbox.example",)
    assert dataset[0].side_effect_destinations == ("toolbox.example",)
    online = capabilities_for_manifest(get_manifest("literature_online"), None)
    assert online[0].operation == OperationClass.READ
    assert online[0].allowed_destinations == ()
    coordinator = capabilities_for_manifest(get_manifest("coordinator"))
    assert "literature-agent" in coordinator[0].allowed_destinations


def test_factory_requires_current_provider_attestation() -> None:
    factory = get_factory("dataset")
    settings = _settings(toolbox_endpoint="https://toolbox.example/toolboxes/dataset/mcp")
    with pytest.raises(ConfigurationError, match="attested provider adapter"):
        factory.resolved_manifest(settings)
    assert factory.readiness(settings)["ready"] is False

    adapter = _ManifestProviderAdapter(factory.manifest.capability_bindings)
    with pytest.raises(ConfigurationError, match="trusted tenant and project"):
        factory.prepare(settings, provider_adapter=adapter)
    prepared = factory.prepare(
        settings,
        provider_adapter=adapter,
        **_trusted_scope(factory.manifest),
    )
    assert prepared.manifest is factory.manifest
    assert all(registration.runtime_attested for registration in prepared.registrations)
    assert adapter.handler_resolutions == len(factory.manifest.capability_bindings)
    assert (
        factory.readiness(
            settings,
            provider_adapter=adapter,
            **_trusted_scope(factory.manifest),
        )["ready"]
        is False
    )

    class DurableStore(InMemoryIdempotencyStore):
        is_durable = True

    assert (
        factory.readiness(
            settings,
            provider_adapter=adapter,
            **_trusted_scope(factory.manifest),
            idempotency_store=DurableStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_attestor=_release_attestor(factory.manifest),
        )["ready"]
        is True
    )
    with pytest.raises(ConfigurationError, match="app-owned durable idempotency store"):
        factory.build(
            client=_FakeChatClient(),
            settings=settings,
            provider_adapter=adapter,
            **_trusted_scope(factory.manifest),
        )
    with pytest.raises(ConfigurationError, match="app-owned durable approval adapter"):
        factory.build(
            client=_FakeChatClient(),
            settings=settings,
            provider_adapter=adapter,
            **_trusted_scope(factory.manifest),
            idempotency_store=DurableStore(),
        )

    binding = factory.manifest.capability_bindings[0]
    key = (binding.instance_ref.provider_id, binding.instance_ref.instance_id)
    adapter.attestations[key] = adapter.attestations[key].model_copy(
        update={"instance_ref": binding.instance_ref.model_copy(update={"fingerprint": "f" * 64})}
    )
    with pytest.raises(StaleCapabilityBindingError):
        factory.resolved_manifest(
            settings,
            provider_adapter=adapter,
            **_trusted_scope(factory.manifest),
        )


class _FakeChatClient:
    def get_web_search_tool(self, **kwargs: Any) -> dict[str, Any]:
        return {"kind": "web", **kwargs}


def test_governed_factory_builds_typed_hosted_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = GovernedAgentFactory(get_manifest("literature"))
    literature_settings = _settings(
        model_deployment_name="gpt-5.6-sol",
        model_deployment_version="2026-07-09",
    )
    agent = factory.build(
        client=_FakeChatClient(),
        settings=literature_settings,
        release_attestor=_release_attestor(factory.manifest),
    )
    assert agent.name == "literature-agent"
    assert agent.default_options["store"] is False
    assert agent.default_options["response_format"] is LiteratureResponse
    assert agent.context_providers == []
    assert factory.capabilities() == ()
    constructed: list[dict[str, Any]] = []
    with monkeypatch.context() as context:
        context.setattr(
            "shared.factory.Agent",
            lambda **kwargs: constructed.append(kwargs),
        )
        with pytest.raises(ReleaseAttestationError, match="release attestor"):
            factory.build(
                client=_FakeChatClient(),
                settings=literature_settings,
            )
    assert constructed == []
    assert (
        factory.readiness(
            literature_settings,
            release_attestor=_release_attestor(factory.manifest),
        )["ready"]
        is True
    )
    assert get_factory("dataset").manifest.id == "dataset"

    marker = object()
    monkeypatch.setattr("shared.factory._build_foundry_client", lambda _settings: marker)
    built = factory.build(
        settings=literature_settings,
        release_attestor=_release_attestor(factory.manifest),
    )
    assert built.client is marker
    with pytest.raises(ConfigurationError, match="model deployment"):
        factory.build(settings=_settings(model_deployment_name="gpt-5.4-mini"))
    with pytest.raises(ConfigurationError, match="model version"):
        factory.build(
            settings=_settings(
                model_deployment_name="gpt-5.6-sol",
                model_deployment_version="wrong",
            )
        )
    with pytest.raises(ConfigurationError, match="explicit governed runtime settings"):
        factory.build(client=_FakeChatClient())
    release = factory.release(literature_settings)
    assert release.model_deployment == "gpt-5.6-sol"
    assert release.model_version == "2026-07-09"
    assert factory.readiness(_settings())["ready"] is False

    class Deployments:
        model_version: str | None = "2026-07-09"

        def get(self, name: str) -> Any:
            assert name == "gpt-5.6-sol"
            return SimpleNamespace(model_version=self.model_version)

    class Project:
        deployments = Deployments()

        def __enter__(self) -> Project:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

    monkeypatch.setattr("shared.factory.AIProjectClient", lambda **_kwargs: Project())
    discovered = literature_settings.model_copy(update={"model_deployment_version": None})
    assert factory.resolved_manifest(discovered).id == "literature"
    Project.deployments.model_version = None
    with pytest.raises(ConfigurationError, match="versioned Foundry model"):
        factory.resolved_manifest(discovered)


def test_foundry_client_factory_and_credentials_are_managed_identity_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import credentials
    from shared import factory as factory_module

    managed: list[str | None] = []
    defaults: list[bool] = []

    def managed_credential(client_id: str | None = None) -> tuple[str, str | None]:
        managed.append(client_id)
        return ("managed", client_id)

    def default_credential() -> str:
        defaults.append(True)
        return "default"

    monkeypatch.setattr(credentials, "ManagedIdentityCredential", managed_credential)
    monkeypatch.setattr(credentials, "DefaultAzureCredential", default_credential)
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.delenv("IDENTITY_ENDPOINT", raising=False)
    monkeypatch.delenv("MSI_ENDPOINT", raising=False)
    get_credential = cast(Any, credentials.get_credential)
    assert get_credential() == "default"
    assert get_credential("explicit") == ("managed", "explicit")
    monkeypatch.setenv("AZURE_CLIENT_ID", "environment")
    assert get_credential() == ("managed", "environment")
    monkeypatch.delenv("AZURE_CLIENT_ID")
    monkeypatch.setenv("IDENTITY_ENDPOINT", "identity")
    assert get_credential() == ("managed", None)
    monkeypatch.delenv("IDENTITY_ENDPOINT")
    monkeypatch.setenv("MSI_ENDPOINT", "msi")
    assert get_credential() == ("managed", None)
    assert defaults == [True]

    captured: dict[str, Any] = {}
    monkeypatch.setattr(factory_module, "get_credential", lambda client_id: ("credential", client_id))
    monkeypatch.setattr(
        factory_module,
        "FoundryChatClient",
        lambda **kwargs: captured.update(kwargs) or "client",
    )
    build_foundry_client = cast(Any, factory_module._build_foundry_client)
    assert build_foundry_client(_settings(managed_identity_client_id="client-id")) == "client"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["credential"] == ("credential", "client-id")


def test_runtime_adapter_builds_describes_and_runs_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shared import runtime

    marker = object()
    factory = SimpleNamespace(build=lambda **kwargs: (marker, kwargs))
    monkeypatch.setattr(runtime, "get_factory", lambda profile_id: factory)
    build_agent = cast(Any, runtime.build_agent)
    assert build_agent("literature", client="client") == (
        marker,
        {
            "client": "client",
            "settings": None,
            "provider_adapter": None,
            "trusted_tenant_id": None,
            "trusted_project_id": None,
            "idempotency_store": None,
            "approval_adapter": None,
            "release_attestor": None,
            "conversation_store": None,
            "long_term_memory_store": None,
            "audit_sink": None,
            "allow_test_idempotency_store": False,
            "allow_test_approval_adapter": False,
            "allow_test_release_attestor": False,
        },
    )
    assert runtime.describe_profile("grant").id == "grant"

    calls: list[Any] = []

    class Host:
        def __init__(self, agent: Any) -> None:
            calls.append(agent)

        def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr(runtime, "ResponsesHostServer", Host)
    monkeypatch.setattr(
        runtime,
        "build_agent",
        lambda profile_id, **_kwargs: f"agent:{profile_id}",
    )
    runtime.run_profile("dataset")
    assert calls == ["agent:dataset", "run"]


def test_tools_are_bounded_to_profile_and_configured_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert delegated_agent_name("literature", "public") == "literature-online-agent"
    assert delegated_agent_name("literature", "internal") == "literature-agent"
    assert delegated_agent_name("invalid", "public") is None
    assert delegated_agent_name("literature", "invalid") is None
    assert tools_for_profile(get_manifest("coordinator"), _FakeChatClient()) == []
    for profile_id in ("dataset", "literature_online"):
        with pytest.raises(ConfigurationError, match="Toolbox"):
            tools_for_profile(get_manifest(profile_id), _FakeChatClient())
    marker = object()
    monkeypatch.setattr("shared.tools.get_credential", lambda _client_id=None: object())
    monkeypatch.setattr("shared.tools.FoundryToolbox", lambda *_args, **_kwargs: marker)
    configured = tools_for_profile(
        get_manifest("grant_online"),
        _FakeChatClient(),
        _settings(
            managed_identity_client_id="client",
            toolbox_endpoint="https://toolbox.example/toolboxes/grant/mcp",
            default_timeout_seconds=15,
        ),
    )
    assert configured is marker
    assert (
        tools_for_profile(
            get_manifest("dataset"),
            _FakeChatClient(),
            _settings(toolbox_endpoint="https://toolbox.example/toolboxes/dataset/mcp"),
        )
        is marker
    )
    dataset_factory = get_factory("dataset")
    dataset_adapter = _ManifestProviderAdapter(dataset_factory.manifest.capability_bindings)
    assert dataset_factory.readiness(_settings())["ready"] is False
    assert (
        dataset_factory.readiness(
            _settings(toolbox_endpoint="https://toolbox.example/toolboxes/dataset/mcp"),
            provider_adapter=dataset_adapter,
            **_trusted_scope(dataset_factory.manifest),
            idempotency_store=type(
                "DurableStore",
                (InMemoryIdempotencyStore,),
                {"is_durable": True},
            )(),
            approval_adapter=_AutoApprovalAdapter(),
            release_attestor=_release_attestor(dataset_factory.manifest),
        )["ready"]
        is True
    )
    with pytest.raises(ConfigurationError, match="Toolbox"):
        dataset_factory.build(client=_FakeChatClient(), settings=_settings())

    class ResponsesInvoker:
        def invoke(self, client: Any, request: str, agent_name: str) -> HostedAgentReply:
            assert (client, request, agent_name) == ("client", "request", "agent")
            return HostedAgentReply(agent_name=agent_name, content="done")

    monkeypatch.setattr("shared.tools.RetryingResponsesInvoker", ResponsesInvoker)
    assert _invoke_specialist("client", "request", "agent") == "done"


class _Responses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _OpenAIClient:
    def __init__(self, responses: _Responses) -> None:
        self.responses = responses
        self.max_retries: int | None = None

    def with_options(self, *, max_retries: int) -> _OpenAIClient:
        self.max_retries = max_retries
        return self


def _status_error(status: int, body: Any) -> APIStatusError:
    return APIStatusError(
        "failed",
        response=httpx.Response(
            status,
            request=httpx.Request("POST", "https://example.test/responses"),
        ),
        body=body,
    )


def test_hosted_invoker_retries_session_and_empty_output() -> None:
    sleeps: list[float] = []
    invoker = RetryingResponsesInvoker(
        HostedInvocationPolicy(
            session_retry_delays=(1,),
            empty_output_retry_delays=(2,),
            timeout_seconds=10,
        ),
        sleep=sleeps.append,
        monotonic=lambda: 0,
    )
    responses = _Responses(
        [
            _status_error(424, {"error": {"code": "session_not_ready"}}),
            SimpleNamespace(output_text=" ", id="empty"),
            SimpleNamespace(output_text=" done ", id="response"),
        ]
    )
    client = _OpenAIClient(responses)
    reply = invoker.invoke(client, "request", "agent")
    assert reply.content == "done"
    assert reply.response_id == "response"
    assert sleeps == [1, 2]
    assert responses.calls[0] == {"input": "request", "timeout": 10}
    assert client.max_retries == 0


def test_hosted_invoker_structured_failure_paths() -> None:
    def context(**kwargs: Any) -> Any:
        responses = kwargs.pop("responses", None)
        if responses is not None:
            assert not kwargs
            return _OpenAIClient(responses)
        return SimpleNamespace(**kwargs)

    invoker = RetryingResponsesInvoker(
        HostedInvocationPolicy(
            session_retry_delays=(),
            empty_output_retry_delays=(),
            timeout_seconds=10,
        ),
        monotonic=lambda: 0,
    )
    with pytest.raises(InvocationError, match="bounded SDK retries"):
        invoker.invoke(
            SimpleNamespace(responses=_Responses([])),
            "request",
            "agent",
        )
    with pytest.raises(InvocationError, match="failed"):
        invoker.invoke(
            context(responses=_Responses([_status_error(500, {})])),
            "request",
            "agent",
        )
    with pytest.raises(RetryableInvocationError, match="ready"):
        invoker.invoke(
            context(responses=_Responses([_status_error(424, {"error": {"code": "session_not_ready"}})])),
            "request",
            "agent",
        )
    with pytest.raises(InvocationError, match="no output"):
        invoker.invoke(
            context(responses=_Responses([context(output_text=None)])),
            "request",
            "agent",
        )
    expired = RetryingResponsesInvoker(monotonic=lambda: 10)
    with pytest.raises(DeadlineExceededError, match="expired"):
        expired.invoke(
            context(responses=_Responses([])),
            "request",
            "agent",
            deadline_monotonic=5,
        )
    bounded = RetryingResponsesInvoker(
        HostedInvocationPolicy(session_retry_delays=(5,), timeout_seconds=10),
        monotonic=lambda: 6,
    )
    with pytest.raises(DeadlineExceededError, match="retry"):
        bounded.invoke(
            context(responses=_Responses([_status_error(424, {"error": {"code": "session_not_ready"}})])),
            "request",
            "agent",
            deadline_monotonic=10,
        )
    times = iter((0.0, 0.0, 11.0))
    exceeded = RetryingResponsesInvoker(
        HostedInvocationPolicy(timeout_seconds=10),
        monotonic=lambda: next(times),
    )
    with pytest.raises(DeadlineExceededError, match="exceeded"):
        exceeded.invoke(
            context(responses=_Responses([context(output_text="late")])),
            "request",
            "agent",
        )


@pytest.mark.asyncio
async def test_contract_middleware_validates_and_redacts_hosted_envelopes() -> None:
    settings = _settings(
        toolbox_endpoint="https://toolbox.example/toolboxes/public/mcp",
        default_timeout_seconds=15,
    )
    middleware = ContractMiddleware(
        get_manifest("literature_online"),
        settings,
        monotonic=lambda: 10,
    )
    request = PublicLiteratureRequest.model_validate(_request(sensitivity="public", query="Find public evidence"))
    context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
    )
    calls: list[bool] = []

    async def call_next() -> None:
        calls.append(True)

    await middleware.process(context, call_next)
    assert calls == [True]
    assert "principal-a" not in context.messages[-1].text
    assert "Find public evidence" in context.messages[-1].text
    governance = InvocationContext.model_validate(context.function_invocation_kwargs["governance_context"])
    assert governance.scopes == frozenset({"research.public.read"})
    assert governance.destination == "toolbox.example"
    assert governance.deadline_monotonic == 25

    dataset = DatasetRequest.model_validate(
        _request(
            dataset_id="dataset.csv",
            approval_decision_id="approval-a",
            invocation_id="invocation-a",
            idempotency_key="dataset-operation-1",
        )
    )
    dataset_context = ContractMiddleware(
        get_manifest("dataset"),
        None,
        monotonic=lambda: 5,
    )._invocation_context(dataset)
    assert dataset_context.approval_decision_id == "approval-a"
    assert dataset_context.invocation_id == "invocation-a"
    assert dataset_context.scopes == frozenset()
    assert dataset_context.idempotency_key == "dataset-operation-1"
    assert dataset_context.deadline_monotonic == 65
    unapproved = DatasetRequest.model_validate(_request(dataset_id="dataset.csv"))
    unapproved_context = ContractMiddleware(
        get_manifest("dataset"),
        None,
    )._invocation_context(unapproved)
    assert unapproved_context.approval_decision_id is None
    assert unapproved_context.invocation_id is None
    with pytest.raises(ValueError, match="supplied together"):
        ContractMiddleware(
            get_manifest("literature"),
            None,
            trusted_tenant_id="tenant-a",
        )
    scoped = ContractMiddleware(
        get_manifest("literature"),
        None,
        trusted_tenant_id="tenant-a",
        trusted_project_id="project-a",
    )
    with pytest.raises(IsolationError, match="authenticated Hosted Agent scope"):
        scoped._invocation_context(
            LiteratureRequest.model_validate(
                _request(tenant_id="tenant-b", project_id="project-a")
            )
        )


@pytest.mark.asyncio
async def test_contract_middleware_resolves_only_runtime_authorized_evidence() -> None:
    canonical = EvidenceRef(
        evidence_id="ev-1",
        source_uri="app://authorized/source",
    )
    request = LiteratureRequest.model_validate(_request(evidence=[canonical.model_dump(mode="json")]))
    spoofed = LiteratureResponse(
        summary="result",
        claims=(
            Claim(
                text="claim",
                support=SupportStatus.SUPPORTED,
                evidence_ids=("ev-1",),
            ),
        ),
        evidence=(
            EvidenceRef(
                evidence_id="ev-1",
                source_uri="https://attacker.example",
            ),
        ),
    )
    context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
    )

    async def call_next() -> None:
        context.result = cast(
            Any,
            AgentResponse[LiteratureResponse](
                messages=[
                    Message(
                        role="assistant",
                        contents=[spoofed.model_dump_json()],
                    ),
                    Message(role="tool", contents=["tool result"]),
                ],
                value=spoofed,
            ),
        )

    await ContractMiddleware(get_manifest("literature"), None).process(
        context,
        call_next,
    )
    assert isinstance(context.result, AgentResponse)
    assert isinstance(context.result.value, LiteratureResponse)
    assert context.result.value.evidence == (canonical,)
    assert "attacker.example" not in context.result.text

    fabricated = spoofed.model_copy(
        update={
            "claims": (
                Claim(
                    text="fabricated",
                    support=SupportStatus.SUPPORTED,
                    evidence_ids=("made-up",),
                ),
            ),
            "evidence": (EvidenceRef(evidence_id="made-up"),),
        }
    )
    stream_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
        stream=True,
    )

    async def stream_next() -> None:
        async def updates() -> Any:
            yield AgentResponseUpdate(
                contents=[Content.from_text(text=fabricated.model_dump_json())],
                role="assistant",
            )

        stream_context.result = ResponseStream(
            updates(),
            finalizer=lambda items: AgentResponse.from_updates(
                items,
                output_format_type=LiteratureResponse,
            ),
        )

    await ContractMiddleware(get_manifest("literature"), None).process(
        stream_context,
        stream_next,
    )
    assert isinstance(stream_context.result, ResponseStream)
    governed_updates = [item async for item in stream_context.result]
    assert len(governed_updates) == 1
    assert "made-up" not in governed_updates[0].text
    normalized = await stream_context.result.get_final_response()
    assert isinstance(normalized.value, LiteratureResponse)
    assert normalized.value.claims[0].support == SupportStatus.UNSUPPORTED
    invalid_stream_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
        stream=True,
    )
    with pytest.raises(ContractError, match="response stream"):
        await ContractMiddleware(get_manifest("literature"), None).process(
            invalid_stream_context,
            stream_next,
        )

    middleware = ContractMiddleware(get_manifest("literature"), None)
    parsed_from_text = middleware._normalize_response(
        AgentResponse(
            messages=[
                Message(
                    role="assistant",
                    contents=[spoofed.model_dump_json()],
                )
            ]
        ),
        (canonical,),
        [],
    )
    assert parsed_from_text.value.evidence == (canonical,)
    appended = middleware._normalize_response(
        AgentResponse(value=spoofed),
        (canonical,),
        [],
    )
    assert appended.messages


@pytest.mark.asyncio
async def test_contract_middleware_accepts_only_trusted_runtime_evidence() -> None:
    capability = CapabilityDescriptor(
        id="literature.lookup",
        operation=OperationClass.READ,
    )
    base_binding = _binding(capability.id)
    binding = base_binding.model_copy(
        update={"operation_ref": base_binding.operation_ref.model_copy(update={"id": "local.searchLiteratureMetadata"})}
    )
    registration, _ = _runtime_registration(binding)
    function_middleware = GovernedFunctionMiddleware(
        capability,
        registration,
        allowed_connector_sources=frozenset({"pubmed"}),
    )
    request = LiteratureRequest.model_validate(_request())
    tool_result = {
        "source": "pubmed",
        "query": "governed agents",
        "records": [
            {
                "id": "123",
                "title": "Governed research agents",
                "url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            }
        ],
        "terms_url": "https://www.ncbi.nlm.nih.gov/home/about/policies/",
        "retrieved_from": "https://eutils.ncbi.nlm.nih.gov/",
        "warnings": [],
        "notice": "Metadata only.",
    }
    evidence = function_middleware._evidence_from_tool_result(
        "searchLiteratureMetadata",
        tool_result,
    )[0]
    response = LiteratureResponse(
        summary="tool grounded",
        claims=(
            Claim(
                text="tool claim",
                support=SupportStatus.SUPPORTED,
                evidence_ids=("tool-1",),
            ),
        ),
        evidence=(evidence,),
    )
    agent_context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=[request.model_dump_json()])],
    )

    async def call_next() -> None:
        function_context = FunctionInvocationContext(
            cast(Any, SimpleNamespace(name="searchLiteratureMetadata")),
            {},
            kwargs=agent_context.function_invocation_kwargs,
        )

        async def tool_call() -> None:
            function_context.result = tool_result

        await function_middleware.process(function_context, tool_call)
        agent_context.result = AgentResponse[Any](value=response)

    await ContractMiddleware(get_manifest("literature"), None).process(
        agent_context,
        call_next,
    )
    assert isinstance(agent_context.result, AgentResponse)
    normalized_literature = agent_context.result.value
    assert isinstance(normalized_literature, LiteratureResponse)
    assert normalized_literature.evidence == (evidence,)

    coordinator_evidence = EvidenceRef(
        evidence_id="specialist-1",
        source_uri="https://trusted-specialist.example/result",
    )
    specialist_response = LiteratureResponse(
        summary="specialist",
        claims=(
            Claim(
                text="specialist claim",
                support=SupportStatus.SUPPORTED,
                evidence_ids=("specialist-1",),
            ),
        ),
        evidence=(coordinator_evidence,),
    )
    coordinator_response = CoordinatorResponse(
        summary="coordinated",
        claims=specialist_response.claims,
        evidence=specialist_response.evidence,
        specialist_results=(
            SpecialistResult(
                request_id="ok",
                capability=SpecialistCapability.LITERATURE,
                agent_name="literature-agent",
                response=specialist_response,
            ),
            SpecialistResult(
                request_id="failed",
                capability=SpecialistCapability.GRANT,
                agent_name="grant-agent",
                error_code="unavailable",
            ),
        ),
    )
    normalized_coordinator = ContractMiddleware(
        get_manifest("coordinator"),
        None,
    )._normalize_response(
        AgentResponse[Any](value=coordinator_response),
        (),
        [],
    )
    normalized_coordinator_response = normalized_coordinator.value
    assert isinstance(normalized_coordinator_response, CoordinatorResponse)
    assert normalized_coordinator_response.evidence == (coordinator_evidence,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        [],
        [Message(role="assistant", contents=["{}"])],
    ],
)
async def test_contract_middleware_requires_final_user_message(
    messages: list[Message],
) -> None:
    middleware = ContractMiddleware(get_manifest("literature"), None)
    context = AgentContext(agent=cast(Any, SimpleNamespace()), messages=messages)
    with pytest.raises(ContractError, match="final user"):
        await middleware.process(context, cast(Any, lambda: None))


@pytest.mark.asyncio
async def test_contract_middleware_rejects_invalid_envelope() -> None:
    middleware = ContractMiddleware(get_manifest("literature"), None)
    context = AgentContext(
        agent=cast(Any, SimpleNamespace()),
        messages=[Message(role="user", contents=["{}"])],
    )
    with pytest.raises(ContractError, match="input contract"):
        await middleware.process(context, cast(Any, lambda: None))


@pytest.mark.asyncio
async def test_function_middleware_enforces_governance_before_tool_execution() -> None:
    capability = CapabilityDescriptor(
        id="public.read",
        operation=OperationClass.READ,
        required_scopes=frozenset({"research.public.read"}),
        allowed_destinations=("toolbox.example",),
    )
    base_binding = _binding(capability.id)
    binding = base_binding.model_copy(
        update={"operation_ref": base_binding.operation_ref.model_copy(update={"id": "local.lookup"})}
    )
    registration, _ = _runtime_registration(binding)
    middleware = GovernedFunctionMiddleware(
        capability,
        registration,
    )
    governance = InvocationContext(
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal-a",
        scopes=frozenset({"research.public.read"}),
        destination="toolbox.example",
    )
    context = FunctionInvocationContext(
        cast(Any, SimpleNamespace(name="lookup")),
        {},
        kwargs={
            "governance_context": governance.model_dump(mode="json"),
            "authorized_tool_evidence": [],
        },
    )

    async def call_next() -> None:
        context.result = {"value": "authorized"}

    await middleware.process(context, call_next)
    assert context.result == {"value": "authorized"}
    evidence_collector: list[EvidenceRef] = []
    evidence_context = FunctionInvocationContext(
        cast(Any, SimpleNamespace(name="lookup")),
        {},
        kwargs={
            "governance_context": governance.model_dump(mode="json"),
            "authorized_tool_evidence": evidence_collector,
        },
    )

    async def return_evidence() -> None:
        evidence_context.result = {
            "evidence": [
                {
                    "evidence_id": "tool-1",
                    "source_uri": "https://trusted-tool.example/result",
                }
            ]
        }

    await middleware.process(evidence_context, return_evidence)
    assert evidence_collector == []
    wrong_operation = FunctionInvocationContext(
        cast(Any, SimpleNamespace(name="not_allowlisted")),
        {},
        kwargs={"governance_context": governance.model_dump(mode="json")},
    )
    with pytest.raises(CapabilityNotFoundError, match="operation"):
        await middleware.process(wrong_operation, call_next)
    online_manifest = get_manifest("literature_online")
    online_adapter = _ManifestProviderAdapter(online_manifest.capability_bindings)
    online_prepared = GovernedAgentFactory(online_manifest).prepare(
        _settings(),
        provider_adapter=online_adapter,
        **_trusted_scope(online_manifest),
    )
    assert (
        len(
            middleware_for_manifest(
                online_manifest,
                _settings(),
                online_prepared.capabilities,
                online_prepared.registrations,
                **_trusted_scope(online_manifest),
            )
        )
        == 2
    )
    with pytest.raises(ConfigurationError, match="authenticated tenant and project"):
        middleware_for_manifest(
            online_manifest,
            _settings(),
            online_prepared.capabilities,
            online_prepared.registrations,
        )
    with pytest.raises(ConfigurationError, match="exactly match"):
        middleware_for_manifest(
            online_manifest,
            _settings(),
            online_prepared.capabilities,
            online_prepared.registrations[:-1],
        )

    missing = FunctionInvocationContext(cast(Any, SimpleNamespace(name="lookup")), {})
    with pytest.raises(AuthorizationError, match="missing"):
        await middleware.process(missing, call_next)
    invalid = FunctionInvocationContext(
        cast(Any, SimpleNamespace(name="lookup")),
        {},
        kwargs={"governance_context": {"tenant_id": ""}},
    )
    with pytest.raises(AuthorizationError, match="invalid"):
        await middleware.process(invalid, call_next)
    collector_missing = FunctionInvocationContext(
        cast(Any, SimpleNamespace(name="lookup")),
        {},
        kwargs={"governance_context": governance.model_dump(mode="json")},
    )
    with pytest.raises(ContractError, match="evidence collector"):
        await middleware.process(collector_missing, call_next)

    with pytest.raises(ContractError, match="payload"):
        await middleware._invoke_framework_function({"context": object(), "call_next": call_next})
    with pytest.raises(ContractError, match="payload"):
        await middleware._invoke_framework_function({"context": context, "call_next": object()})
    assert (
        middleware._evidence_from_tool_result(
            "lookup",
            {"evidence": [{"evidence_id": "untrusted"}]},
        )
        == ()
    )
    with pytest.raises(ValueError, match="attached tool registration"):
        GovernedFunctionMiddleware(
            capability,
            (),
        )
    with pytest.raises(ConfigurationError, match="continuously provider-attested"):
        GovernedFunctionMiddleware(
            capability,
            ToolRegistration(
                binding=binding,
                tool_name="lookup",
                handler=GovernedFunctionMiddleware._invoke_framework_function,
                current_instance_fingerprint=binding.instance_ref.fingerprint,
            ),
        )
    other_registration, _ = _runtime_registration(_binding("public.other"))
    with pytest.raises(ConfigurationError, match="capability descriptor"):
        GovernedFunctionMiddleware(capability, other_registration)

    connector_binding = binding.model_copy(
        update={"operation_ref": binding.operation_ref.model_copy(update={"id": "local.searchLiteratureMetadata"})}
    )
    connector_registration, _ = _runtime_registration(connector_binding)
    connector_middleware = GovernedFunctionMiddleware(
        capability,
        connector_registration,
        allowed_connector_sources=frozenset({"pubmed"}),
    )
    connector_result = {
        "source": "pubmed",
        "query": "governed agents",
        "records": [
            {
                "id": "123",
                "title": "Governed research agents",
                "url": "https://pubmed.ncbi.nlm.nih.gov/123/",
            },
            {"id": "456", "title": "Fallback URI"},
        ],
        "terms_url": "https://www.ncbi.nlm.nih.gov/home/about/policies/",
        "retrieved_from": "https://eutils.ncbi.nlm.nih.gov/",
        "warnings": [],
        "notice": "Metadata only.",
    }
    connector_evidence = connector_middleware._evidence_from_tool_result(
        "searchLiteratureMetadata",
        connector_result,
    )
    assert len(connector_evidence) == 2
    assert connector_evidence[0].source_uri == "https://pubmed.ncbi.nlm.nih.gov/123/"
    assert connector_evidence[1].source_uri == "https://eutils.ncbi.nlm.nih.gov/"
    with pytest.raises(ContractError, match="governed response contract"):
        connector_middleware._evidence_from_tool_result(
            "searchLiteratureMetadata",
            {**connector_result, "source": "forbidden"},
        )
    connector_context = FunctionInvocationContext(
        cast(Any, SimpleNamespace(name="searchLiteratureMetadata")),
        {},
        kwargs={
            "governance_context": governance.model_dump(mode="json"),
            "authorized_tool_evidence": [],
        },
    )

    async def return_connector_result() -> None:
        connector_context.result = connector_result

    await connector_middleware.process(connector_context, return_connector_result)
    assert isinstance(connector_context.result, dict)
    assert connector_context.result["authorized_evidence"] == [
        evidence.model_dump(mode="json") for evidence in connector_evidence
    ]

    web_evidence = middleware._evidence_from_tool_result(
        "web_search",
        Content(
            "text",
            text="not-json",
            annotations=cast(
                Any,
                [
                    {
                        "type": "citation",
                        "title": "Official source",
                        "url": "https://learn.microsoft.com/official",
                    },
                    {"type": "citation", "title": "Missing URL"},
                    {"type": "other", "url": "https://example.test/ignored"},
                ],
            ),
        ),
    )
    assert len(web_evidence) == 1
    assert web_evidence[0].source_uri == "https://learn.microsoft.com/official"
    assert (
        middleware._evidence_from_tool_result(
            "web_search",
            [
                Content.from_mcp_server_tool_result(
                    "call-1",
                    output=Content(
                        "text",
                        annotations=cast(
                            Any,
                            [
                                {
                                    "type": "citation",
                                    "title": "Nested citation",
                                    "url": "https://example.test/nested",
                                }
                            ],
                        ),
                    ),
                )
            ],
        )[0].source_uri
        == "https://example.test/nested"
    )
    assert (
        middleware._evidence_from_tool_result(
            "web_search",
            Content.from_function_result(
                "call-2",
                result=Content("text", items=[Content("text")]),
            ),
        )
        == ()
    )
    assert (
        middleware._evidence_from_tool_result(
            "web_search",
            Content(
                "text",
                items=[
                    Content(
                        "text",
                        annotations=cast(
                            Any,
                            [
                                {
                                    "type": "citation",
                                    "url": "https://example.test/item",
                                }
                            ],
                        ),
                    )
                ],
            ),
        )[0].source_uri
        == "https://example.test/item"
    )
    assert middleware._evidence_from_tool_result("web_search", "untrusted") == ()
    connector_json = json.dumps(connector_result)
    assert (
        connector_middleware._evidence_from_tool_result(
            "searchLiteratureMetadata",
            Content.from_mcp_server_tool_result("call-3", output=connector_json),
        )
        == connector_evidence
    )
    assert connector_middleware._dict_payloads(LiteratureResponse(summary="model")) == (
        LiteratureResponse(summary="model").model_dump(mode="json"),
    )
    assert connector_middleware._dict_payloads(Content.from_function_result("call-4", result=connector_result)) == (
        connector_result,
    )
    assert connector_middleware._dict_payloads(Content("text", items=[Content.from_text(text=connector_json)])) == (
        connector_result,
    )
    assert connector_middleware._dict_payloads(Content.from_text(text=connector_json)) == (connector_result,)
    assert connector_middleware._dict_payloads(Content("text")) == ()
    assert connector_middleware._dict_payloads([connector_result]) == (connector_result,)
    assert connector_middleware._dict_payloads("not-json") == ()
    assert connector_middleware._dict_payloads(42) == ()
    assert (
        connector_middleware._evidence_from_tool_result(
            "code_interpreter",
            connector_result,
        )
        == ()
    )
    exposed = connector_middleware._expose_authorized_evidence
    assert exposed([], connector_evidence)[-1].text is not None
    assert exposed((), connector_evidence)[-1].text is not None
    assert isinstance(exposed(Content("text"), connector_evidence), list)
    assert "authorized_evidence" in exposed("result", connector_evidence)
    assert exposed(42, connector_evidence)["authorized_evidence"]
    assert exposed(connector_result, ()) is connector_result
    assert exposed(LiteratureResponse(summary="model"), connector_evidence)["authorized_evidence"]
    with pytest.raises(ConfigurationError, match="resolved runtime settings"):
        middleware_for_manifest(
            online_manifest,
            None,
            online_prepared.capabilities,
            online_prepared.registrations,
            **_trusted_scope(online_manifest),
        )

    write_capability = CapabilityDescriptor(
        id="dataset.compute",
        operation=OperationClass.PRIVILEGED,
        required_scopes=frozenset({"research.dataset.compute"}),
        allowed_destinations=("toolbox.example",),
        side_effect_destinations=("toolbox.example",),
        approval=ApprovalMode.REQUIRED,
        idempotency=IdempotencyMode.REQUIRED,
        idempotency_policy=IdempotencyPolicy(
            completed_replay=CompletedReplayMode.RETURN_RESULT,
        ),
    )
    write_registration, _ = _runtime_registration(_binding(write_capability.id))
    write_middleware = GovernedFunctionMiddleware(
        write_capability,
        write_registration,
        idempotency_store=InMemoryIdempotencyStore(),
        approval_adapter=_AutoApprovalAdapter(),
        release_id=f"sha256:{'a' * 64}",
        allow_test_idempotency_store=True,
        allow_test_approval_adapter=True,
    )
    write_governance = InvocationContext(
        tenant_id="tenant-a",
        project_id="project-a",
        principal_id="principal-a",
        scopes=frozenset({"research.dataset.compute"}),
        destination="toolbox.example",
        approval_decision_id="approval-1",
        invocation_id="invocation-1",
        idempotency_key="session-a",
    )
    executions: list[int] = []

    async def invoke_with(value: int) -> Any:
        write_context = FunctionInvocationContext(
            cast(Any, SimpleNamespace(name="compute")),
            {"value": value},
            kwargs={
                "governance_context": write_governance.model_copy(
                    update={
                        "approval_decision_id": f"approval-{value}",
                        "invocation_id": f"invocation-{value}",
                    }
                ).model_dump(mode="json"),
                "authorized_tool_evidence": [],
            },
        )

        async def execute() -> None:
            executions.append(value)
            write_context.result = {"computed": value}

        await write_middleware.process(write_context, execute)
        return write_context.result

    assert await invoke_with(1) == {"computed": 1}
    assert await invoke_with(1) == {"computed": 1}
    assert await invoke_with(2) == {"computed": 2}
    assert executions == [1, 2]


@pytest.mark.asyncio
async def test_local_harness_validates_protocol_and_runner_failures() -> None:
    manifest = get_manifest("literature")
    harness = LocalHarness(manifest, lambda _request: _evidence_response())
    assert harness.readiness()["input_contract"] == "LiteratureRequestV2"
    assert harness.readiness()["idempotency_store_configured"] is True
    result = await harness.invoke(LocalInvocation(manifest_id="literature", payload=_request()))
    assert result.ok is True
    wrong = await harness.invoke(LocalInvocation(manifest_id="grant", payload=_request()))
    assert wrong.error is not None and wrong.error.code == "contract_error"
    invalid = await harness.invoke(LocalInvocation(manifest_id="literature", payload={"query": ""}))
    assert invalid.error is not None and invalid.error.context["errors"]

    async def async_runner(_request: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"summary": "async result"}

    async_result = await LocalHarness(manifest, async_runner).invoke(
        LocalInvocation(manifest_id="literature", payload=_request())
    )
    assert async_result.ok is True
    failed = await LocalHarness(
        manifest,
        lambda _request: (_ for _ in ()).throw(ContractError("blocked")),
    ).invoke(LocalInvocation(manifest_id="literature", payload=_request()))
    assert failed.error is not None and failed.error.code == "contract_error"

    async def cancelled(_request: Any) -> LiteratureResponse:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await LocalHarness(manifest, cancelled).invoke(LocalInvocation(manifest_id="literature", payload=_request()))

    dataset = get_manifest("dataset")
    dataset_registrations = (
        GovernedAgentFactory(dataset)
        .prepare(
            _settings(toolbox_endpoint="https://toolbox.example/toolboxes/dataset/mcp"),
            provider_adapter=_ManifestProviderAdapter(dataset.capability_bindings),
            **_trusted_scope(dataset),
        )
        .registrations
    )
    local_dataset = LocalHarness(
        dataset,
        lambda _request: ResearchResponse(summary="not executed"),
        registrations=dataset_registrations,
    )
    assert local_dataset.readiness()["ready"] is False
    assert (
        LocalHarness(
            dataset,
            lambda _request: ResearchResponse(summary="not executed"),
            registrations=dataset_registrations,
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=_AutoApprovalAdapter(),
        ).readiness()["ready"]
        is True
    )


def test_router_uses_caller_sensitivity_and_allowlisted_targets() -> None:
    router = CoordinatorRouter(
        offline_names={SpecialistCapability.LITERATURE: "offline"},
        online_names={SpecialistCapability.LITERATURE: "online"},
    )
    public = CoordinatorRequest.model_validate(_request(sensitivity="public", requested_capabilities=["literature"]))
    routes = router.route(public)
    assert [route.target_agent for route in routes] == ["online"]
    internal = public.model_copy(update={"sensitivity": Sensitivity.INTERNAL})
    assert router.route(internal)[0].target_agent == "offline"
    assert router.target(SpecialistCapability.DATASET, Sensitivity.PUBLIC) is None
    unroutable = public.model_copy(update={"requested_capabilities": (SpecialistCapability.DATASET,)})
    with pytest.raises(ContractError, match="no pinned specialist"):
        router.route(unroutable)
    dataset_request = CoordinatorRequest.model_validate(
        _request(
            requested_capabilities=["dataset"],
            specialist_inputs={"dataset": {"dataset_id": "dataset.csv"}},
        )
    )
    typed = CoordinatorRouter().route(dataset_request)[0].request
    assert isinstance(typed, DatasetRequest)
    assert typed.dataset_id == "dataset.csv"
    missing_dataset_input = dataset_request.model_copy(update={"specialist_inputs": {}})
    with pytest.raises(ContractError, match="pinned contract"):
        CoordinatorRouter().route(missing_dataset_input)


def test_coordinator_policy_is_required_and_budget_is_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = get_manifest("coordinator")
    policy = manifest.specialist_policy
    assert policy is not None
    bounded = policy.model_copy(update={"budget_units": 1})
    router = CoordinatorRouter(specialist_policy=bounded)
    coordinator_binding = manifest.capability_bindings[0]
    request = CoordinatorRequest.model_validate(
        _request(
            tenant_id=coordinator_binding.tenant_scope,
            project_id=coordinator_binding.project_scope,
            requested_capabilities=["literature", "grant"],
        )
    )
    with pytest.raises(ContractError, match="budget"):
        router.route(request)

    without_policy = manifest.model_copy(update={"specialist_policy": None})
    monkeypatch.setattr("shared.workflows.get_manifest", lambda _profile_id: without_policy)
    with pytest.raises(ContractError, match="specialist policy"):
        CoordinatorRouter()
    with pytest.raises(ContractError, match="specialist policy"):
        build_coordinator_workflow(cast(Any, lambda _request: None))


@pytest.mark.asyncio
async def test_agent_framework_coordinator_workflow_preserves_typed_evidence() -> None:
    async def invoke(request: Any) -> SpecialistResult:
        if request.capability == SpecialistCapability.GRANT:
            return SpecialistResult(
                request_id=request.request_id,
                capability=request.capability,
                agent_name=request.target_agent,
                error_code="not_available",
            )
        return SpecialistResult(
            request_id=request.request_id,
            capability=request.capability,
            agent_name=request.target_agent,
            response=_evidence_response(),
        )

    coordinator_manifest = get_manifest("coordinator")
    coordinator_binding = coordinator_manifest.capability_bindings[0]
    with pytest.raises(ConfigurationError, match="runtime-attested"):
        build_coordinator_workflow(
            ToolRegistration(
                binding=coordinator_binding,
                tool_name="invoke",
                handler=lambda payload: payload,
                current_instance_fingerprint=(coordinator_binding.instance_ref.fingerprint),
            )
        )
    mismatched_registration, _ = _runtime_registration(_binding("specialist.delegate"))
    with pytest.raises(ConfigurationError, match="runtime-attested"):
        build_coordinator_workflow(mismatched_registration)

    workflow = build_coordinator_workflow(_coordinator_registration(invoke))
    request = CoordinatorRequest.model_validate(
        _request(
            tenant_id=coordinator_binding.tenant_scope,
            project_id=coordinator_binding.project_scope,
            requested_capabilities=["literature", "grant"],
        )
    )
    events = await workflow.run(request)
    outputs = events.get_outputs()
    assert len(outputs) == 1
    response = CoordinatorResponse.model_validate_json(outputs[0])
    assert len(response.specialist_results) == 2
    assert response.claims[0].evidence_ids == ("ev-1",)


@pytest.mark.asyncio
async def test_coordinator_workflow_validates_hosted_message_envelope() -> None:
    async def invoke(request: SpecialistRequest) -> SpecialistResult:
        return SpecialistResult(
            request_id=request.request_id,
            capability=request.capability,
            agent_name=request.target_agent,
            error_code="not_configured",
        )

    manifest = get_manifest("coordinator")
    adapter = _ManifestProviderAdapter(manifest.capability_bindings)
    attestation = next(iter(adapter.attestations.values()))
    invalid_handler = specialist_handler_resolver(invoke)(attestation)
    invalid_result = invalid_handler({})
    assert inspect.isawaitable(invalid_result)
    with pytest.raises(ContractError, match="payload"):
        await invalid_result

    workflow = build_coordinator_workflow(_coordinator_registration(invoke))
    with pytest.raises(ContractError, match="final user"):
        await workflow.run([])
    with pytest.raises(ContractError, match="final user"):
        await workflow.run([Message(role="assistant", contents=["{}"])])
    with pytest.raises(ContractError, match="invalid"):
        await workflow.run([Message(role="user", contents=["{}"])])

    binding = manifest.capability_bindings[0]
    request = CoordinatorRequest.model_validate(
        _request(
            tenant_id=binding.tenant_scope,
            project_id=binding.project_scope,
            requested_capabilities=["literature"],
        )
    )
    response = await workflow.run([Message(role="user", contents=[request.model_dump_json()])])
    parsed = CoordinatorResponse.model_validate_json(response.get_outputs()[0])
    assert parsed.specialist_results[0].error_code == "not_configured"


def _specialist_request(
    capability: SpecialistCapability,
    *,
    sensitivity: Sensitivity = Sensitivity.INTERNAL,
    target: str | None = None,
) -> SpecialistRequest:
    specialist_inputs = {"dataset": {"dataset_id": "dataset.csv"}} if capability == SpecialistCapability.DATASET else {}
    request = CoordinatorRequest.model_validate(
        _request(
            sensitivity=sensitivity,
            requested_capabilities=[capability],
            specialist_inputs=specialist_inputs,
        )
    )
    return SpecialistRequest(
        request_id=f"request-{capability}",
        capability=capability,
        request=CoordinatorRouter._typed_request(request, capability),
        target_agent=target or f"{capability}-agent",
    )


@pytest.mark.asyncio
async def test_foundry_specialist_invoker_uses_typed_contract_and_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shared.workflows.get_credential", lambda _client_id: "credential")
    project_calls: list[dict[str, Any]] = []
    invocation_calls: list[tuple[Any, str, str, float | None]] = []

    class Project:
        def __enter__(self) -> Project:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get_openai_client(self, *, agent_name: str) -> str:
            return f"client:{agent_name}"

    def project_factory(**kwargs: Any) -> Project:
        project_calls.append(kwargs)
        return Project()

    class ResponsesInvoker:
        def invoke(
            self,
            client: Any,
            request: str,
            agent_name: str,
            *,
            deadline_monotonic: float | None = None,
        ) -> HostedAgentReply:
            invocation_calls.append((client, request, agent_name, deadline_monotonic))
            return HostedAgentReply(
                agent_name=agent_name,
                content='{"summary":"typed specialist result"}',
            )

    invoker = FoundrySpecialistInvoker(
        _settings(default_timeout_seconds=20),
        project_factory=project_factory,
        responses_invoker=cast(Any, ResponsesInvoker()),
        monotonic=lambda: 5,
    )
    result = await invoker(
        _specialist_request(
            SpecialistCapability.LITERATURE,
            target="literature-agent",
        )
    )
    assert result.response is not None
    assert result.response.summary == "typed specialist result"
    assert project_calls[0]["credential"] == "credential"
    assert invocation_calls[0][0] == "client:literature-agent"
    assert invocation_calls[0][3] == 25
    typed_request = LiteratureRequest.model_validate_json(invocation_calls[0][1])
    assert typed_request.principal_id == "principal-a"

    with pytest.raises(ContractError, match="pinned manifest"):
        await invoker(
            _specialist_request(
                SpecialistCapability.LITERATURE,
                target="model-selected-agent",
            )
        )


@pytest.mark.asyncio
async def test_foundry_specialist_invoker_returns_structured_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shared.workflows.get_credential", lambda _client_id: "credential")

    class Project:
        def __enter__(self) -> Project:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def get_openai_client(self, *, agent_name: str) -> str:
            return agent_name

    class InvalidOutput:
        def invoke(self, *_args: Any, **_kwargs: Any) -> HostedAgentReply:
            return HostedAgentReply(agent_name="literature-agent", content="{}")

    invalid = FoundrySpecialistInvoker(
        _settings(),
        project_factory=lambda **_kwargs: Project(),
        responses_invoker=cast(Any, InvalidOutput()),
    )
    invalid_result = await invalid(
        _specialist_request(
            SpecialistCapability.LITERATURE,
            target="literature-agent",
        )
    )
    assert invalid_result.error_code == "contract_error"

    class FailedInvocation:
        def invoke(self, *_args: Any, **_kwargs: Any) -> HostedAgentReply:
            raise RetryableInvocationError("not ready")

    failed = FoundrySpecialistInvoker(
        _settings(),
        project_factory=lambda **_kwargs: Project(),
        responses_invoker=cast(Any, FailedInvocation()),
    )
    failed_result = await failed(
        _specialist_request(
            SpecialistCapability.GRANT,
            target="grant-agent",
        )
    )
    assert failed_result.error_code == "retryable_invocation_failed"

    setup_failure = FoundrySpecialistInvoker(
        _settings(),
        project_factory=lambda **_kwargs: (_ for _ in ()).throw(ValueError("setup")),
        responses_invoker=cast(Any, InvalidOutput()),
    )
    setup_result = await setup_failure(
        _specialist_request(
            SpecialistCapability.MATCHING,
            target="matching-agent",
        )
    )
    assert setup_result.error_code == "invocation_failed"

    default_invoker = FoundrySpecialistInvoker(
        _settings(),
        project_factory=lambda **_kwargs: Project(),
    )
    assert isinstance(default_invoker._responses, RetryingResponsesInvoker)


def test_specialist_manifest_selection_and_payload_are_deterministic() -> None:
    expected = {
        SpecialistCapability.LITERATURE: "literature",
        SpecialistCapability.GRANT: "grant",
        SpecialistCapability.MATCHING: "matching",
        SpecialistCapability.DATASET: "dataset",
        SpecialistCapability.INSTITUTION: "institution",
    }
    for capability, profile_id in expected.items():
        request = _specialist_request(capability)
        assert _specialist_manifest(request).id == profile_id
    for capability in (
        SpecialistCapability.LITERATURE,
        SpecialistCapability.GRANT,
        SpecialistCapability.MATCHING,
    ):
        request = _specialist_request(
            capability,
            sensitivity=Sensitivity.PUBLIC,
            target=f"{capability}-online-agent",
        )
        assert _specialist_manifest(request).id == f"{expected[capability]}_online"
    dataset = _specialist_request(SpecialistCapability.DATASET)
    payload = _specialist_payload(dataset, "dataset")
    assert payload["dataset_id"] == "dataset.csv"
    assert "approved_compute" not in payload
    assert payload["idempotency_key"] is None
    assert "requested_capabilities" not in payload


def test_all_hosted_agents_construct_responses_servers_without_history_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_names = (
        "dataset.factory",
        "grant.factory",
        "grant_online.factory",
        "institution.factory",
        "literature.factory",
        "literature_online.factory",
        "matching.factory",
        "matching_online.factory",
    )
    monkeypatch.setattr("shared.tools.get_credential", lambda _client_id=None: object())
    monkeypatch.setattr("shared.tools.FoundryToolbox", lambda *_args, **_kwargs: object())
    for module_name in module_names:
        module = importlib.import_module(module_name)
        agent = module.build_agent(
            client=_FakeChatClient(),
            settings=_settings(
                model_deployment_name=module.MANIFEST.model_policy.deployment_name,
                model_deployment_version=module.MANIFEST.model_policy.pinned_model_version,
                toolbox_endpoint="https://toolbox.example/toolboxes/research/mcp",
            ),
            provider_adapter=(
                _ManifestProviderAdapter(module.MANIFEST.capability_bindings)
                if module.MANIFEST.capability_bindings
                else None
            ),
            **(
                _trusted_scope(module.MANIFEST)
                if module.MANIFEST.capability_bindings
                else {}
            ),
            idempotency_store=InMemoryIdempotencyStore(),
            approval_adapter=_AutoApprovalAdapter(),
            release_attestor=_release_attestor(module.MANIFEST),
            allow_test_idempotency_store=True,
            allow_test_approval_adapter=True,
        )
        server = ResponsesHostServer(agent)
        assert server is not None

    async def invoke(request: SpecialistRequest) -> SpecialistResult:
        return SpecialistResult(
            request_id=request.request_id,
            capability=request.capability,
            agent_name=request.target_agent,
            error_code="not_configured",
        )

    coordinator = importlib.import_module("coordinator.factory")
    coordinator_agent = coordinator.build_agent(
        settings=_settings(),
        invoker=invoke,
        provider_adapter=_ManifestProviderAdapter(coordinator.MANIFEST.capability_bindings),
        **_trusted_scope(coordinator.MANIFEST),
        release_attestor=_release_attestor(coordinator.MANIFEST),
    )
    assert ResponsesHostServer(coordinator_agent) is not None


def test_coordinator_factory_fails_closed_for_future_privileged_toolbox_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = importlib.import_module("coordinator.factory")
    coordinator_manifest = get_manifest("coordinator")
    dataset_binding = get_manifest("dataset").capability_bindings[0]
    mutated_manifest = coordinator_manifest.model_copy(
        update={
            "capability_bindings": (
                *coordinator_manifest.capability_bindings,
                dataset_binding,
            )
        }
    )
    mutated_factory = GovernedAgentFactory(mutated_manifest)
    monkeypatch.setattr(coordinator, "FACTORY", mutated_factory)
    adapter = _ManifestProviderAdapter(mutated_manifest.capability_bindings)

    async def invoke(request: SpecialistRequest) -> SpecialistResult:
        return SpecialistResult(
            request_id=request.request_id,
            capability=request.capability,
            agent_name=request.target_agent,
            error_code="not_configured",
        )

    trusted_scope = _trusted_scope(mutated_manifest)
    with pytest.raises(ConfigurationError, match="Toolbox endpoint"):
        coordinator.build_agent(
            settings=_settings(),
            invoker=invoke,
            provider_adapter=adapter,
            **trusted_scope,
        )
    toolbox_settings = _settings(
        toolbox_endpoint="https://toolbox.example/toolboxes/research/mcp",
    )
    with pytest.raises(ConfigurationError, match="durable idempotency store"):
        coordinator.build_agent(
            settings=toolbox_settings,
            invoker=invoke,
            provider_adapter=adapter,
            **trusted_scope,
        )
    with pytest.raises(ConfigurationError, match="durable approval adapter"):
        coordinator.build_agent(
            settings=toolbox_settings,
            invoker=invoke,
            provider_adapter=adapter,
            **trusted_scope,
            idempotency_store=InMemoryIdempotencyStore(),
            allow_test_idempotency_store=True,
        )


def test_all_nine_agent_specific_factories_are_first_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_names = (
        "dataset.factory",
        "grant.factory",
        "grant_online.factory",
        "institution.factory",
        "literature.factory",
        "literature_online.factory",
        "matching.factory",
        "matching_online.factory",
    )
    ids = []
    monkeypatch.setattr("shared.tools.get_credential", lambda _client_id=None: object())
    monkeypatch.setattr("shared.tools.FoundryToolbox", lambda *_args, **_kwargs: object())
    for module_name in module_names:
        module = importlib.import_module(module_name)
        ids.append(module.MANIFEST.id)
        assert module.FACTORY.manifest is module.MANIFEST
        provider_adapter = (
            _ManifestProviderAdapter(module.MANIFEST.capability_bindings)
            if module.MANIFEST.capability_bindings
            else None
        )
        assert (
            module.build_agent(
                client=_FakeChatClient(),
                settings=_settings(
                    model_deployment_name=module.MANIFEST.model_policy.deployment_name,
                    model_deployment_version=module.MANIFEST.model_policy.pinned_model_version,
                    toolbox_endpoint="https://toolbox.example/toolboxes/research/mcp",
                ),
                provider_adapter=provider_adapter,
                **(
                    _trusted_scope(module.MANIFEST)
                    if module.MANIFEST.capability_bindings
                    else {}
                ),
                idempotency_store=InMemoryIdempotencyStore(),
                approval_adapter=_AutoApprovalAdapter(),
                release_attestor=_release_attestor(module.MANIFEST),
                allow_test_idempotency_store=True,
                allow_test_approval_adapter=True,
            ).name
            == module.MANIFEST.name
        )
        calls: list[str] = []
        monkeypatch.setattr(module, "run_profile", calls.append)
        module.run()
        assert calls == [module.FACTORY.manifest.id]
    coordinator = importlib.import_module("coordinator.factory")
    ids.append(coordinator.MANIFEST.id)
    assert coordinator.FACTORY.manifest is coordinator.MANIFEST

    async def invoke(request: Any) -> SpecialistResult:
        return SpecialistResult(
            request_id=request.request_id,
            capability=request.capability,
            agent_name=request.target_agent,
            error_code="not_configured",
        )

    coordinator_agent = coordinator.build_agent(
        settings=_settings(),
        invoker=invoke,
        provider_adapter=_ManifestProviderAdapter(coordinator.MANIFEST.capability_bindings),
        **_trusted_scope(coordinator.MANIFEST),
        release_attestor=_release_attestor(coordinator.MANIFEST),
    )
    assert isinstance(coordinator_agent, WorkflowAgent)
    assert coordinator_agent.name == coordinator.MANIFEST.name
    coordinator_calls: list[Any] = []

    class CoordinatorHost:
        def __init__(self, agent: Any) -> None:
            coordinator_calls.append(agent)

        def run(self) -> None:
            coordinator_calls.append("run")

    monkeypatch.setattr(coordinator, "build_agent", lambda: "coordinator-agent")
    monkeypatch.setattr(coordinator, "ResponsesHostServer", CoordinatorHost)
    coordinator.run()
    assert coordinator_calls == ["coordinator-agent", "run"]
    assert set(ids) == {manifest.id for manifest in list_manifests()}
