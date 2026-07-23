from __future__ import annotations

import asyncio
import importlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

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
from shared.capabilities import (
    ApprovalMode,
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityExecutor,
    CapabilityPolicy,
    CapabilityRegistry,
    IdempotencyMode,
    InvocationContext,
    OperationClass,
    ProviderInstanceAttestation,
    RetryPolicy,
    ToolRegistration,
    attach_provider_binding,
    template_instance_fingerprint,
)
from shared.catalog import capabilities_for_manifest
from shared.contracts import (
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
    ApprovalRequiredError,
    AuthorizationError,
    CapabilityNotFoundError,
    ConfigurationError,
    ContractError,
    DeadlineExceededError,
    DestinationDeniedError,
    HarnessError,
    IdempotencyRequiredError,
    InvocationError,
    IsolationError,
    RetryableInvocationError,
    StaleCapabilityBindingError,
    error_from_exception,
    error_response,
)
from shared.factory import GovernedAgentFactory, get_factory
from shared.invocation import HostedAgentReply, HostedInvocationPolicy, RetryingResponsesInvoker
from shared.local_harness import LocalHarness, LocalInvocation
from shared.middleware import (
    ContractMiddleware,
    GovernedFunctionMiddleware,
    middleware_for_manifest,
)
from shared.profiles import get_manifest, get_profile, list_manifests
from shared.release import (
    build_release_metadata,
    manifest_digest,
    source_bundle_digest,
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
from shared.telemetry import redact_attributes
from shared.tools import _invoke_specialist, delegated_agent_name, tools_for_profile
from shared.workflows import (
    CoordinatorRouter,
    FoundrySpecialistInvoker,
    _specialist_manifest,
    _specialist_payload,
    build_coordinator_workflow,
)


def _request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": "Compare supplied evidence",
        "tenant_id": "tenant-a",
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


def _binding(capability_id: str) -> CapabilityBinding:
    return CapabilityBinding(
        descriptor_id=capability_id,
        descriptor_version="1.0.0",
        operation_id=f"local.{capability_id}",
        provider_id="test-provider",
        instance_id=f"test:{capability_id}",
        instance_ref="app://tests/local",
        instance_fingerprint="1" * 64,
        provider_contract_version="test-provider.v2",
        provider_contract_schema_digest="2" * 64,
        pinned_provider_version="1.0.0",
        input_schema_digest=SCHEMA_REFERENCES["LiteratureRequestV2"].sha256,
        output_schema_digest=SCHEMA_REFERENCES["LiteratureSynthesisV2"].sha256,
        config={"mode": "test"},
        config_digest=canonical_digest({"mode": "test"}),
        connection_ref="app://connections/tests",
        policy_ref="app://policy/tests",
        destination_pins=("app://tests/local",),
        tenant_scope="tenant-a",
        project_scope="project-a",
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
            approved_compute=True,
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
    with pytest.raises(ValidationError, match="stable idempotency"):
        DatasetRequest.model_validate(_request(dataset_id="dataset.csv", approved_compute=True))
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
    assert parse_specialist_request(
        {"capability": "literature", "request": "invalid"}
    ) == {"capability": "literature", "request": "invalid"}
    invalid_discriminator = {
        "capability": "unknown",
        "request": _request(),
    }
    assert (
        parse_specialist_request(invalid_discriminator) == invalid_discriminator
    )
    missing_discriminator = {"request": _request()}
    assert (
        parse_specialist_request(missing_discriminator) == missing_discriminator
    )


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
    with pytest.raises(ValidationError, match="mutually exclusive"):
        CapabilityBinding.model_validate(
            {
                **binding.model_dump(),
                "config": {"limit": 1},
                "config_ref": "app://config/read-action",
            }
        )
    with pytest.raises(ValidationError, match="instance_fingerprint"):
        CapabilityBinding.model_validate(
            {
                **binding.model_dump(),
                "instance_fingerprint": "A" * 64,
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
        "principal_id": "principal",
        "scopes": frozenset({"write"}),
        "destination": "approved.example",
        "approved_capabilities": frozenset({"write.action"}),
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
            InvocationContext.model_validate({**base, "approved_capabilities": frozenset()}),
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
            InvocationContext(tenant_id="tenant", principal_id="principal"),
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
    registry.register_tool(
        ToolRegistration(
            binding=_binding("read.action").model_copy(
                update={"operation_id": "local.secondAction"}
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
        "instance_ref": "app://tests/local",
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
    binding = _binding("read.action").model_copy(
        update={
            "operation_id": "provider.read",
            "instance_fingerprint": "3" * 64,
        }
    )
    valid = ProviderInstanceAttestation(
        provider_id=binding.provider_id,
        provider_contract_version=binding.provider_contract_version,
        provider_contract_schema_digest=binding.provider_contract_schema_digest,
        descriptor_id=binding.descriptor_id,
        descriptor_version=binding.descriptor_version,
        operation_id=binding.operation_id,
        instance_id=binding.instance_id,
        instance_ref=binding.instance_ref,
        instance_fingerprint=binding.instance_fingerprint,
        discovered_version=binding.pinned_provider_version,
        input_schema_digest=binding.input_schema_digest,
        output_schema_digest=binding.output_schema_digest,
        config_digest=canonical_digest(binding.config),
        config_ref=binding.config_ref,
        connection_ref=binding.connection_ref,
        policy_ref=binding.policy_ref,
        destination_pins=binding.destination_pins,
        tenant_id="tenant-a",
        project_id="project-a",
        readiness="available",
        auth_ready=True,
        maturity="GA",
        lifecycle="ACTIVE",
        approval_expires_at=datetime(2026, 7, 24, tzinfo=UTC),
    )

    class Adapter:
        contract_version = binding.provider_contract_version
        contract_schema_digest = binding.provider_contract_schema_digest
        trusted_legacy_derivation = False

        def __init__(self, attestation: ProviderInstanceAttestation) -> None:
            self.attestation = attestation
            self.handler_resolutions = 0

        def discover_instance(
            self,
            provider_id: str,
            instance_id: str,
        ) -> ProviderInstanceAttestation:
            assert provider_id == binding.provider_id
            assert instance_id == binding.instance_id
            return self.attestation

        def resolve_handler(self, _attestation: ProviderInstanceAttestation) -> Any:
            self.handler_resolutions += 1
            return lambda payload: payload

        def load_schema(self, schema_digest: str) -> dict[str, Any]:
            schemas = {
                binding.input_schema_digest: LiteratureRequest.model_json_schema(),
                binding.output_schema_digest: LiteratureResponse.model_json_schema(),
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
    assert registration.current_instance_fingerprint == binding.instance_fingerprint
    assert adapter.handler_resolutions == 1

    failures = (
        (valid.model_copy(update={"input_schema_digest": "4" * 64}), ConfigurationError),
        (
            valid.model_copy(update={"instance_fingerprint": "5" * 64}),
            StaleCapabilityBindingError,
        ),
        (valid.model_copy(update={"tenant_id": "other-tenant"}), AuthorizationError),
        (valid.model_copy(update={"project_id": "other-project"}), AuthorizationError),
        (valid.model_copy(update={"readiness": "degraded"}), ConfigurationError),
        (valid.model_copy(update={"readiness": "unavailable"}), ConfigurationError),
        (valid.model_copy(update={"auth_ready": False}), AuthorizationError),
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
            valid.model_copy(update={"connection_ref": "app://connections/other"}),
            ConfigurationError,
        ),
        (
            valid.model_copy(update={"policy_ref": "app://policy/other"}),
            ConfigurationError,
        ),
        (
            valid.model_copy(update={"config_digest": "7" * 64}),
            ConfigurationError,
        ),
        (
            valid.model_copy(update={"destination_pins": ("app://other",)}),
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
    digest_mismatch.contract_schema_digest = "6" * 64
    with pytest.raises(ConfigurationError, match="pinned capability binding"):
        attach_provider_binding(
            binding,
            digest_mismatch,
            tenant_id="tenant-a",
            project_id="project-a",
        )
    assert digest_mismatch.handler_resolutions == 0

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

    legacy_binding = binding.model_copy(
        update={"provider_contract_version": "integration-provider.v1"}
    )
    legacy = Adapter(
        valid.model_copy(
            update={"provider_contract_version": "integration-provider.v1"}
        )
    )
    legacy.contract_version = "integration-provider.v1"
    with pytest.raises(ConfigurationError, match="Legacy provider"):
        attach_provider_binding(
            legacy_binding,
            legacy,
            tenant_id="tenant-a",
            project_id="project-a",
        )
    assert legacy.handler_resolutions == 0


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
        tenant_id="tenant",
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
    assert await executor.invoke("read.action", {"value": 3}, other_tenant) == {"value": 3}
    assert attempts == 3
    assert await executor.invoke_operation("action", {"value": 4}, other_tenant) == {"value": 3}


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
        tenant_id="tenant",
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
    context = InvocationContext(tenant_id="tenant", principal_id="principal")
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
            InvocationContext(tenant_id="t", principal_id="p"),
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
            InvocationContext(tenant_id="t", principal_id="p"),
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
            InvocationContext(tenant_id="t", principal_id="p"),
        )


@pytest.mark.asyncio
async def test_conversation_and_long_term_memory_enforce_tenant_boundary() -> None:
    store = InMemoryConversationStore()
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


def test_release_metadata_is_immutable_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = get_manifest("dataset")
    assert manifest_digest(manifest) == manifest_digest(manifest.model_copy())
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
    )
    assert release.source_revision == "abc123"
    assert release.dependencies[-1] == ("pydantic", "not-installed")
    assert release.built_at == built_at
    assert release.release_id.startswith("sha256:")
    assert release.model_deployment == "gpt-5.4-mini"
    assert release.capability_versions == (("dataset.compute", "mcp-v1"),)
    assert release.toolbox_versions == (("foundry.toolbox.code_interpreter", "mcp-v1"),)
    assert len(release.contract_schema_digest) == 64
    assert release.provider_contracts == (
        (
            "microsoft-foundry-toolbox",
            "foundry-toolbox.mcp-v1",
            manifest.capability_bindings[0].provider_contract_schema_digest,
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
    )
    assert explicit.source_revision == "explicit"
    same_content = build_release_metadata(
        manifest,
        model_deployment="gpt-5.4-mini",
        source_bundle_hash="a" * 64,
        parent_release_id=f"sha256:{'b' * 64}",
        built_at=datetime(2026, 7, 23, tzinfo=UTC),
    )
    assert same_content.release_id == release.release_id
    with pytest.raises(ValueError, match="model policy"):
        build_release_metadata(manifest, model_deployment="unapproved-model")

    root = tmp_path / "bundle"
    root.mkdir()
    (root / "agent.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "ignored.md").write_text("ignored", encoding="utf-8")
    first_hash = source_bundle_digest(root)
    (root / "agent.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert source_bundle_digest(root) != first_hash


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


def test_runtime_capability_fingerprint_detects_provider_retargeting() -> None:
    factory = get_factory("dataset")
    template = factory.manifest.capability_bindings[0]
    assert template.instance_fingerprint == template_instance_fingerprint(template)
    first_settings = _settings(
        toolbox_endpoint="https://toolbox-a.example/toolboxes/dataset/mcp"
    )
    second_settings = _settings(
        toolbox_endpoint="https://toolbox-b.example/toolboxes/dataset/mcp"
    )
    first = factory.resolved_manifest(first_settings)
    second = factory.resolved_manifest(second_settings)
    first_fingerprint = first.capability_bindings[0].instance_fingerprint
    second_fingerprint = second.capability_bindings[0].instance_fingerprint
    assert first_fingerprint != template.instance_fingerprint
    assert second_fingerprint != first_fingerprint
    stale_factory = GovernedAgentFactory(first)
    with pytest.raises(StaleCapabilityBindingError) as captured:
        stale_factory.resolved_manifest(second_settings)
    assert captured.value.context["expected_fingerprint"] == first_fingerprint
    assert captured.value.context["current_fingerprint"] == second_fingerprint
    assert stale_factory.readiness(second_settings)["ready"] is False


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
    )
    assert agent.name == "literature-agent"
    assert agent.default_options["store"] is False
    assert agent.default_options["response_format"] is LiteratureResponse
    assert agent.context_providers == []
    assert factory.capabilities() == ()
    assert get_factory("dataset").manifest.id == "dataset"

    marker = object()
    monkeypatch.setattr("shared.factory._build_foundry_client", lambda _settings: marker)
    built = factory.build(settings=literature_settings)
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
    discovered = literature_settings.model_copy(
        update={"model_deployment_version": None}
    )
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
        {"client": "client", "settings": None},
    )
    assert runtime.describe_profile("grant").id == "grant"

    calls: list[Any] = []

    class Host:
        def __init__(self, agent: Any) -> None:
            calls.append(agent)

        def run(self) -> None:
            calls.append("run")

    monkeypatch.setattr(runtime, "ResponsesHostServer", Host)
    monkeypatch.setattr(runtime, "build_agent", lambda profile_id: f"agent:{profile_id}")
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
    assert dataset_factory.readiness(_settings())["ready"] is False
    assert (
        dataset_factory.readiness(
            _settings(toolbox_endpoint="https://toolbox.example/toolboxes/dataset/mcp")
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
            approved_compute=True,
            idempotency_key="dataset-operation-1",
        )
    )
    dataset_context = ContractMiddleware(
        get_manifest("dataset"),
        None,
        monotonic=lambda: 5,
    )._invocation_context(dataset)
    assert dataset_context.approved_capabilities == frozenset()
    assert dataset_context.scopes == frozenset()
    assert dataset_context.idempotency_key == "dataset-operation-1"
    assert dataset_context.deadline_monotonic == 65
    unapproved = dataset.model_copy(update={"approved_compute": False})
    assert (
        ContractMiddleware(get_manifest("dataset"), None)._invocation_context(unapproved).approved_capabilities
        == frozenset()
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
    binding = _binding(capability.id).model_copy(
        update={"operation_id": "local.searchLiteratureMetadata"}
    )
    function_middleware = GovernedFunctionMiddleware(
        capability,
        binding,
        current_instance_fingerprints=binding.instance_fingerprint,
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
    binding = _binding(capability.id).model_copy(update={"operation_id": "local.lookup"})
    middleware = GovernedFunctionMiddleware(
        capability,
        binding,
        current_instance_fingerprints=binding.instance_fingerprint,
    )
    governance = InvocationContext(
        tenant_id="tenant-a",
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
    online_capability = capabilities_for_manifest(online_manifest, _settings())[0]
    assert (
        len(
            middleware_for_manifest(
                online_manifest,
                _settings(),
                (online_capability,),
            )
        )
        == 2
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
    assert middleware._evidence_from_tool_result(
        "lookup",
        {"evidence": [{"evidence_id": "untrusted"}]},
    ) == ()
    with pytest.raises(ValueError, match="Every capability binding"):
        GovernedFunctionMiddleware(
            capability,
            (binding,),
            current_instance_fingerprints=(),
        )

    connector_binding = binding.model_copy(
        update={"operation_id": "local.searchLiteratureMetadata"}
    )
    connector_middleware = GovernedFunctionMiddleware(
        capability,
        connector_binding,
        current_instance_fingerprints=connector_binding.instance_fingerprint,
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
        )
    )
    assert len(web_evidence) == 1
    assert web_evidence[0].source_uri == "https://learn.microsoft.com/official"
    assert middleware._evidence_from_tool_result(
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
        ]
    )[0].source_uri == "https://example.test/nested"
    assert middleware._evidence_from_tool_result(
        "web_search",
        Content.from_function_result(
            "call-2",
            result=Content("text", items=[Content("text")]),
        ),
    ) == ()
    assert middleware._evidence_from_tool_result(
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
    )[0].source_uri == "https://example.test/item"
    assert middleware._evidence_from_tool_result("web_search", "untrusted") == ()
    connector_json = json.dumps(connector_result)
    assert connector_middleware._evidence_from_tool_result(
        "searchLiteratureMetadata",
        Content.from_mcp_server_tool_result("call-3", output=connector_json),
    ) == connector_evidence
    assert connector_middleware._dict_payloads(
        LiteratureResponse(summary="model")
    ) == (LiteratureResponse(summary="model").model_dump(mode="json"),)
    assert connector_middleware._dict_payloads(
        Content.from_function_result("call-4", result=connector_result)
    ) == (connector_result,)
    assert connector_middleware._dict_payloads(
        Content("text", items=[Content.from_text(text=connector_json)])
    ) == (connector_result,)
    assert connector_middleware._dict_payloads(
        Content.from_text(text=connector_json)
    ) == (connector_result,)
    assert connector_middleware._dict_payloads(Content("text")) == ()
    assert connector_middleware._dict_payloads([connector_result]) == (
        connector_result,
    )
    assert connector_middleware._dict_payloads("not-json") == ()
    assert connector_middleware._dict_payloads(42) == ()
    assert connector_middleware._evidence_from_tool_result(
        "code_interpreter",
        connector_result,
    ) == ()
    exposed = connector_middleware._expose_authorized_evidence
    assert exposed([], connector_evidence)[-1].text is not None
    assert exposed((), connector_evidence)[-1].text is not None
    assert isinstance(exposed(Content("text"), connector_evidence), list)
    assert "authorized_evidence" in exposed("result", connector_evidence)
    assert exposed(42, connector_evidence)["authorized_evidence"]
    assert exposed(connector_result, ()) is connector_result
    assert exposed(LiteratureResponse(summary="model"), connector_evidence)[
        "authorized_evidence"
    ]
    with pytest.raises(ConfigurationError, match="resolved runtime settings"):
        middleware_for_manifest(
            online_manifest,
            None,
            (online_capability,),
        )

    write_capability = CapabilityDescriptor(
        id="dataset.compute",
        operation=OperationClass.PRIVILEGED,
        required_scopes=frozenset({"research.dataset.compute"}),
        allowed_destinations=("toolbox.example",),
        side_effect_destinations=("toolbox.example",),
        approval=ApprovalMode.REQUIRED,
        idempotency=IdempotencyMode.REQUIRED,
    )
    write_middleware = GovernedFunctionMiddleware(
        write_capability,
        _binding(write_capability.id),
        current_instance_fingerprints="1" * 64,
    )
    write_governance = InvocationContext(
        tenant_id="tenant-a",
        principal_id="principal-a",
        scopes=frozenset({"research.dataset.compute"}),
        destination="toolbox.example",
        approved_capabilities=frozenset({"dataset.compute"}),
        idempotency_key="session-a",
    )
    executions: list[int] = []

    async def invoke_with(value: int) -> Any:
        write_context = FunctionInvocationContext(
            cast(Any, SimpleNamespace(name="compute")),
            {"value": value},
            kwargs={
                "governance_context": write_governance.model_dump(mode="json"),
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
    request = CoordinatorRequest.model_validate(_request(requested_capabilities=["literature", "grant"]))
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

    workflow = build_coordinator_workflow(invoke)
    request = CoordinatorRequest.model_validate(_request(requested_capabilities=["literature", "grant"]))
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

    workflow = build_coordinator_workflow(invoke)
    with pytest.raises(ContractError, match="final user"):
        await workflow.run([])
    with pytest.raises(ContractError, match="final user"):
        await workflow.run([Message(role="assistant", contents=["{}"])])
    with pytest.raises(ContractError, match="invalid"):
        await workflow.run([Message(role="user", contents=["{}"])])

    request = CoordinatorRequest.model_validate(_request(requested_capabilities=["literature"]))
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
    assert payload["approved_compute"] is False
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
    coordinator_agent = coordinator.build_agent(settings=_settings(), invoker=invoke)
    assert ResponsesHostServer(coordinator_agent) is not None


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
        assert (
            module.build_agent(
                client=_FakeChatClient(),
                settings=_settings(
                    model_deployment_name=module.MANIFEST.model_policy.deployment_name,
                    model_deployment_version=module.MANIFEST.model_policy.pinned_model_version,
                    toolbox_endpoint="https://toolbox.example/toolboxes/research/mcp",
                ),
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

    coordinator_agent = coordinator.build_agent(settings=_settings(), invoker=invoke)
    assert isinstance(coordinator_agent, WorkflowAgent)
    assert coordinator_agent.name == coordinator.MANIFEST.name
    assert set(ids) == {manifest.id for manifest in list_manifests()}
