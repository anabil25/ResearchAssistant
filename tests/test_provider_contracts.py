from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from time import sleep as blocking_sleep
from typing import Any

import httpx
import pytest
from research_assistant_connectors.providers import (
    AccessToken,
    ApprovalPolicy,
    AuthConfig,
    AuthMode,
    BindabilityDecision,
    BindabilityReason,
    BindingChangeCategory,
    BlobConfig,
    CapabilityDescriptor,
    CapabilityInstance,
    CapabilityRecord,
    DiscoveryResult,
    FoundryConfig,
    FunctionPolicy,
    FunctionsConfig,
    GitHubConfig,
    GraphConfig,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    Maturity,
    MCPConfig,
    NeedsConsentError,
    OpenAPIConfig,
    OpenAPIOperationPolicy,
    OperationClass,
    OperationDescriptor,
    PolicyError,
    ProvenanceRecord,
    ProviderDescriptor,
    ProviderEnvironment,
    ProviderFactory,
    ProviderRegistry,
    ProviderTimeoutError,
    ProviderValidationError,
    RateLimitError,
    Readiness,
    SearchConfig,
    StaleBindingError,
    ToolRegistration,
    UnauthorizedError,
    UnavailableError,
    UpstreamError,
    WebhookConfig,
    approval_decision,
    bindability_decisions,
    canonical_json_hash,
    capability_binding,
    capability_instance_fingerprint,
    operation_allows_retry,
)
from research_assistant_connectors.providers._http import (
    _retry_after,
    auth_headers,
    base64_encoded_length,
    collection,
    json_object,
    request_signing_credential,
    safe_url,
    send,
    signing_credential,
    stable_resource_id,
)
from research_assistant_connectors.providers.contracts import (
    audit_metadata,
    capability_instance,
    capability_operations_digest,
    discovery_result,
    find_operation,
    official_provenance,
    resolve_capability_target,
    validate_binding,
    validate_json,
)


class Credential:
    def __init__(self, *, token: str = "token", secret: str = "secret") -> None:
        self.token = token
        self.secret = secret

    def get_token(self, *scopes: str) -> AccessToken:
        assert scopes
        return AccessToken(self.token, 2_000_000_000)

    def get_secret(self, name: str) -> str:
        assert name
        return self.secret

    def sign(self, payload: bytes, *, algorithm: str) -> str:
        return f"{algorithm}:{len(payload)}"

    def authorization(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        content_length: int,
    ) -> str:
        return f"SharedKey {method}:{url}:{len(headers)}:{content_length}"


def context(
    handler: Any = None,
    *,
    credential: object | None = None,
    tenant: str = "tenant",
    sleep: Any = None,
) -> InvocationContext:
    transport = httpx.MockTransport(handler or (lambda _: httpx.Response(200, json={})))
    return InvocationContext(
        tenant_id=tenant,
        principal_id="principal",
        project_id="resource",
        credential=Credential() if credential is None else credential,
        transport=httpx.Client(transport=transport),
        correlation_id="correlation",
        trace_id="trace",
        sleep=sleep or (lambda _: None),
        consume_approval=lambda _: True,
    )


def operation(
    *,
    operation_id: str = "operation",
    maturity: Maturity = Maturity.GA,
    approval: ApprovalPolicy = ApprovalPolicy.NEVER,
    idempotency: Idempotency = Idempotency.PROVIDER_NATIVE,
) -> OperationDescriptor:
    return OperationDescriptor(
        operation_id,
        "1.0.0",
        maturity,
        {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
        {"type": "object"},
        OperationClass.READ,
        approval,
        idempotency=idempotency,
    )


def capability(
    *,
    readiness: Readiness = Readiness.READY,
    op: OperationDescriptor | None = None,
) -> CapabilityInstance:
    record = capability_instance(
        provider_id="provider",
        instance_id="capability",
        descriptor_id="descriptor",
        family="family",
        resource_kind="resource",
        name="Capability",
        readiness=readiness,
        auth_modes=(AuthMode.NONE,),
        tenant_boundary="tenant",
        data_boundary="resource",
        operations=(op or operation(),),
        provenance=official_provenance(
            ("https://example.test/docs",),
            source_version="test-v1",
            last_verified_at="2026-07-23T08:37:02Z",
        ),
        descriptor_metadata={"nested": {"items": [1, 2]}},
        configuration={"request_limits": {"max_upload_bytes": 2}},
        status_evidence=("tested",),
        unavailable_reason=None if readiness is Readiness.READY else "not ready",
        discovered_version="1.0.0",
    )
    _DESCRIPTORS[record.descriptor.descriptor_digest] = record.descriptor
    return record.instance


_DESCRIPTORS: dict[str, CapabilityDescriptor] = {}


def descriptor_of(instance: CapabilityInstance) -> CapabilityDescriptor:
    return _DESCRIPTORS[instance.descriptor_digest]


def discovery(instance: CapabilityInstance) -> DiscoveryResult:
    return discovery_result((CapabilityRecord(descriptor_of(instance), instance),))


def test_instance_fingerprint_is_canonical_secret_free_and_ignores_volatile_state() -> None:
    config = {
        "provider_endpoint": "https://provider.test",
        "request_limits": {"max_upload_bytes": 2},
    }
    first_operation = replace(
        operation(operation_id="z.read"),
        least_privilege_scopes=("scope.z", "scope.a"),
        least_privilege_roles=("Role Z", "Role A"),
        docs=("https://example.test/z", "https://example.test/a"),
        audit_events=("completed", "started"),
    )
    second_operation = operation(operation_id="a.read")

    def record(
        *,
        operations: tuple[OperationDescriptor, ...],
        auth_modes: tuple[AuthMode, ...],
        scopes: tuple[str, ...],
        destinations: tuple[str, ...],
        configuration: Mapping[str, Any],
    ) -> CapabilityRecord:
        return capability_instance(
            provider_id="provider",
            instance_id="canonical",
            descriptor_id="canonical-descriptor",
            family="family",
            resource_kind="resource",
            name="Canonical",
            readiness=Readiness.READY,
            auth_modes=auth_modes,
            selected_auth_mode=AuthMode.OAUTH,
            tenant_boundary="tenant",
            data_boundary="project",
            resource_id="/resources/canonical",
            connection_id="connection",
            connection_scopes=scopes,
            allowed_destination_constraints=destinations,
            operations=operations,
            provenance=official_provenance(
                ("https://example.test/docs-b", "https://example.test/docs-a"),
                source_version="test-v1",
                last_verified_at="2026-07-23T08:37:02Z",
            ),
            configuration=configuration,
            status_evidence=("tested",),
        )

    first = record(
        operations=(first_operation, second_operation),
        auth_modes=(AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
        scopes=("scope.z", "scope.a"),
        destinations=("https://z.test", "https://a.test"),
        configuration=config,
    )
    second = record(
        operations=(second_operation, first_operation),
        auth_modes=(AuthMode.MANAGED_IDENTITY, AuthMode.OAUTH),
        scopes=("scope.a", "scope.z"),
        destinations=("https://a.test", "https://z.test"),
        configuration={
            "request_limits": {"max_upload_bytes": 2},
            "provider_endpoint": "https://provider.test",
        },
    )
    first_hash = capability_instance_fingerprint(
        first.instance,
        first.descriptor,
        policy_ref="policy-v1",
    )
    second_hash = capability_instance_fingerprint(
        second.instance,
        second.descriptor,
        policy_ref="policy-v1",
    )
    assert first_hash == second_hash
    assert first.descriptor.descriptor_digest == second.descriptor.descriptor_digest
    assert first_hash == first_hash.lower() and len(first_hash) == 64
    refreshed_descriptor = replace(
        first.descriptor,
        provenance=tuple(
            replace(record, last_verified_at="2026-07-24T08:37:02Z")
            for record in first.descriptor.provenance
        ),
    )
    refreshed_instance = replace(
        first.instance,
        descriptor_digest=refreshed_descriptor.descriptor_digest,
    )
    assert refreshed_descriptor.descriptor_digest == first.descriptor.descriptor_digest
    assert (
        capability_instance_fingerprint(
            refreshed_instance,
            refreshed_descriptor,
            policy_ref="policy-v1",
        )
        == first_hash
    )

    config["provider_endpoint"] = "https://mutated.test"
    config["request_limits"]["max_upload_bytes"] = 3  # type: ignore[index]
    assert (
        capability_instance_fingerprint(
            first.instance,
            first.descriptor,
            policy_ref="policy-v1",
        )
        == first_hash
    )
    volatile = replace(
        first.instance,
        readiness=Readiness.DEGRADED,
        health=Readiness.UNAVAILABLE,
        unavailable_reason="temporary outage",
        last_checked_at="2026-07-24T08:37:02Z",
        config_validated=False,
    )
    assert (
        capability_instance_fingerprint(
            volatile,
            first.descriptor,
            policy_ref="policy-v1",
        )
        == first_hash
    )
    with pytest.raises(ValueError, match="non-binding-safe"):
        replace(first.instance, configuration={"access_token": "do-not-store"}, config_fingerprint="")
    with pytest.raises(ValueError, match="non-binding-safe"):
        replace(
            first.instance,
            configuration={"request_limits": [{"Authorization": "do-not-store"}]},
            config_fingerprint="",
        )
    with pytest.raises(ValueError, match="connection scopes"):
        replace(first.instance, connection_scopes=("scope", "scope"))
    with pytest.raises(ValueError, match="selected auth mode"):
        capability_instance(
            provider_id="provider",
            instance_id="ambiguous-auth",
            family="family",
            resource_kind="resource",
            name="Ambiguous auth",
            readiness=Readiness.READY,
            auth_modes=(AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
            tenant_boundary="tenant",
            data_boundary="project",
            operations=(operation(),),
            provenance=first.descriptor.provenance,
            status_evidence=("tested",),
        )
    with pytest.raises(ValueError, match="descriptor reference"):
        capability_instance_fingerprint(
            first.instance,
            replace(first.descriptor, descriptor_id="other"),
            policy_ref="policy-v1",
        )
    with pytest.raises(ValueError, match="policy reference"):
        capability_instance_fingerprint(first.instance, first.descriptor, policy_ref="")


def test_stale_binding_reports_deterministic_non_secret_change_categories() -> None:
    instance = capability()
    descriptor = descriptor_of(instance)
    operation_descriptor = descriptor.operations[0]
    binding = capability_binding(
        binding_id="binding",
        instance=instance,
        descriptor=descriptor,
        operation=operation_descriptor,
        policy_ref="policy-v1",
    )
    changes = (
        (
            replace(instance, provider_resource_id="changed-resource"),
            descriptor,
            None,
            (BindingChangeCategory.INSTANCE,),
        ),
        (
            replace(instance, discovered_resource_version="2.0.0"),
            descriptor,
            None,
            (BindingChangeCategory.INSTANCE,),
        ),
        (
            replace(instance, tenant_id="other-tenant"),
            descriptor,
            None,
            (BindingChangeCategory.BOUNDARY,),
        ),
        (
            replace(
                instance,
                connection_ref="other-connection",
                auth_mode=AuthMode.OAUTH,
                connection_scopes=("scope.read",),
            ),
            descriptor,
            None,
            (BindingChangeCategory.CONNECTION,),
        ),
        (
            replace(instance, allowed_destination_constraints=("https://destination.test",)),
            descriptor,
            None,
            (BindingChangeCategory.DESTINATIONS,),
        ),
        (
            replace(instance, configuration={"source": "changed"}, config_fingerprint=""),
            descriptor,
            None,
            (BindingChangeCategory.CONFIGURATION,),
        ),
        (
            instance,
            descriptor,
            "policy-v2",
            (BindingChangeCategory.POLICY,),
        ),
    )
    for current, current_descriptor, policy_ref, expected in changes:
        with pytest.raises(StaleBindingError) as caught:
            validate_binding(
                current,
                current_descriptor,
                binding,
                policy_ref=policy_ref,
            )
        assert caught.value.old_fingerprint == binding.instance_fingerprint
        assert caught.value.new_fingerprint != binding.instance_fingerprint
        assert caught.value.changed_categories == expected
        assert "source" not in str(caught.value)

    changed_operation = replace(operation_descriptor, input_schema={"type": "object"})
    changed_descriptor = replace(descriptor, operations=(changed_operation,))
    changed_instance = replace(
        instance,
        descriptor_digest=changed_descriptor.descriptor_digest,
    )
    with pytest.raises(StaleBindingError) as operation_change:
        validate_binding(changed_instance, changed_descriptor, binding)
    assert operation_change.value.changed_categories == (
        BindingChangeCategory.DESCRIPTOR,
        BindingChangeCategory.OPERATIONS,
    )
    renamed_descriptor = replace(descriptor, name="Renamed descriptor")
    renamed_instance = replace(
        instance,
        descriptor_digest=renamed_descriptor.descriptor_digest,
    )
    with pytest.raises(StaleBindingError) as descriptor_change:
        validate_binding(renamed_instance, renamed_descriptor, binding)
    assert descriptor_change.value.changed_categories == (BindingChangeCategory.DESCRIPTOR,)
    with pytest.raises(StaleBindingError) as provider_change:
        validate_binding(
            instance,
            descriptor,
            replace(binding, provider_id="other", instance_fingerprint="0" * 64),
        )
    assert provider_change.value.changed_categories == (BindingChangeCategory.PROVIDER,)
    with pytest.raises(ValueError, match="different provider"):
        validate_binding(instance, descriptor, replace(binding, provider_id="other"))
    with pytest.raises(ValueError, match="tenant or project"):
        validate_binding(instance, descriptor, replace(binding, tenant_id="other"))
    with pytest.raises(ValueError, match="discovered instance version"):
        validate_binding(
            instance,
            descriptor,
            replace(binding, instance_discovered_resource_version="other"),
        )
    with pytest.raises(ValueError, match="connection scopes"):
        replace(binding, connection_scopes=("scope", "scope"))
    with pytest.raises(ValueError, match="non-empty and unique"):
        StaleBindingError(
            provider_id="provider",
            instance_id="capability",
            old_fingerprint="0" * 64,
            new_fingerprint="1" * 64,
            changed_categories=(),
        )


def test_raw_instance_drift_reports_each_changed_field_category() -> None:
    original = capability()
    cases = (
        (replace(original, provider_id="other"), BindingChangeCategory.PROVIDER),
        (replace(original, provider_resource_id="other"), BindingChangeCategory.INSTANCE),
        (replace(original, discovered_provider_version="2.0.0"), BindingChangeCategory.INSTANCE),
        (replace(original, discovered_resource_version="2.0.0"), BindingChangeCategory.INSTANCE),
        (replace(original, tenant_id="other"), BindingChangeCategory.BOUNDARY),
        (replace(original, project_id="other"), BindingChangeCategory.BOUNDARY),
        (
            replace(original, connection_ref="other", auth_mode=AuthMode.OAUTH),
            BindingChangeCategory.CONNECTION,
        ),
        (replace(original, auth_mode=AuthMode.OAUTH), BindingChangeCategory.CONNECTION),
        (
            replace(original, connection_scopes=("scope.read",)),
            BindingChangeCategory.CONNECTION,
        ),
        (
            replace(original, allowed_destination_constraints=("https://destination.test",)),
            BindingChangeCategory.DESTINATIONS,
        ),
        (
            replace(original, configuration={"source": "changed"}, config_fingerprint=""),
            BindingChangeCategory.CONFIGURATION,
        ),
    )
    for current, category in cases:
        _DESCRIPTORS[current.descriptor_digest] = descriptor_of(original)
        with pytest.raises(StaleBindingError) as caught:
            resolve_capability_target(
                discovery(current),
                original,
                provider_id="provider",
                policy_ref="policy-v1",
            )
        assert category in caught.value.changed_categories

    renamed_descriptor = replace(descriptor_of(original), name="Renamed descriptor")
    renamed_instance = replace(original, descriptor_digest=renamed_descriptor.descriptor_digest)
    renamed_discovery = discovery_result(
        (CapabilityRecord(renamed_descriptor, renamed_instance),)
    )
    with pytest.raises(StaleBindingError) as descriptor_drift:
        resolve_capability_target(
            renamed_discovery,
            original,
            provider_id="provider",
            policy_ref="policy-v1",
        )
    assert descriptor_drift.value.changed_categories == (BindingChangeCategory.DESCRIPTOR,)


def test_descriptor_contracts_are_deeply_immutable_and_validate_invariants() -> None:
    instance = capability()
    descriptor = descriptor_of(instance)
    assert descriptor.metadata["nested"]["items"] == (1, 2)
    assert instance.configuration["request_limits"]["max_upload_bytes"] == 2
    with pytest.raises(TypeError):
        descriptor.metadata["new"] = "value"  # type: ignore[index]
    with pytest.raises(TypeError):
        instance.configuration["new"] = "value"  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        descriptor.name = "changed"  # type: ignore[misc]
    preview = capability(op=operation(maturity=Maturity.PREVIEW))
    assert descriptor_of(preview).operations[0].maturity is Maturity.PREVIEW
    with pytest.raises(ValueError, match="unavailable reason"):
        replace(instance, readiness=Readiness.DEGRADED)
    with pytest.raises(ValueError, match="Ready capability"):
        replace(instance, unavailable_reason="bad")
    with pytest.raises(ValueError, match="instance identity"):
        replace(instance, instance_id="")
    with pytest.raises(ValueError, match="descriptor identity"):
        replace(descriptor, descriptor_id="")
    with pytest.raises(ValueError, match="operation identifiers"):
        replace(descriptor, operations=(operation(), operation()))
    with pytest.raises(ValueError, match="identifiers, versions"):
        replace(operation(), timeout_seconds=0)
    with pytest.raises(ValueError, match="identifiers, versions"):
        replace(operation(), version="")
    with pytest.raises(ValueError, match="cannot be empty"):
        replace(operation(), side_effect_destinations=("",))
    with pytest.raises(ValueError, match="must be unique"):
        replace(operation(), side_effect_destinations=("destination", "destination"))
    with pytest.raises(ValueError, match="External side effects"):
        replace(operation(), external_side_effect=True, side_effect_destinations=())
    with pytest.raises(ValueError, match="string object keys"):
        replace(descriptor, metadata={1: "bad"})  # type: ignore[dict-item]
    with pytest.raises(ValueError, match="non-JSON"):
        replace(descriptor, metadata={"bad": object()})
    with pytest.raises(ValueError, match="finite JSON"):
        replace(descriptor, metadata={"bad": float("nan")})
    assert replace(descriptor, metadata={"finite": 1.5}).metadata["finite"] == 1.5
    with pytest.raises(ValueError, match="non-JSON"):
        replace(operation(), input_schema={"bad": object()})
    with pytest.raises(ValueError, match="non-JSON"):
        replace(instance, configuration={"bad": object()})
    with pytest.raises(ValueError, match="Provider identity"):
        ProviderDescriptor("", "family", "", "description", (), ())
    provenance = official_provenance(
        ("https://example.test/docs",),
        source_version="test-v1",
        last_verified_at="2026-07-23T08:37:02Z",
    )
    assert ProviderDescriptor("provider", "family", "Name", "description", (), provenance, (descriptor,))


def test_discovery_provenance_and_instance_state_invariants() -> None:
    instance = capability()
    assert instance.health is Readiness.READY
    assert len(instance.config_fingerprint) == 64
    assert instance.discovered_provider_version == "1.0.0"
    assert descriptor_of(capability(op=operation(maturity=Maturity.UNKNOWN))).operations[0].maturity is Maturity.UNKNOWN

    descriptor = descriptor_of(instance)
    result = discovery_result(
        (CapabilityRecord(descriptor, instance),),
        warnings=("One malformed upstream resource was ignored.",),
        refreshed_at="2026-07-23T08:37:02Z",
    )
    assert result.descriptors == (descriptor,)
    assert result.instances[0] is instance
    assert result.warnings
    assert len(result) == 1

    with pytest.raises(ValueError, match="configuration fingerprint"):
        replace(
            instance,
            configuration={"source": "changed"},
            config_fingerprint=instance.config_fingerprint,
        )

    with pytest.raises(ValueError, match="official HTTPS"):
        ProvenanceRecord("http://example.test", "v1", "2026-07-23T08:37:02Z")
    with pytest.raises(ValueError, match="ISO 8601"):
        ProvenanceRecord("https://example.test", "v1", "not-a-time")
    with pytest.raises(ValueError, match="UTC"):
        ProvenanceRecord("https://example.test", "v1", "2026-07-23T08:37:02")
    with pytest.raises(ValueError, match="descriptor identities"):
        DiscoveryResult(
            (descriptor, descriptor),
            (instance,),
            (),
            "2026-07-23T08:37:02Z",
        )
    drifted_descriptor = descriptor_of(capability(op=operation(maturity=Maturity.PREVIEW)))
    with pytest.raises(ValueError, match="descriptor identities"):
        DiscoveryResult(
            (descriptor, drifted_descriptor),
            (instance,),
            (),
            "2026-07-23T08:37:02Z",
        )
    with pytest.raises(ValueError, match="instance identities"):
        DiscoveryResult(
            (descriptor,),
            (instance, instance),
            (),
            "2026-07-23T08:37:02Z",
        )
    with pytest.raises(ValueError, match="reference a returned descriptor"):
        DiscoveryResult(
            (replace(descriptor, descriptor_id="other"),),
            (instance,),
            (),
            "2026-07-23T08:37:02Z",
        )
    with pytest.raises(ValueError, match="warnings"):
        DiscoveryResult(
            (descriptor,),
            (instance,),
            ("",),
            "2026-07-23T08:37:02Z",
        )


def test_binding_and_runtime_registration_are_separate_and_ga_pinned() -> None:
    instance = capability()
    descriptor = descriptor_of(instance)
    operation_descriptor = descriptor.operations[0]
    binding = capability_binding(
        binding_id="binding",
        instance=instance,
        descriptor=descriptor,
        operation=operation_descriptor,
        policy_ref="agent-studio-v1",
    )
    assert validate_binding(instance, descriptor, binding).operation_id == "operation"
    assert binding.instance_ref == instance.instance_id
    assert binding.pinned_provider_version == operation_descriptor.version
    assert binding.provider_version == binding.pinned_provider_version
    assert binding.operation_version == binding.pinned_provider_version
    assert binding.input_schema_digest == operation_descriptor.input_schema_digest
    assert binding.output_schema_digest == operation_descriptor.output_schema_digest
    assert binding.input_schema_digest == canonical_json_hash(operation_descriptor.input_schema)
    assert canonical_json_hash({"b": 2, "a": "\u00e9"}) == canonical_json_hash({"a": "\u00e9", "b": 2})
    assert binding.config_ref == instance.config_fingerprint
    assert binding.connection_ref is instance.connection_ref
    assert binding.policy_ref == "agent-studio-v1"
    for changed, message in (
        (replace(binding, instance_id="other"), "different instance"),
        (replace(binding, descriptor_id="other"), "different descriptor"),
        (replace(binding, operation_id="other"), "not declared"),
        (replace(binding, operation_version="2.0.0"), "not declared"),
        (replace(binding, operations_digest="0" * 64), "operation set digest"),
        (replace(binding, connection_ref="other"), "connection reference"),
        (replace(binding, input_schema_digest="0" * 64), "input schema digest"),
        (replace(binding, output_schema_digest="0" * 64), "output schema digest"),
    ):
        with pytest.raises(ValueError, match=message):
            validate_binding(instance, descriptor, changed)
    with pytest.raises(StaleBindingError) as stale:
        validate_binding(instance, descriptor, replace(binding, instance_fingerprint="0" * 64))
    assert stale.value.changed_categories == (BindingChangeCategory.DESCRIPTOR,)
    with pytest.raises(ValueError, match="config hash"):
        replace(binding, config_hash="0" * 64)
    with pytest.raises(ValueError, match="destination constraints"):
        replace(binding, allowed_destination_constraints=("",))
    with pytest.raises(ValueError, match="destination constraints"):
        replace(binding, allowed_destination_constraints=("same", "same"))
    with pytest.raises(ValueError, match="ready instance"):
        degraded = capability(readiness=Readiness.DEGRADED)
        validate_binding(degraded, descriptor_of(degraded), binding)
    preview = capability(op=operation(maturity=Maturity.PREVIEW))
    preview_descriptor = descriptor_of(preview)
    preview_binding = replace(
        binding,
        descriptor_id=preview.descriptor_id,
        descriptor_digest=preview.descriptor_digest,
        descriptor_version=preview.descriptor_version,
        operations_digest=capability_operations_digest(preview_descriptor),
        instance_fingerprint=capability_instance_fingerprint(
            preview,
            preview_descriptor,
            policy_ref=binding.policy_ref,
        ),
    )
    with pytest.raises(ValueError, match="Only GA"):
        validate_binding(preview, preview_descriptor, preview_binding)
    with pytest.raises(ValueError, match="identifiers"):
        replace(binding, binding_id="")
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(binding, instance_fingerprint="x" * 64)
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(binding, input_schema_digest="x" * 64)
    with pytest.raises(PolicyError, match="policy reference"):
        resolve_capability_target(
            discovery(instance),
            replace(binding, policy_ref="other"),
            provider_id="provider",
            policy_ref="agent-studio-v1",
        )

    changed_instance = replace(instance, provider_resource_id="changed")
    assert (
        capability_instance_fingerprint(
            changed_instance,
            descriptor,
            policy_ref=binding.policy_ref,
        )
        != binding.instance_fingerprint
    )

    async def handler(
        arguments: Mapping[str, Any],
        context: InvocationContext,
    ) -> InvocationResult:
        assert context.tenant_id == "tenant"
        return InvocationResult("provider", "capability", "operation", 200, arguments, {})

    registration = ToolRegistration("registration", binding, handler)

    async def invoke_handler() -> InvocationResult:
        return await registration.handler({"ok": True}, context())

    assert asyncio.run(invoke_handler()).output["ok"] is True
    with pytest.raises(ValueError, match="identity"):
        replace(registration, registration_id="")
    with pytest.raises(ValueError, match="callable"):
        ToolRegistration("registration", binding, object())  # type: ignore[arg-type]


def test_migration_lock_policy_scope_cancellation_and_bindability_edges() -> None:
    instance = capability()
    descriptor = descriptor_of(instance)
    operation_descriptor = descriptor.operations[0]
    binding = capability_binding(
        binding_id="binding",
        instance=instance,
        descriptor=descriptor,
        operation=operation_descriptor,
        policy_ref="agent-studio-v1",
    )
    unsupported_auth = replace(
        instance,
        auth_mode=AuthMode.OAUTH,
        connection_ref="oauth-connection",
        connection_scopes=("scope.read",),
    )
    with pytest.raises(ValueError, match="authentication mode"):
        capability_binding(
            binding_id="unsupported-auth",
            instance=unsupported_auth,
            descriptor=descriptor,
            operation=operation_descriptor,
            policy_ref="agent-studio-v1",
        )
    with pytest.raises(PolicyError, match="authentication mode"):
        find_operation(
            discovery(unsupported_auth),
            InvocationRequest(unsupported_auth, operation_descriptor.operation_id, {"value": "ok"}),
            context(),
            provider_id="provider",
            tenant_id="tenant",
        )

    with pytest.raises(ValueError, match="non-binding-safe"):
        secret_config: dict[str, Any] = {"request_limits": [{"Authorization": "sensitive"}]}
        replace(binding, config=secret_config, config_hash=canonical_json_hash(secret_config))
    with pytest.raises(ValueError, match="non-binding-safe"):
        secret_config = {"password": "sensitive"}
        replace(binding, config=secret_config, config_hash=canonical_json_hash(secret_config))
    with pytest.raises(ValueError, match="evaluated policy"):
        replace(
            operation_descriptor,
            operation_class=OperationClass.PRIVILEGED,
            approval_policy=ApprovalPolicy.NEVER,
        )
    assert replace(
        operation_descriptor,
        operation_class=OperationClass.PRIVILEGED,
        approval_policy=ApprovalPolicy.NEVER,
        policy_exception_ref="platform-policy:v1",
    )
    with pytest.raises(ValueError, match="idempotency support"):
        replace(
            operation_descriptor,
            operation_class=OperationClass.WRITE_REVERSIBLE,
            max_retries=1,
            idempotency=Idempotency.NONE,
        )
    with pytest.raises(ValueError, match="destination constraints"):
        replace(instance, allowed_destination_constraints=("",))
    with pytest.raises(ValueError, match="descriptor reference"):
        CapabilityRecord(replace(descriptor, descriptor_id="other"), instance)
    with pytest.raises(ValueError, match="unknown descriptor"):
        discovery(instance).descriptor_for(replace(instance, descriptor_digest="0" * 64))

    provenance = official_provenance(
        ("https://example.test/docs",),
        source_version="test-v2",
        last_verified_at="2026-07-23T08:37:02Z",
        retirement_date="2027-07-23T08:37:02Z",
    )
    assert provenance[0].retirement_date == "2027-07-23T08:37:02Z"

    future = replace(context(), deadline_at="2999-01-01T00:00:00Z")
    future.raise_if_cancelled_or_expired(provider_id="provider")
    with pytest.raises(ProviderTimeoutError, match="cancelled"):
        replace(context(), is_cancelled=lambda: True).raise_if_cancelled_or_expired(provider_id="provider")
    with pytest.raises(ProviderTimeoutError, match="deadline"):
        replace(context(), deadline_at="2000-01-01T00:00:00Z").raise_if_cancelled_or_expired(provider_id="provider")

    with pytest.raises(ValueError, match="consistent reasons"):
        BindabilityDecision("operation", True, (BindabilityReason.TENANT_MISMATCH,))
    preview = capability(readiness=Readiness.UNAUTHORIZED, op=operation(maturity=Maturity.PREVIEW))
    preview = replace(
        preview,
        tenant_id="other",
        project_id="other",
        health=Readiness.DEGRADED,
        config_validated=False,
    )
    _DESCRIPTORS[preview.descriptor_digest] = descriptor_of(capability(op=operation(maturity=Maturity.PREVIEW)))
    preview_discovery = discovery(preview)
    reasons = bindability_decisions(
        preview_discovery,
        preview,
        tenant_id="tenant",
        project_id="project",
        policy_ref=None,
    )[0].reason_codes
    assert set(reasons) == set(BindabilityReason)
    assert bindability_decisions(
        discovery(instance),
        instance,
        tenant_id="tenant",
        project_id="resource",
        policy_ref="agent-studio-v1",
    )[0].bindable

    different_config = {"source": "different"}
    with pytest.raises(ValueError, match="configuration reference"):
        validate_binding(
            instance,
            descriptor,
            replace(
                binding,
                config=different_config,
                config_hash=canonical_json_hash(different_config),
            ),
        )
    with pytest.raises(ValueError, match="discovered instance version"):
        validate_binding(
            instance,
            descriptor,
            replace(binding, instance_discovered_version="other"),
        )
    with pytest.raises(ValueError, match="healthy instance"):
        validate_binding(replace(instance, health=Readiness.DEGRADED), descriptor, binding)
    unvalidated_instance = replace(instance, config_validated=False)
    with pytest.raises(ValueError, match="validated configuration"):
        validate_binding(
            unvalidated_instance,
            descriptor,
            replace(
                binding,
                instance_fingerprint=capability_instance_fingerprint(
                    unvalidated_instance,
                    descriptor,
                    policy_ref=binding.policy_ref,
                ),
            ),
        )
    with pytest.raises(ValueError, match="destination constraints"):
        validate_binding(
            instance,
            descriptor,
            replace(binding, allowed_destination_constraints=("https://outside.test",)),
        )
    destination_instance = capability(op=replace(operation_descriptor, side_effect_destinations=("destination",)))
    destination_descriptor = descriptor_of(destination_instance)
    destination_operation = destination_descriptor.operations[0]
    destination_binding = capability_binding(
        binding_id="destination-binding",
        instance=destination_instance,
        descriptor=destination_descriptor,
        operation=destination_operation,
        policy_ref="agent-studio-v1",
    )
    with pytest.raises(ValueError, match="cannot be empty"):
        validate_binding(
            destination_instance,
            destination_descriptor,
            replace(destination_binding, allowed_destination_constraints=()),
        )


def test_invocation_contracts_and_json_schema_validation() -> None:
    request = InvocationRequest(capability(), "operation", {"value": "ok"})
    assert request.arguments["value"] == "ok"
    with pytest.raises(TypeError):
        request.arguments["value"] = "changed"  # type: ignore[index]
    with pytest.raises(ValueError, match="instance and operation"):
        InvocationRequest("", "operation", {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Idempotency keys"):
        InvocationRequest(capability(), "operation", {}, "bad key")
    with pytest.raises(ValueError, match="Tenant"):
        replace(context(), tenant_id="")
    assert "ok" not in repr(request)
    assert "token-value" not in repr(AccessToken("token-value", 1))
    assert "Credential" not in repr(context())
    assert "sensitive" not in repr(InvocationResult("p", "c", "o", 200, {"sensitive": True}, {}))

    validate_json({"type": "array", "items": {"type": "integer"}}, [1, 2])
    validate_json({"type": "number"}, 1.5)
    validate_json({"type": "boolean"}, True)
    validate_json({"type": "null"}, None)
    validate_json({"enum": ["a"]}, "a")
    for schema, value, message in (
        ({"type": "array"}, "x", "must be array"),
        ({"type": "integer"}, True, "must be integer"),
        ({"type": "string"}, 1, "must be string"),
        ({"enum": ["a"]}, "b", "declared values"),
        ({"type": "object", "required": ["x"]}, {}, "is required"),
        (
            {"type": "object", "properties": {}, "additionalProperties": False},
            {"x": 1},
            "unsupported properties",
        ),
        (
            {"type": "object", "properties": {"x": {"type": "string"}}},
            {"x": 1},
            "must be string",
        ),
        (
            {"type": "array", "items": {"type": "string"}},
            [1],
            "must be string",
        ),
    ):
        with pytest.raises(ValueError, match=message):
            validate_json(schema, value)


def test_policy_gate_covers_tenant_readiness_approval_idempotency_and_validation() -> None:
    ctx = context()
    ready = capability()
    request = InvocationRequest(ready, "operation", {"value": "ok"})
    instance, op = find_operation(
        discovery(ready),
        request,
        ctx,
        provider_id="provider",
        tenant_id="tenant",
    )
    assert instance.instance_id == "capability"
    assert op.operation_id == "operation"
    with pytest.raises(PolicyError, match="tenant"):
        find_operation(
            discovery(ready),
            request,
            ctx,
            provider_id="provider",
            tenant_id="other",
        )
    with pytest.raises(PolicyError, match="capability boundary"):
        find_operation(
            discovery(ready),
            request,
            replace(ctx, tenant_id="other"),
            provider_id="provider",
            tenant_id=None,
        )
    with pytest.raises(PolicyError, match="project"):
        find_operation(
            discovery(ready),
            request,
            replace(ctx, project_id="other"),
            provider_id="provider",
            tenant_id=None,
        )
    with pytest.raises(UnavailableError, match="not present"):
        find_operation(
            discovery(ready),
            replace(request, target=replace(ready, instance_id="missing")),
            ctx,
            provider_id="provider",
            tenant_id=None,
        )
    degraded = capability(readiness=Readiness.DEGRADED)
    with pytest.raises(UnavailableError, match="not ready"):
        find_operation(
            discovery(degraded),
            replace(request, target=degraded),
            ctx,
            provider_id="provider",
            tenant_id=None,
        )
    unhealthy = replace(ready, health=Readiness.DEGRADED)
    with pytest.raises(UnavailableError, match="health"):
        find_operation(
            discovery(unhealthy),
            replace(request, target=unhealthy),
            ctx,
            provider_id="provider",
            tenant_id=None,
        )
    unvalidated = replace(ready, config_validated=False)
    with pytest.raises(PolicyError, match="not validated"):
        find_operation(
            discovery(unvalidated),
            replace(request, target=unvalidated),
            ctx,
            provider_id="provider",
            tenant_id=None,
        )
    with pytest.raises(ProviderValidationError, match="not declared"):
        find_operation(
            discovery(ready),
            replace(request, operation_id="missing"),
            ctx,
            provider_id="provider",
            tenant_id=None,
        )
    preview = capability(op=operation(maturity=Maturity.PREVIEW))
    with pytest.raises(UnavailableError, match="Only GA"):
        find_operation(
            discovery(preview),
            replace(request, target=preview),
            ctx,
            provider_id="provider",
            tenant_id=None,
        )
    approval_cap = capability(op=operation(approval=ApprovalPolicy.REQUIRED))
    approval_request = replace(request, target=approval_cap)
    with pytest.raises(PolicyError, match="approval"):
        find_operation(
            discovery(approval_cap),
            approval_request,
            ctx,
            provider_id="provider",
            tenant_id=None,
        )
    required = capability(op=operation(idempotency=Idempotency.CALLER_KEY))
    required_request = replace(request, target=required)
    with pytest.raises(PolicyError, match="idempotency"):
        find_operation(
            discovery(required),
            required_request,
            ctx,
            provider_id="provider",
            tenant_id=None,
        )
    with pytest.raises(ProviderValidationError, match="required"):
        find_operation(
            discovery(ready),
            replace(request, arguments={}),
            ctx,
            provider_id="provider",
            tenant_id=None,
        )
    find_operation(
        discovery(required),
        replace(required_request, idempotency_key="key"),
        ctx,
        provider_id="provider",
        tenant_id=None,
    )
    decision = approval_decision(
        ctx,
        target=approval_cap,
        instance=approval_cap,
        descriptor=descriptor_of(approval_cap),
        operation=descriptor_of(approval_cap).operations[0],
        arguments=approval_request.arguments,
        decision_id="decision",
        expires_at="2999-01-01T00:00:00Z",
    )
    approved = replace(ctx, approval_decisions=(decision,))
    find_operation(
        discovery(approval_cap),
        approval_request,
        approved,
        provider_id="provider",
        tenant_id=None,
    )
    audit = audit_metadata(
        ctx,
        provider_id="provider",
        instance_id="capability",
        operation_id="operation",
        attempts=2,
        response=httpx.Response(
            200,
            extensions={"provider_elapsed_ms": 1.5},
        ),
    )
    assert audit["correlation_id"] == "correlation"
    assert audit["attempts"] == 2
    assert audit["status_code"] == 200
    assert audit["latency_ms"] == 1.5


def test_approval_decisions_bind_every_authorization_dimension() -> None:
    ctx = context()
    instance = capability(op=operation(approval=ApprovalPolicy.REQUIRED))
    descriptor = descriptor_of(instance)
    operation_descriptor = descriptor.operations[0]
    binding = capability_binding(
        binding_id="binding",
        instance=instance,
        descriptor=descriptor,
        operation=operation_descriptor,
        policy_ref=ctx.policy_release,
    )
    request = InvocationRequest(binding, operation_descriptor.operation_id, {"value": "ok"})
    for out_of_scope in (replace(ctx, tenant_id="other"), replace(ctx, project_id="other")):
        with pytest.raises(PolicyError, match="Approval scope"):
            approval_decision(
                out_of_scope,
                target=binding,
                instance=instance,
                descriptor=descriptor,
                operation=operation_descriptor,
                arguments=request.arguments,
                decision_id="out-of-scope",
                expires_at="2999-01-01T00:00:00Z",
            )
    decision = approval_decision(
        ctx,
        target=binding,
        instance=instance,
        descriptor=descriptor,
        operation=operation_descriptor,
        arguments=request.arguments,
        decision_id="decision",
        expires_at="2999-01-01T00:00:00Z",
    )
    approved = replace(ctx, approval_decisions=(decision,))
    found, found_operation = find_operation(
        discovery(instance),
        request,
        approved,
        provider_id="provider",
        tenant_id="tenant",
    )
    assert found is instance
    assert found_operation is operation_descriptor
    consumed: set[str] = set()

    def consume_once(decision_id: str) -> bool:
        if decision_id in consumed:
            return False
        consumed.add(decision_id)
        return True

    single_use = replace(approved, consume_approval=consume_once)
    find_operation(
        discovery(instance),
        request,
        single_use,
        provider_id="provider",
        tenant_id="tenant",
    )
    with pytest.raises(PolicyError, match="already been consumed"):
        find_operation(
            discovery(instance),
            request,
            single_use,
            provider_id="provider",
            tenant_id="tenant",
        )

    for tampered in (
        replace(decision, approved=False),
        replace(decision, tenant_id="other"),
        replace(decision, principal_id="other"),
        replace(decision, instance_id="other"),
        replace(decision, project_id="other"),
        replace(decision, provider_resource_id="other"),
        replace(decision, instance_fingerprint="0" * 64),
        replace(decision, descriptor_id="other"),
        replace(decision, operation_id="other"),
        replace(decision, operation_version="2.0.0"),
        replace(decision, arguments_hash="0" * 64),
        replace(decision, destination_hash="0" * 64),
        replace(decision, policy_release="other"),
        replace(decision, binding_id="other"),
        replace(decision, expires_at="2000-01-01T00:00:00Z"),
    ):
        with pytest.raises(PolicyError, match="approval decision"):
            find_operation(
                discovery(instance),
                request,
                replace(ctx, approval_decisions=(tampered,)),
                provider_id="provider",
                tenant_id="tenant",
            )

    with pytest.raises(ValueError, match="identity"):
        replace(decision, decision_id="")
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        replace(decision, arguments_hash="bad")
    with pytest.raises(ValueError, match="match the capability binding"):
        replace(request, operation_id="other")
    stale = replace(instance, configuration={"source": "changed"}, config_fingerprint="")
    _DESCRIPTORS[stale.descriptor_digest] = descriptor
    with pytest.raises(StaleBindingError) as stale_instance:
        resolve_capability_target(
            discovery(stale),
            instance,
            provider_id="provider",
        )
    assert stale_instance.value.changed_categories == (BindingChangeCategory.CONFIGURATION,)
    with pytest.raises(StaleBindingError) as stale_binding:
        resolve_capability_target(
            discovery(stale),
            binding,
            provider_id="provider",
        )
    assert stale_binding.value.changed_categories == (BindingChangeCategory.CONFIGURATION,)


def test_retry_policy_never_replays_streams_or_irreversible_writes() -> None:
    read = replace(operation(), max_retries=2)
    reversible = replace(
        read,
        operation_class=OperationClass.WRITE_REVERSIBLE,
        idempotency=Idempotency.CALLER_KEY,
    )
    irreversible = replace(
        read,
        operation_class=OperationClass.WRITE_IRREVERSIBLE,
        idempotency=Idempotency.PROVIDER_NATIVE,
        approval_policy=ApprovalPolicy.REQUIRED,
    )
    assert operation_allows_retry(read, idempotency_key=None)
    assert not operation_allows_retry(read, idempotency_key=None, stream_started=True)
    assert not operation_allows_retry(reversible, idempotency_key=None)
    assert operation_allows_retry(reversible, idempotency_key="key")
    assert not operation_allows_retry(irreversible, idempotency_key="key")


def test_url_auth_and_collection_helpers() -> None:
    assert safe_url("https://service.test/root", "/items") == "https://service.test/root/items"
    assert safe_url("http://127.0.0.1:8000", "/mcp") == "http://127.0.0.1:8000/mcp"
    assert stable_resource_id("prefix", "A Resource") == stable_resource_id("prefix", "A Resource")
    for bad in ("ftp://service.test", "https://user:pass@service.test", "not-a-url"):
        with pytest.raises(ValueError, match="valid HTTP"):
            safe_url(bad, "/")
    with pytest.raises(ValueError, match="traversal"):
        safe_url("https://service.test", "../secret")
    with pytest.raises(ValueError, match="override"):
        safe_url("https://service.test/root", "/https://evil.test/secret")
    with pytest.raises(ValueError, match="loopback"):
        safe_url("http://service.test", "/")

    ctx = context()
    assert auth_headers(AuthConfig(AuthMode.NONE), ctx, provider_id="p") == {}
    assert (
        auth_headers(
            AuthConfig(AuthMode.SIGNATURE),
            replace(ctx, credential=object()),
            provider_id="p",
            allow_signature=True,
        )
        == {}
    )
    with pytest.raises(UnauthorizedError, match="not supported"):
        auth_headers(AuthConfig(AuthMode.SIGNATURE), ctx, provider_id="p")
    assert auth_headers(AuthConfig(AuthMode.OAUTH, "scope"), ctx, provider_id="p")["Authorization"] == "Bearer token"
    assert auth_headers(AuthConfig(AuthMode.GITHUB_APP, "scope"), ctx, provider_id="p")["Authorization"].startswith(
        "Bearer "
    )
    assert auth_headers(
        AuthConfig(AuthMode.API_KEY, secret_name="key", header_name="x-key"),
        ctx,
        provider_id="p",
    ) == {"x-key": "secret"}
    assert signing_credential(ctx, provider_id="p").sign(b"x", algorithm="hmac") == "hmac:1"
    assert (
        request_signing_credential(ctx, provider_id="p")
        .authorization(method="GET", url="https://x", headers={}, content_length=0)
        .startswith("SharedKey")
    )
    for auth in (
        AuthConfig(AuthMode.OAUTH),
        AuthConfig(AuthMode.API_KEY, secret_name="x"),
    ):
        with pytest.raises(UnauthorizedError):
            auth_headers(auth, replace(ctx, credential=object()), provider_id="p")
    with pytest.raises(UnauthorizedError, match="empty token"):
        auth_headers(AuthConfig(AuthMode.OAUTH, "scope"), context(credential=Credential(token="")), provider_id="p")
    with pytest.raises(UnauthorizedError, match="empty value"):
        auth_headers(
            AuthConfig(AuthMode.API_KEY, secret_name="x", header_name="x"),
            context(credential=Credential(secret="")),
            provider_id="p",
        )
    with pytest.raises(UnauthorizedError):
        signing_credential(replace(ctx, credential=object()), provider_id="p")
    with pytest.raises(UnauthorizedError):
        request_signing_credential(replace(ctx, credential=object()), provider_id="p")
    assert base64_encoded_length(0) == 0
    assert base64_encoded_length(1) == 4
    with pytest.raises(ValueError, match="negative"):
        base64_encoded_length(-1)
    assert collection({"value": [{"id": 1}, "bad"]}) == ({"id": 1},)
    assert collection({"other": []}) == ()


def test_http_helper_retries_timeouts_status_mapping_and_safe_json() -> None:
    sleeps: list[float] = []
    calls = 0

    def retry_handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    response, attempts = send(
        context(retry_handler, sleep=sleeps.append),
        provider_id="p",
        method="GET",
        url="https://service.test",
        max_retries=1,
        idempotent=True,
    )
    assert response.json() == {"ok": True}
    assert attempts == 2
    assert sleeps == [0.0]

    deadline = (datetime.now(UTC) + timedelta(milliseconds=50)).isoformat()

    def slow_handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"]["read"] < 1
        blocking_sleep(0.1)
        return httpx.Response(200, json={})

    with pytest.raises(ProviderTimeoutError, match="deadline"):
        send(
            replace(context(slow_handler), deadline_at=deadline),
            provider_id="p",
            method="GET",
            url="https://service.test",
            idempotent=True,
        )

    capped_sleeps: list[float] = []
    capped_calls = 0

    def capped_handler(_: httpx.Request) -> httpx.Response:
        nonlocal capped_calls
        capped_calls += 1
        return (
            httpx.Response(503, headers={"Retry-After": "999"}) if capped_calls == 1 else httpx.Response(200, json={})
        )

    send(
        context(capped_handler, sleep=capped_sleeps.append),
        provider_id="p",
        method="GET",
        url="https://service.test",
        max_retries=1,
        idempotent=True,
    )
    assert sum(capped_sleeps) == pytest.approx(30.0)
    assert max(capped_sleeps) <= 0.1

    redirects = 0

    def redirect_handler(_: httpx.Request) -> httpx.Response:
        nonlocal redirects
        redirects += 1
        return httpx.Response(302, headers={"Location": "https://evil.test"})

    with pytest.raises(UpstreamError, match="redirects"):
        send(
            context(redirect_handler),
            provider_id="p",
            method="GET",
            url="https://service.test",
            idempotent=True,
        )
    assert redirects == 1

    timeout_calls = 0

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        nonlocal timeout_calls
        timeout_calls += 1
        raise httpx.ReadTimeout("timeout", request=request)

    with pytest.raises(ProviderTimeoutError):
        send(
            context(timeout_handler),
            provider_id="p",
            method="POST",
            url="https://service.test",
            idempotent=False,
        )
    assert timeout_calls == 1
    with pytest.raises(ProviderTimeoutError):
        send(
            context(timeout_handler),
            provider_id="p",
            method="GET",
            url="https://service.test",
            idempotent=True,
            max_retries=1,
        )
    assert timeout_calls == 3

    for status, error, consent, headers in (
        (401, UnauthorizedError, False, {}),
        (403, UnauthorizedError, False, {}),
        (403, NeedsConsentError, True, {}),
        (403, RateLimitError, True, {"X-RateLimit-Remaining": "0"}),
        (429, RateLimitError, False, {"Retry-After": "2"}),
        (500, UpstreamError, False, {}),
    ):
        with pytest.raises(error):
            send(
                context(lambda _, s=status, h=headers: httpx.Response(s, headers=h)),
                provider_id="p",
                method="POST",
                url="https://service.test",
                idempotent=False,
                consent_on_forbidden=consent,
            )

    assert _retry_after(httpx.Response(429, headers={"Retry-After": "1.5"})) == 1.5
    future = format_datetime(datetime.now(tz=UTC) + timedelta(seconds=10), usegmt=True)
    assert (_retry_after(httpx.Response(429, headers={"Retry-After": future})) or 0) > 0
    assert _retry_after(httpx.Response(429, headers={"Retry-After": "invalid"})) is None
    reset = str(int(datetime.now(tz=UTC).timestamp()) + 10)
    assert (_retry_after(httpx.Response(429, headers={"X-RateLimit-Reset": reset})) or 0) > 0
    assert _retry_after(httpx.Response(429)) is None

    assert json_object(httpx.Response(200, json={"ok": True}), provider_id="p") == {"ok": True}
    with pytest.raises(UpstreamError, match="invalid JSON"):
        json_object(httpx.Response(200, text="bad"), provider_id="p")
    with pytest.raises(UpstreamError, match="non-object"):
        json_object(httpx.Response(200, json=[]), provider_id="p")


def test_factory_and_registry_cover_every_configuration_type() -> None:
    auth = AuthConfig(AuthMode.NONE)
    configs = (
        FoundryConfig(None, "tenant"),
        SearchConfig(None, "tenant"),
        FunctionsConfig(None, "tenant", auth),
        BlobConfig(None, "tenant"),
        MCPConfig(None, "tenant"),
        OpenAPIConfig(None, "tenant"),
        WebhookConfig(None, "tenant", "send"),
        GitHubConfig(None, "tenant", auth),
        GraphConfig(None, "tenant"),
    )
    providers = tuple(ProviderFactory.create(config) for config in configs)
    assert len({provider.descriptor.provider_id for provider in providers}) == len(providers)
    registry = ProviderRegistry(providers)
    assert registry.get("webhook").descriptor.name == "Webhook"
    with pytest.raises(KeyError, match="Unknown provider"):
        registry.get("missing")
    with pytest.raises(ValueError, match="unique"):
        ProviderRegistry((providers[0], providers[0]))
    with pytest.raises(TypeError, match="Unsupported"):
        ProviderFactory.create(object())  # type: ignore[arg-type]
    environment = ProviderEnvironment("test", "tenant", configs)
    assert len(ProviderRegistry.from_environment(environment).providers) == len(configs)
    with pytest.raises(ValueError, match="name and tenant"):
        ProviderEnvironment("", "tenant", ())
    with pytest.raises(ValueError, match="environment tenant"):
        ProviderEnvironment("test", "other", configs)
    discovered = registry.discover_all(context())
    assert set(discovered) == set(registry.providers)
    assert FunctionPolicy("f").operation_class is OperationClass.PRIVILEGED
    assert OpenAPIOperationPolicy("op", OperationClass.READ, ApprovalPolicy.NEVER).operation_id == "op"
