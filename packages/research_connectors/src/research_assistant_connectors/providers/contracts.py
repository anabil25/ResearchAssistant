"""Immutable contracts for operational capability providers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

import httpx

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonSchema = Mapping[str, Any]


class Maturity(StrEnum):
    UNKNOWN = "unknown"
    GA = "ga"
    PREVIEW = "preview"
    RETIRED = "retired"


class Readiness(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    NEEDS_CONSENT = "needs_consent"
    MISCONFIGURED = "misconfigured"
    DEGRADED = "degraded"


class OperationClass(StrEnum):
    PURE = "pure"
    READ = "read"
    WRITE_REVERSIBLE = "write_reversible"
    WRITE_IRREVERSIBLE = "write_irreversible"
    PRIVILEGED = "privileged"


class ApprovalPolicy(StrEnum):
    NEVER = "never"
    POLICY_EVALUATED = "policy_evaluated"
    REQUIRED = "required"


class Idempotency(StrEnum):
    NONE = "none"
    CALLER_KEY = "caller_key"
    PROVIDER_NATIVE = "provider_native"


class AuthMode(StrEnum):
    OAUTH = "oauth"
    MANAGED_IDENTITY = "managed_identity"
    API_KEY = "api_key"
    SHARED_KEY = "shared_key"
    GITHUB_APP = "github_app"
    SIGNATURE = "signature"
    NONE = "none"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(_freeze(item) for item in value)
    return value


def plain_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [plain_json(item) for item in value]
    return value


def _validate_json_value(value: Any, *, path: str) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain finite JSON numbers")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} must contain only string object keys")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    raise ValueError(f"{path} contains a non-JSON value")


def canonical_json_hash(value: Any) -> str:
    _validate_json_value(value, path="hash_input")
    encoded = json.dumps(
        plain_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_sha256(value: str, *, path: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{path} must be lowercase SHA-256")


_BINDING_SAFE_CONFIGURATION_KEYS = frozenset(
    {
        "auth_header_name",
        "agents_path_digest",
        "api_version_digest",
        "container",
        "connections_path_digest",
        "deployments_path_digest",
        "drive_id",
        "full_name",
        "function_name",
        "health_method",
        "index_name",
        "item_id",
        "invoke_path_template_digest",
        "max_upload_bytes",
        "method",
        "models_path_digest",
        "path",
        "provider_endpoint",
        "provider_endpoint_digest",
        "protocol_version_digest",
        "request_limits",
        "resource_id",
        "resource_ids",
        "signature_header",
        "signing_algorithm",
        "site_id",
        "source",
        "tool_name",
        "untrusted_tool_metadata_digest",
        "vector_stores_path_digest",
        "responses_path_digest",
    }
)


def _validate_binding_safe_configuration(value: Any) -> None:
    if isinstance(value, Mapping):
        if any(key not in _BINDING_SAFE_CONFIGURATION_KEYS for key in value):
            raise ValueError("Capability configuration contains a non-binding-safe key")
        for child in value.values():
            _validate_binding_safe_configuration(child)
    elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for child in value:
            _validate_binding_safe_configuration(child)


def _validate_utc_timestamp(value: str, *, path: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{path} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError(f"{path} must be a UTC timestamp")


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ProvenanceRecord:
    official_url: str
    source_version: str
    last_verified_at: str
    retirement_date: str | None = None

    def __post_init__(self) -> None:
        if not self.official_url.startswith("https://") or not self.source_version:
            raise ValueError("Provenance requires an official HTTPS URL and source version")
        _validate_utc_timestamp(self.last_verified_at, path="provenance.last_verified_at")
        if self.retirement_date is not None:
            _validate_utc_timestamp(self.retirement_date, path="provenance.retirement_date")


def official_provenance(
    urls: tuple[str, ...],
    *,
    source_version: str,
    last_verified_at: str,
    retirement_date: str | None = None,
) -> tuple[ProvenanceRecord, ...]:
    return tuple(
        ProvenanceRecord(
            official_url=url,
            source_version=source_version,
            last_verified_at=last_verified_at,
            retirement_date=retirement_date,
        )
        for url in urls
    )


@dataclass(frozen=True, slots=True)
class OperationDescriptor:
    operation_id: str
    version: str
    maturity: Maturity
    input_schema: JsonSchema
    output_schema: JsonSchema
    operation_class: OperationClass
    approval_policy: ApprovalPolicy
    external_side_effect: bool = False
    side_effect_destinations: tuple[str, ...] = ()
    timeout_seconds: float = 20.0
    max_retries: int = 0
    idempotency: Idempotency = Idempotency.NONE
    least_privilege_scopes: tuple[str, ...] = ()
    least_privilege_roles: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()
    audit_events: tuple[str, ...] = ("provider.invoke",)
    policy_exception_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.operation_id or not self.version or self.timeout_seconds <= 0 or not 0 <= self.max_retries <= 5:
            raise ValueError("Operation identifiers, versions, timeouts, and retries must be valid")
        if any(not destination for destination in self.side_effect_destinations):
            raise ValueError("Side-effect destinations cannot be empty")
        if len(set(self.side_effect_destinations)) != len(self.side_effect_destinations):
            raise ValueError("Side-effect destinations must be unique")
        if self.external_side_effect and not self.side_effect_destinations:
            raise ValueError("External side effects require declared destination constraints")
        if (
            self.approval_policy is ApprovalPolicy.NEVER
            and self.operation_class in {OperationClass.WRITE_IRREVERSIBLE, OperationClass.PRIVILEGED}
            and not self.policy_exception_ref
        ):
            raise ValueError("Privileged and irreversible operations require evaluated policy or approval")
        if (
            self.max_retries > 0
            and self.operation_class
            in {
                OperationClass.WRITE_REVERSIBLE,
                OperationClass.WRITE_IRREVERSIBLE,
                OperationClass.PRIVILEGED,
            }
            and self.idempotency is Idempotency.NONE
        ):
            raise ValueError("Retriable writes require declared idempotency support")
        _validate_json_value(self.input_schema, path="input_schema")
        _validate_json_value(self.output_schema, path="output_schema")
        object.__setattr__(self, "input_schema", _freeze(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze(self.output_schema))

    @property
    def provider_version(self) -> str:
        return self.version

    @property
    def input_schema_digest(self) -> str:
        return canonical_json_hash(self.input_schema)

    @property
    def output_schema_digest(self) -> str:
        return canonical_json_hash(self.output_schema)


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    descriptor_id: str
    family: str
    resource_kind: str
    name: str
    auth_modes: tuple[AuthMode, ...]
    operations: tuple[OperationDescriptor, ...]
    provenance: tuple[ProvenanceRecord, ...]
    descriptor_version: str = "1.0.0"
    observability: tuple[str, ...] = ("correlation_id", "trace_id", "latency_ms", "status")
    audit: tuple[str, ...] = ("principal_id", "tenant_id", "provider_id", "instance_id", "operation_id")
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not self.descriptor_id
            or not self.descriptor_version
            or not self.family
            or not self.resource_kind
            or not self.operations
            or not self.provenance
        ):
            raise ValueError("Capability descriptor identity and operations are required")
        if len({operation.operation_id for operation in self.operations}) != len(self.operations):
            raise ValueError("Capability descriptor operation identifiers must be unique")
        _validate_json_value(self.metadata, path="descriptor.metadata")
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    @property
    def descriptor_digest(self) -> str:
        return capability_descriptor_digest(self)


def capability_descriptor_digest(descriptor: CapabilityDescriptor) -> str:
    return canonical_json_hash(
        {
            "descriptor_id": descriptor.descriptor_id,
            "descriptor_version": descriptor.descriptor_version,
            "family": descriptor.family,
            "resource_kind": descriptor.resource_kind,
            "name": descriptor.name,
            "auth_modes": sorted(mode.value for mode in descriptor.auth_modes),
            "operations": [
                _operation_governance_payload(operation)
                for operation in sorted(
                    descriptor.operations,
                    key=lambda item: (item.operation_id, item.version),
                )
            ],
            "provenance": [
                {
                    "official_url": record.official_url,
                    "source_version": record.source_version,
                    "retirement_date": record.retirement_date,
                }
                for record in sorted(
                    descriptor.provenance,
                    key=lambda item: (
                        item.official_url,
                        item.source_version,
                        item.last_verified_at,
                        item.retirement_date or "",
                    ),
                )
            ],
            "observability": sorted(descriptor.observability),
            "audit": sorted(descriptor.audit),
            "metadata": plain_json(descriptor.metadata),
        }
    )


def _operation_governance_payload(operation: OperationDescriptor) -> dict[str, Any]:
    return {
        "operation_id": operation.operation_id,
        "operation_version": operation.version,
        "maturity": operation.maturity.value,
        "input_schema_digest": operation.input_schema_digest,
        "output_schema_digest": operation.output_schema_digest,
        "operation_class": operation.operation_class.value,
        "approval_policy": operation.approval_policy.value,
        "external_side_effect": operation.external_side_effect,
        "side_effect_destinations": sorted(operation.side_effect_destinations),
        "timeout_seconds": operation.timeout_seconds,
        "max_retries": operation.max_retries,
        "idempotency": operation.idempotency.value,
        "least_privilege_scopes": sorted(operation.least_privilege_scopes),
        "least_privilege_roles": sorted(operation.least_privilege_roles),
        "docs": sorted(operation.docs),
        "audit_events": sorted(operation.audit_events),
        "policy_exception_ref": operation.policy_exception_ref,
    }


def capability_operations_digest(descriptor: CapabilityDescriptor) -> str:
    return canonical_json_hash(
        [
            _operation_governance_payload(operation)
            for operation in sorted(
                descriptor.operations,
                key=lambda item: (item.operation_id, item.version),
            )
        ]
    )


@dataclass(frozen=True, slots=True)
class CapabilityInstance:
    provider_id: str
    instance_id: str
    descriptor_id: str
    descriptor_version: str
    descriptor_digest: str
    name: str
    readiness: Readiness
    tenant_id: str
    project_id: str
    provider_resource_id: str
    discovered_provider_version: str
    discovered_resource_version: str | None
    connection_ref: str | None
    auth_mode: AuthMode
    health: Readiness
    last_checked_at: str
    configuration: Mapping[str, Any] = field(default_factory=dict)
    config_fingerprint: str = ""
    config_validated: bool = True
    connection_scopes: tuple[str, ...] = ()
    allowed_destination_constraints: tuple[str, ...] = ()
    status_evidence: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.provider_id,
                self.instance_id,
                self.descriptor_id,
                self.descriptor_version,
                self.descriptor_digest,
                self.name,
                self.tenant_id,
                self.project_id,
                self.provider_resource_id,
                self.discovered_provider_version,
            )
        ):
            raise ValueError("Capability instance identity is required")
        _validate_sha256(self.descriptor_digest, path="instance.descriptor_digest")
        _validate_utc_timestamp(self.last_checked_at, path="instance.last_checked_at")
        if self.readiness is Readiness.READY and self.unavailable_reason is not None:
            raise ValueError("Ready capability instances cannot have an unavailable reason")
        if self.readiness is not Readiness.READY and not self.unavailable_reason:
            raise ValueError("Non-ready capability instances require an unavailable reason")
        _validate_json_value(self.configuration, path="instance.configuration")
        _validate_binding_safe_configuration(self.configuration)
        object.__setattr__(self, "configuration", _freeze(self.configuration))
        fingerprint = canonical_json_hash(self.configuration)
        if self.config_fingerprint and self.config_fingerprint != fingerprint:
            raise ValueError("Capability instance configuration fingerprint does not match configuration")
        object.__setattr__(self, "config_fingerprint", fingerprint)
        if any(not constraint for constraint in self.allowed_destination_constraints):
            raise ValueError("Instance destination constraints cannot be empty")
        if any(not scope for scope in self.connection_scopes) or len(set(self.connection_scopes)) != len(
            self.connection_scopes
        ):
            raise ValueError("Instance connection scopes must be non-empty and unique")
        object.__setattr__(self, "connection_scopes", tuple(sorted(self.connection_scopes)))
        object.__setattr__(
            self,
            "allowed_destination_constraints",
            tuple(sorted(self.allowed_destination_constraints)),
        )


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    descriptor: CapabilityDescriptor
    instance: CapabilityInstance

    def __post_init__(self) -> None:
        if (
            self.instance.descriptor_id != self.descriptor.descriptor_id
            or self.instance.descriptor_version != self.descriptor.descriptor_version
            or self.instance.descriptor_digest != self.descriptor.descriptor_digest
        ):
            raise ValueError("Capability record descriptor reference does not match")


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    descriptors: tuple[CapabilityDescriptor, ...]
    instances: tuple[CapabilityInstance, ...]
    warnings: tuple[str, ...]
    refreshed_at: str

    def __post_init__(self) -> None:
        _validate_utc_timestamp(self.refreshed_at, path="discovery.refreshed_at")
        descriptor_identities = {
            (descriptor.descriptor_id, descriptor.descriptor_version) for descriptor in self.descriptors
        }
        descriptor_references = {
            (descriptor.descriptor_id, descriptor.descriptor_version, descriptor.descriptor_digest)
            for descriptor in self.descriptors
        }
        if len(descriptor_identities) != len(self.descriptors):
            raise ValueError("Discovery descriptor identities must be unique")
        if len({instance.instance_id for instance in self.instances}) != len(self.instances):
            raise ValueError("Discovery instance identities must be unique")
        if any(
            (instance.descriptor_id, instance.descriptor_version, instance.descriptor_digest)
            not in descriptor_references
            for instance in self.instances
        ):
            raise ValueError("Every discovered instance must reference a returned descriptor")
        if any(not warning for warning in self.warnings):
            raise ValueError("Discovery warnings cannot be empty")

    def __iter__(self) -> Iterator[CapabilityInstance]:
        return iter(self.instances)

    def __len__(self) -> int:
        return len(self.instances)

    def __getitem__(self, index: int) -> CapabilityInstance:
        return self.instances[index]

    def descriptor_for(self, instance: CapabilityInstance) -> CapabilityDescriptor:
        descriptor = next(
            (
                item
                for item in self.descriptors
                if item.descriptor_id == instance.descriptor_id
                and item.descriptor_version == instance.descriptor_version
                and item.descriptor_digest == instance.descriptor_digest
            ),
            None,
        )
        if descriptor is None:
            raise ValueError("Capability instance references an unknown descriptor")
        return descriptor


def discovery_result(
    records: Sequence[CapabilityRecord],
    *,
    warnings: tuple[str, ...] = (),
    refreshed_at: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> DiscoveryResult:
    canonical_records = (
        tuple(records)
        if tenant_id is None and project_id is None
        else tuple(
            CapabilityRecord(
                descriptor=record.descriptor,
                instance=replace(
                    record.instance,
                    tenant_id=tenant_id or record.instance.tenant_id,
                    project_id=project_id or record.instance.project_id,
                ),
            )
            for record in records
        )
    )
    descriptors_by_key = {
        (record.descriptor.descriptor_id, record.descriptor.descriptor_version): record.descriptor
        for record in canonical_records
    }
    return DiscoveryResult(
        descriptors=tuple(descriptors_by_key.values()),
        instances=tuple(record.instance for record in canonical_records),
        warnings=warnings,
        refreshed_at=refreshed_at or utc_now(),
    )


def capability_instance(
    *,
    provider_id: str,
    instance_id: str,
    family: str,
    resource_kind: str,
    name: str,
    readiness: Readiness,
    auth_modes: tuple[AuthMode, ...],
    tenant_boundary: str,
    data_boundary: str,
    operations: tuple[OperationDescriptor, ...],
    provenance: tuple[ProvenanceRecord, ...],
    status_evidence: tuple[str, ...],
    unavailable_reason: str | None = None,
    configuration: Mapping[str, Any] | None = None,
    selected_auth_mode: AuthMode | None = None,
    connection_scopes: tuple[str, ...] = (),
    descriptor_id: str | None = None,
    descriptor_metadata: Mapping[str, Any] | None = None,
    resource_id: str | None = None,
    connection_id: str | None = None,
    health: Readiness | None = None,
    discovered_version: str | None = None,
    descriptor_version: str = "1.0.0",
    allowed_destination_constraints: tuple[str, ...] = (),
    last_checked_at: str | None = None,
) -> CapabilityRecord:
    if selected_auth_mode is None:
        if len(auth_modes) != 1:
            raise ValueError("Capability instances with multiple supported auth modes require a selected auth mode")
        selected_auth_mode = auth_modes[0]
    descriptor = CapabilityDescriptor(
        descriptor_id=descriptor_id or instance_id,
        descriptor_version=descriptor_version,
        family=family,
        resource_kind=resource_kind,
        name=name,
        auth_modes=auth_modes,
        operations=operations,
        provenance=provenance,
        metadata=descriptor_metadata or {},
    )
    operation_versions = {operation.version for operation in descriptor.operations}
    resolved_version = discovered_version or (operation_versions.pop() if len(operation_versions) == 1 else "multiple")
    instance = CapabilityInstance(
        provider_id=provider_id,
        instance_id=instance_id,
        descriptor_id=descriptor.descriptor_id,
        descriptor_version=descriptor.descriptor_version,
        descriptor_digest=descriptor.descriptor_digest,
        name=name,
        readiness=readiness,
        tenant_id=tenant_boundary,
        project_id=data_boundary,
        provider_resource_id=resource_id or instance_id,
        discovered_provider_version=resolved_version,
        discovered_resource_version=resolved_version,
        connection_ref=connection_id,
        auth_mode=selected_auth_mode,
        health=health or readiness,
        last_checked_at=last_checked_at or utc_now(),
        configuration=configuration or {},
        connection_scopes=connection_scopes,
        allowed_destination_constraints=allowed_destination_constraints,
        status_evidence=status_evidence,
        unavailable_reason=unavailable_reason,
    )
    return CapabilityRecord(descriptor=descriptor, instance=instance)


def capability_instance_fingerprint(
    instance: CapabilityInstance,
    descriptor: CapabilityDescriptor,
    *,
    policy_ref: str,
) -> str:
    if (
        instance.descriptor_id != descriptor.descriptor_id
        or instance.descriptor_version != descriptor.descriptor_version
        or instance.descriptor_digest != descriptor.descriptor_digest
    ):
        raise ValueError("Capability fingerprint descriptor reference does not match the instance")
    if not policy_ref:
        raise ValueError("Capability fingerprint policy reference is required")
    payload = {
        "provider": {"provider_id": instance.provider_id},
        "descriptor": {
            "descriptor_id": descriptor.descriptor_id,
            "descriptor_version": descriptor.descriptor_version,
            "descriptor_digest": descriptor.descriptor_digest,
            "auth_modes": sorted(mode.value for mode in descriptor.auth_modes),
        },
        "operations": [
            _operation_governance_payload(operation)
            for operation in sorted(
                descriptor.operations,
                key=lambda item: (item.operation_id, item.version),
            )
        ],
        "instance": {
            "instance_id": instance.instance_id,
            "provider_resource_id": instance.provider_resource_id,
            "discovered_provider_version": instance.discovered_provider_version,
            "discovered_resource_version": instance.discovered_resource_version,
        },
        "boundary": {
            "tenant_id": instance.tenant_id,
            "project_id": instance.project_id,
        },
        "connection": {
            "connection_ref": instance.connection_ref,
            "auth_mode": instance.auth_mode.value,
            "connection_scopes": sorted(instance.connection_scopes),
        },
        "allowed_destination_constraints": sorted(instance.allowed_destination_constraints),
        "policy_ref": policy_ref,
        "configuration": plain_json(instance.configuration),
    }
    return canonical_json_hash(payload)


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    binding_id: str
    provider_id: str
    descriptor_id: str
    descriptor_version: str
    descriptor_digest: str
    operations_digest: str
    operation_id: str
    operation_version: str
    instance_id: str
    provider_resource_id: str
    tenant_id: str
    project_id: str
    instance_discovered_version: str
    instance_discovered_resource_version: str | None
    input_schema_digest: str
    output_schema_digest: str
    config: Mapping[str, Any]
    config_hash: str
    connection_ref: str | None
    auth_mode: AuthMode
    connection_scopes: tuple[str, ...]
    policy_ref: str
    allowed_destination_constraints: tuple[str, ...]
    instance_fingerprint: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.binding_id,
                self.provider_id,
                self.descriptor_id,
                self.descriptor_version,
                self.operation_id,
                self.operation_version,
                self.instance_id,
                self.provider_resource_id,
                self.tenant_id,
                self.project_id,
                self.instance_discovered_version,
                self.config_hash,
                self.policy_ref,
                self.instance_fingerprint,
            )
        ):
            raise ValueError("Capability binding identifiers, versions, fingerprints, and references are required")
        for field_name, value in (
            ("input_schema_digest", self.input_schema_digest),
            ("output_schema_digest", self.output_schema_digest),
            ("descriptor_digest", self.descriptor_digest),
            ("operations_digest", self.operations_digest),
            ("config_hash", self.config_hash),
            ("instance_fingerprint", self.instance_fingerprint),
        ):
            _validate_sha256(value, path=f"binding.{field_name}")
        _validate_json_value(self.config, path="binding.config")
        _validate_binding_safe_configuration(self.config)
        object.__setattr__(self, "config", _freeze(self.config))
        if canonical_json_hash(self.config) != self.config_hash:
            raise ValueError("Capability binding config hash does not match canonical config")
        if any(not constraint for constraint in self.allowed_destination_constraints) or len(
            set(self.allowed_destination_constraints)
        ) != len(self.allowed_destination_constraints):
            raise ValueError("Capability binding destination constraints must be non-empty and unique")
        if any(not scope for scope in self.connection_scopes) or len(set(self.connection_scopes)) != len(
            self.connection_scopes
        ):
            raise ValueError("Capability binding connection scopes must be non-empty and unique")
        object.__setattr__(self, "connection_scopes", tuple(sorted(self.connection_scopes)))
        object.__setattr__(
            self,
            "allowed_destination_constraints",
            tuple(sorted(self.allowed_destination_constraints)),
        )

    @property
    def provider_version(self) -> str:
        return self.operation_version

    @property
    def instance_ref(self) -> str:
        return self.instance_id

    @property
    def pinned_provider_version(self) -> str:
        return self.operation_version

    @property
    def config_ref(self) -> str:
        return self.config_hash


def capability_binding(
    *,
    binding_id: str,
    instance: CapabilityInstance,
    descriptor: CapabilityDescriptor,
    operation: OperationDescriptor,
    policy_ref: str,
) -> CapabilityBinding:
    binding = CapabilityBinding(
        binding_id=binding_id,
        provider_id=instance.provider_id,
        descriptor_id=descriptor.descriptor_id,
        descriptor_version=descriptor.descriptor_version,
        descriptor_digest=descriptor.descriptor_digest,
        operations_digest=capability_operations_digest(descriptor),
        operation_id=operation.operation_id,
        operation_version=operation.version,
        instance_id=instance.instance_id,
        provider_resource_id=instance.provider_resource_id,
        tenant_id=instance.tenant_id,
        project_id=instance.project_id,
        instance_discovered_version=instance.discovered_provider_version,
        instance_discovered_resource_version=instance.discovered_resource_version,
        input_schema_digest=operation.input_schema_digest,
        output_schema_digest=operation.output_schema_digest,
        config=instance.configuration,
        config_hash=instance.config_fingerprint,
        connection_ref=instance.connection_ref,
        auth_mode=instance.auth_mode,
        connection_scopes=instance.connection_scopes,
        policy_ref=policy_ref,
        allowed_destination_constraints=instance.allowed_destination_constraints or operation.side_effect_destinations,
        instance_fingerprint=capability_instance_fingerprint(
            instance,
            descriptor,
            policy_ref=policy_ref,
        ),
    )
    validate_binding(instance, descriptor, binding)
    return binding


@runtime_checkable
class ToolHandler(Protocol):
    def __call__(
        self,
        arguments: Mapping[str, Any],
        context: InvocationContext,
    ) -> Awaitable[InvocationResult]: ...


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    registration_id: str
    binding: CapabilityBinding
    handler: ToolHandler = field(repr=False)

    def __post_init__(self) -> None:
        if not self.registration_id:
            raise ValueError("Tool registration identity is required")
        if not callable(self.handler):
            raise ValueError("Tool registration handler must be callable")


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    family: str
    name: str
    description: str
    auth_modes: tuple[AuthMode, ...]
    provenance: tuple[ProvenanceRecord, ...]
    capability_descriptors: tuple[CapabilityDescriptor, ...] = ()

    def __post_init__(self) -> None:
        if not self.provider_id or not self.name or not self.provenance:
            raise ValueError("Provider identity and provenance are required")


@dataclass(frozen=True, slots=True)
class AccessToken:
    token: str = field(repr=False)
    expires_on: int


@runtime_checkable
class TokenCredential(Protocol):
    def get_token(self, *scopes: str) -> AccessToken: ...


@runtime_checkable
class SecretCredential(Protocol):
    def get_secret(self, name: str) -> str: ...


@runtime_checkable
class SigningCredential(Protocol):
    def sign(self, payload: bytes, *, algorithm: str) -> str: ...


@runtime_checkable
class RequestSigningCredential(Protocol):
    def authorization(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        content_length: int,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    decision_id: str
    approved: bool
    tenant_id: str
    principal_id: str
    instance_id: str
    project_id: str
    provider_resource_id: str
    instance_fingerprint: str
    descriptor_id: str
    operation_id: str
    operation_version: str
    arguments_hash: str
    destination_hash: str
    expires_at: str
    policy_release: str
    binding_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.decision_id,
                self.tenant_id,
                self.principal_id,
                self.instance_id,
                self.project_id,
                self.provider_resource_id,
                self.descriptor_id,
                self.operation_id,
                self.operation_version,
                self.policy_release,
            )
        ):
            raise ValueError("Approval decision identity and policy bindings are required")
        for field_name, value in (
            ("instance_fingerprint", self.instance_fingerprint),
            ("arguments_hash", self.arguments_hash),
            ("destination_hash", self.destination_hash),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"Approval decision {field_name} must be lowercase SHA-256")
        _validate_utc_timestamp(self.expires_at, path="approval.expires_at")


@dataclass(frozen=True, slots=True)
class InvocationContext:
    tenant_id: str
    principal_id: str
    project_id: str
    credential: object | None = field(repr=False)
    transport: httpx.Client
    correlation_id: str
    trace_id: str
    sleep: Callable[[float], None]
    approval_decisions: tuple[ApprovalDecision, ...] = ()
    policy_release: str = "agent-studio-v1"
    deadline_at: str | None = None
    is_cancelled: Callable[[], bool] = field(default=lambda: False, repr=False)
    consume_approval: Callable[[str], bool] = field(default=lambda _: False, repr=False)

    def __post_init__(self) -> None:
        if not all(
            (
                self.tenant_id,
                self.principal_id,
                self.project_id,
                self.correlation_id,
                self.trace_id,
                self.policy_release,
            )
        ):
            raise ValueError(
                "Tenant, principal, project, correlation, trace, and policy release identifiers are required"
            )
        if self.deadline_at is not None:
            _validate_utc_timestamp(self.deadline_at, path="invocation.deadline_at")

    def raise_if_cancelled_or_expired(self, *, provider_id: str, instance_id: str | None = None) -> None:
        if self.is_cancelled():
            raise ProviderTimeoutError(
                "Invocation was cancelled",
                provider_id=provider_id,
                instance_id=instance_id,
            )
        if self.deadline_at is not None:
            deadline = datetime.fromisoformat(self.deadline_at.replace("Z", "+00:00"))
            if deadline <= datetime.now(UTC):
                raise ProviderTimeoutError(
                    "Invocation deadline expired",
                    provider_id=provider_id,
                    instance_id=instance_id,
                )

    def remaining_seconds(self, *, provider_id: str, instance_id: str | None = None) -> float | None:
        self.raise_if_cancelled_or_expired(provider_id=provider_id, instance_id=instance_id)
        if self.deadline_at is None:
            return None
        deadline = datetime.fromisoformat(self.deadline_at.replace("Z", "+00:00"))
        return max(0.0, (deadline - datetime.now(UTC)).total_seconds())


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    target: CapabilityInstance | CapabilityBinding = field(repr=False)
    operation_id: str
    arguments: Mapping[str, Any] = field(repr=False)
    idempotency_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target, CapabilityInstance | CapabilityBinding) or not self.operation_id:
            raise ValueError("Capability instance and operation identifiers are required")
        if isinstance(self.target, CapabilityBinding) and self.target.operation_id != self.operation_id:
            raise ValueError("Invocation operation must match the capability binding")
        if self.idempotency_key is not None and (
            not 1 <= len(self.idempotency_key) <= 256
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in self.idempotency_key)
        ):
            raise ValueError("Idempotency keys must contain 1-256 visible ASCII characters")
        _validate_json_value(self.arguments, path="invocation.arguments")
        object.__setattr__(self, "arguments", _freeze(self.arguments))


@dataclass(frozen=True, slots=True)
class InvocationResult:
    provider_id: str
    instance_id: str
    operation_id: str
    status_code: int
    output: Any = field(repr=False)
    audit_metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "output", _freeze(self.output))
        object.__setattr__(self, "audit_metadata", _freeze(self.audit_metadata))


@dataclass(frozen=True, slots=True)
class ValidationReport:
    readiness: Readiness
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HealthReport:
    readiness: Readiness
    evidence: tuple[str, ...]


class Provider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def discover(self, context: InvocationContext) -> DiscoveryResult: ...

    def validate(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> ValidationReport: ...

    def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport: ...

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult: ...


class ProviderError(Exception):
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        instance_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.instance_id = instance_id
        self.retry_after = retry_after


class UnauthorizedError(ProviderError):
    code = "unauthorized"


class NeedsConsentError(ProviderError):
    code = "needs_consent"


class UnavailableError(ProviderError):
    code = "unavailable"


class ProviderValidationError(ProviderError):
    code = "validation"


class ProviderTimeoutError(ProviderError):
    code = "timeout"


class RateLimitError(ProviderError):
    code = "rate_limit"


class UpstreamError(ProviderError):
    code = "upstream"


class PolicyError(ProviderError):
    code = "policy"


class BindingChangeCategory(StrEnum):
    PROVIDER = "provider"
    DESCRIPTOR = "descriptor"
    OPERATIONS = "operations"
    INSTANCE = "instance"
    BOUNDARY = "boundary"
    CONNECTION = "connection"
    DESTINATIONS = "destinations"
    POLICY = "policy"
    CONFIGURATION = "configuration"


class StaleBindingError(PolicyError):
    code = "stale_binding"

    def __init__(
        self,
        *,
        provider_id: str,
        instance_id: str,
        old_fingerprint: str,
        new_fingerprint: str,
        changed_categories: tuple[BindingChangeCategory, ...],
    ) -> None:
        _validate_sha256(old_fingerprint, path="stale_binding.old_fingerprint")
        _validate_sha256(new_fingerprint, path="stale_binding.new_fingerprint")
        if not changed_categories or len(set(changed_categories)) != len(changed_categories):
            raise ValueError("Stale binding change categories must be non-empty and unique")
        super().__init__(
            "Capability binding is stale and requires explicit rebind and review",
            provider_id=provider_id,
            instance_id=instance_id,
        )
        self.old_fingerprint = old_fingerprint
        self.new_fingerprint = new_fingerprint
        self.changed_categories = changed_categories


class BindabilityReason(StrEnum):
    MATURITY_NOT_GA = "maturity_not_ga"
    INSTANCE_NOT_READY = "instance_not_ready"
    HEALTH_NOT_READY = "health_not_ready"
    TENANT_MISMATCH = "tenant_mismatch"
    PROJECT_MISMATCH = "project_mismatch"
    CONNECTION_NOT_AUTHORIZED = "connection_not_authorized"
    CONFIGURATION_NOT_VALIDATED = "configuration_not_validated"
    POLICY_UNSATISFIABLE = "policy_unsatisfiable"


@dataclass(frozen=True, slots=True)
class BindabilityDecision:
    operation_id: str
    bindable: bool
    reason_codes: tuple[BindabilityReason, ...]

    def __post_init__(self) -> None:
        if not self.operation_id or self.bindable == bool(self.reason_codes):
            raise ValueError("Bindability decisions require an operation and consistent reasons")


def validate_json(schema: JsonSchema, value: Any, *, path: str = "$") -> None:
    """Validate the deterministic JSON Schema subset used by provider operations."""

    expected = schema.get("type")
    type_map: dict[str, type[Any] | tuple[type[Any], ...]] = {
        "object": Mapping,
        "array": Sequence,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if isinstance(expected, str) and expected in type_map:
        expected_type = type_map[expected]
        valid = isinstance(value, expected_type)
        if expected == "array":
            valid = valid and not isinstance(value, str | bytes)
        if expected in {"integer", "number"}:
            valid = valid and not isinstance(value, bool)
        if not valid:
            raise ValueError(f"{path} must be {expected}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of the declared values")
    if isinstance(value, Mapping):
        required = schema.get("required", ())
        for key in required:
            if key not in value:
                raise ValueError(f"{path}.{key} is required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise ValueError(f"{path} contains unsupported properties: {', '.join(sorted(extra))}")
        for key, child in properties.items():
            if key in value and isinstance(child, Mapping):
                validate_json(child, value[key], path=f"{path}.{key}")
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                validate_json(items, item, path=f"{path}[{index}]")


def bindability_decisions(
    discovery: DiscoveryResult,
    instance: CapabilityInstance,
    *,
    tenant_id: str,
    project_id: str | None,
    policy_ref: str | None,
) -> tuple[BindabilityDecision, ...]:
    descriptor = discovery.descriptor_for(instance)
    decisions: list[BindabilityDecision] = []
    for operation in descriptor.operations:
        reasons: list[BindabilityReason] = []
        if operation.maturity is not Maturity.GA:
            reasons.append(BindabilityReason.MATURITY_NOT_GA)
        if instance.readiness is not Readiness.READY:
            reasons.append(BindabilityReason.INSTANCE_NOT_READY)
        if instance.health is not Readiness.READY:
            reasons.append(BindabilityReason.HEALTH_NOT_READY)
        if instance.tenant_id != tenant_id:
            reasons.append(BindabilityReason.TENANT_MISMATCH)
        if instance.project_id != project_id:
            reasons.append(BindabilityReason.PROJECT_MISMATCH)
        if (
            instance.readiness in {Readiness.UNAUTHORIZED, Readiness.NEEDS_CONSENT}
            or (instance.auth_mode is not AuthMode.NONE and not instance.connection_ref)
            or instance.auth_mode not in descriptor.auth_modes
        ):
            reasons.append(BindabilityReason.CONNECTION_NOT_AUTHORIZED)
        if not instance.config_validated:
            reasons.append(BindabilityReason.CONFIGURATION_NOT_VALIDATED)
        if not policy_ref:
            reasons.append(BindabilityReason.POLICY_UNSATISFIABLE)
        decisions.append(
            BindabilityDecision(
                operation_id=operation.operation_id,
                bindable=not reasons,
                reason_codes=tuple(reasons),
            )
        )
    return tuple(decisions)


def validate_binding(
    instance: CapabilityInstance,
    descriptor: CapabilityDescriptor,
    binding: CapabilityBinding,
    *,
    policy_ref: str | None = None,
) -> OperationDescriptor:
    active_policy_ref = policy_ref or binding.policy_ref
    operation = next(
        (
            item
            for item in descriptor.operations
            if item.operation_id == binding.operation_id and item.version == binding.operation_version
        ),
        None,
    )
    current_fingerprint = capability_instance_fingerprint(
        instance,
        descriptor,
        policy_ref=active_policy_ref,
    )
    if binding.instance_fingerprint != current_fingerprint:
        categories: list[BindingChangeCategory] = []
        if binding.provider_id != instance.provider_id:
            categories.append(BindingChangeCategory.PROVIDER)
        descriptor_changed = (
            binding.descriptor_id != instance.descriptor_id
            or binding.descriptor_version != instance.descriptor_version
            or binding.descriptor_digest != instance.descriptor_digest
            or descriptor.descriptor_digest != instance.descriptor_digest
        )
        if descriptor_changed:
            categories.append(BindingChangeCategory.DESCRIPTOR)
        if (
            binding.operations_digest != capability_operations_digest(descriptor)
            or operation is None
            or binding.input_schema_digest != operation.input_schema_digest
            or binding.output_schema_digest != operation.output_schema_digest
        ):
            categories.append(BindingChangeCategory.OPERATIONS)
        if (
            binding.instance_id != instance.instance_id
            or binding.provider_resource_id != instance.provider_resource_id
            or binding.instance_discovered_version != instance.discovered_provider_version
            or binding.instance_discovered_resource_version != instance.discovered_resource_version
        ):
            categories.append(BindingChangeCategory.INSTANCE)
        if binding.tenant_id != instance.tenant_id or binding.project_id != instance.project_id:
            categories.append(BindingChangeCategory.BOUNDARY)
        if (
            binding.connection_ref != instance.connection_ref
            or binding.auth_mode is not instance.auth_mode
            or set(binding.connection_scopes) != set(instance.connection_scopes)
        ):
            categories.append(BindingChangeCategory.CONNECTION)
        effective_destinations = instance.allowed_destination_constraints or (
            operation.side_effect_destinations if operation is not None else ()
        )
        if set(binding.allowed_destination_constraints) != set(effective_destinations):
            categories.append(BindingChangeCategory.DESTINATIONS)
        if binding.policy_ref != active_policy_ref:
            categories.append(BindingChangeCategory.POLICY)
        if binding.config_hash != instance.config_fingerprint or binding.config != instance.configuration:
            categories.append(BindingChangeCategory.CONFIGURATION)
        if not categories:
            categories.append(BindingChangeCategory.DESCRIPTOR)
        raise StaleBindingError(
            provider_id=instance.provider_id,
            instance_id=instance.instance_id,
            old_fingerprint=binding.instance_fingerprint,
            new_fingerprint=current_fingerprint,
            changed_categories=tuple(categories),
        )
    if instance.readiness is not Readiness.READY:
        raise ValueError("Capability binding requires a ready instance")
    if instance.health is not Readiness.READY:
        raise ValueError("Capability binding requires a healthy instance")
    if not instance.config_validated:
        raise ValueError("Capability binding requires validated configuration")
    if instance.auth_mode not in descriptor.auth_modes:
        raise ValueError("Capability binding authentication mode is not supported by the descriptor")
    if binding.provider_id != instance.provider_id:
        raise ValueError("Capability binding belongs to a different provider")
    if binding.instance_id != instance.instance_id:
        raise ValueError("Capability binding belongs to a different instance")
    if (
        binding.descriptor_id != instance.descriptor_id
        or binding.descriptor_version != instance.descriptor_version
        or binding.descriptor_digest != instance.descriptor_digest
    ):
        raise ValueError("Capability binding belongs to a different descriptor")
    if binding.operations_digest != capability_operations_digest(descriptor):
        raise ValueError("Capability binding operation set digest does not match")
    if (
        binding.provider_resource_id != instance.provider_resource_id
        or binding.instance_discovered_version != instance.discovered_provider_version
        or binding.instance_discovered_resource_version != instance.discovered_resource_version
    ):
        raise ValueError("Capability binding resource identity or discovered instance version does not match")
    if binding.tenant_id != instance.tenant_id or binding.project_id != instance.project_id:
        raise ValueError("Capability binding tenant or project boundary does not match")
    if (
        binding.connection_ref != instance.connection_ref
        or binding.auth_mode is not instance.auth_mode
        or set(binding.connection_scopes) != set(instance.connection_scopes)
    ):
        raise ValueError("Capability binding connection reference or authorization scope does not match")
    if binding.config_hash != instance.config_fingerprint or binding.config != instance.configuration:
        raise ValueError("Capability binding configuration reference does not match")
    if binding.policy_ref != active_policy_ref:
        raise ValueError("Capability binding policy reference does not match")
    if operation is None:
        raise ValueError("Capability binding operation or version is not declared")
    if binding.input_schema_digest != operation.input_schema_digest:
        raise ValueError("Capability binding input schema digest does not match")
    if binding.output_schema_digest != operation.output_schema_digest:
        raise ValueError("Capability binding output schema digest does not match")
    if operation.maturity is not Maturity.GA:
        raise ValueError("Only GA operations can be bound")
    allowed = set(operation.side_effect_destinations)
    if allowed and not binding.allowed_destination_constraints:
        raise ValueError("Binding destination constraints cannot be empty for a destination-bound operation")
    if not set(binding.allowed_destination_constraints).issubset(allowed):
        raise ValueError("Binding destination constraints must narrow descriptor constraints")
    return operation


def resolve_capability_target(
    discovery: DiscoveryResult,
    target: CapabilityInstance | CapabilityBinding,
    *,
    provider_id: str,
    policy_ref: str | None = None,
) -> tuple[CapabilityInstance, CapabilityBinding | None]:
    current = next(
        (instance for instance in discovery.instances if instance.instance_id == target.instance_id),
        None,
    )
    if current is None:
        raise UnavailableError(
            "Capability instance is not present in current provider discovery",
            provider_id=provider_id,
            instance_id=target.instance_id,
        )
    if isinstance(target, CapabilityBinding):
        try:
            validate_binding(
                current,
                discovery.descriptor_for(current),
                target,
                policy_ref=policy_ref,
            )
        except StaleBindingError:
            raise
        except ValueError as exc:
            raise PolicyError(
                str(exc),
                provider_id=provider_id,
                instance_id=current.instance_id,
            ) from exc
        return current, target
    descriptor = discovery.descriptor_for(current)
    active_policy_ref = policy_ref or "unbound"
    current_fingerprint = capability_instance_fingerprint(
        current,
        descriptor,
        policy_ref=active_policy_ref,
    )
    if (
        target.descriptor_id != descriptor.descriptor_id
        or target.descriptor_version != descriptor.descriptor_version
        or target.descriptor_digest != descriptor.descriptor_digest
    ):
        old_snapshot_fingerprint = canonical_json_hash(
            {
                "provider_id": target.provider_id,
                "instance_id": target.instance_id,
                "descriptor_id": target.descriptor_id,
                "descriptor_version": target.descriptor_version,
                "descriptor_digest": target.descriptor_digest,
                "provider_resource_id": target.provider_resource_id,
                "discovered_provider_version": target.discovered_provider_version,
                "discovered_resource_version": target.discovered_resource_version,
                "tenant_id": target.tenant_id,
                "project_id": target.project_id,
                "connection_ref": target.connection_ref,
                "auth_mode": target.auth_mode.value,
                "connection_scopes": sorted(target.connection_scopes),
                "allowed_destination_constraints": sorted(target.allowed_destination_constraints),
                "policy_ref": active_policy_ref,
                "configuration": plain_json(target.configuration),
            }
        )
        raise StaleBindingError(
            provider_id=provider_id,
            instance_id=current.instance_id,
            old_fingerprint=old_snapshot_fingerprint,
            new_fingerprint=current_fingerprint,
            changed_categories=(BindingChangeCategory.DESCRIPTOR,),
        )
    target_fingerprint = capability_instance_fingerprint(
        target,
        descriptor,
        policy_ref=active_policy_ref,
    )
    if current_fingerprint != target_fingerprint:
        categories: list[BindingChangeCategory] = []
        if current.provider_id != target.provider_id:
            categories.append(BindingChangeCategory.PROVIDER)
        if (
            current.provider_resource_id != target.provider_resource_id
            or current.discovered_provider_version != target.discovered_provider_version
            or current.discovered_resource_version != target.discovered_resource_version
        ):
            categories.append(BindingChangeCategory.INSTANCE)
        if current.tenant_id != target.tenant_id or current.project_id != target.project_id:
            categories.append(BindingChangeCategory.BOUNDARY)
        if (
            current.connection_ref != target.connection_ref
            or current.auth_mode is not target.auth_mode
            or set(current.connection_scopes) != set(target.connection_scopes)
        ):
            categories.append(BindingChangeCategory.CONNECTION)
        if set(current.allowed_destination_constraints) != set(target.allowed_destination_constraints):
            categories.append(BindingChangeCategory.DESTINATIONS)
        if current.configuration != target.configuration:
            categories.append(BindingChangeCategory.CONFIGURATION)
        raise StaleBindingError(
            provider_id=provider_id,
            instance_id=current.instance_id,
            old_fingerprint=target_fingerprint,
            new_fingerprint=current_fingerprint,
            changed_categories=tuple(categories or (BindingChangeCategory.DESCRIPTOR,)),
        )
    return current, None


def validation_for_target(
    discovery: DiscoveryResult,
    target: CapabilityInstance | CapabilityBinding,
    *,
    provider_id: str,
    policy_ref: str | None = None,
) -> ValidationReport:
    instance, _ = resolve_capability_target(
        discovery,
        target,
        provider_id=provider_id,
        policy_ref=policy_ref,
    )
    return ValidationReport(
        instance.readiness,
        () if instance.readiness is Readiness.READY else (instance.unavailable_reason or "Not ready",),
    )


def health_for_target(
    discovery: DiscoveryResult,
    target: CapabilityInstance | CapabilityBinding,
    *,
    provider_id: str,
    policy_ref: str | None = None,
) -> HealthReport:
    instance, _ = resolve_capability_target(
        discovery,
        target,
        provider_id=provider_id,
        policy_ref=policy_ref,
    )
    return HealthReport(instance.health or instance.readiness, instance.status_evidence)


def approval_decision(
    context: InvocationContext,
    *,
    target: CapabilityInstance | CapabilityBinding,
    instance: CapabilityInstance,
    descriptor: CapabilityDescriptor,
    operation: OperationDescriptor,
    arguments: Mapping[str, Any],
    decision_id: str,
    expires_at: str,
    approved: bool = True,
) -> ApprovalDecision:
    if context.tenant_id != instance.tenant_id or context.project_id != instance.project_id:
        raise PolicyError(
            "Approval scope is outside the capability tenant or project boundary",
            provider_id=instance.provider_id,
            instance_id=instance.instance_id,
        )
    destinations = resolve_exact_destinations(
        instance,
        operation,
        arguments,
        destination_constraints=(
            target.allowed_destination_constraints if isinstance(target, CapabilityBinding) else None
        ),
    )
    return ApprovalDecision(
        decision_id=decision_id,
        approved=approved,
        tenant_id=context.tenant_id,
        principal_id=context.principal_id,
        instance_id=instance.instance_id,
        project_id=instance.project_id,
        provider_resource_id=instance.provider_resource_id,
        instance_fingerprint=capability_instance_fingerprint(
            instance,
            descriptor,
            policy_ref=context.policy_release,
        ),
        descriptor_id=instance.descriptor_id,
        operation_id=operation.operation_id,
        operation_version=operation.version,
        arguments_hash=canonical_json_hash(arguments),
        destination_hash=canonical_json_hash(destinations),
        expires_at=expires_at,
        policy_release=context.policy_release,
        binding_id=target.binding_id if isinstance(target, CapabilityBinding) else None,
    )


def operation_allows_retry(
    operation: OperationDescriptor,
    *,
    idempotency_key: str | None,
    stream_started: bool = False,
) -> bool:
    if stream_started or operation.operation_class is OperationClass.WRITE_IRREVERSIBLE:
        return False
    if operation.operation_class in {
        OperationClass.WRITE_REVERSIBLE,
        OperationClass.PRIVILEGED,
    }:
        return operation.idempotency is not Idempotency.NONE and idempotency_key is not None
    return operation.max_retries > 0


def resolve_exact_destinations(
    instance: CapabilityInstance,
    operation: OperationDescriptor,
    arguments: Mapping[str, Any],
    *,
    destination_constraints: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    constraints = (
        destination_constraints
        if destination_constraints is not None
        else instance.allowed_destination_constraints or operation.side_effect_destinations
    )
    if not constraints:
        return ()
    arguments_hash = canonical_json_hash(arguments)
    return tuple(f"{constraint}#arguments-sha256={arguments_hash}" for constraint in constraints)


def _approval_matches(
    decision: ApprovalDecision,
    *,
    context: InvocationContext,
    instance: CapabilityInstance,
    descriptor: CapabilityDescriptor,
    operation: OperationDescriptor,
    request: InvocationRequest,
    binding: CapabilityBinding | None,
    resolved_destinations: tuple[str, ...],
) -> bool:
    expires_at = datetime.fromisoformat(decision.expires_at.replace("Z", "+00:00"))
    return (
        decision.approved
        and decision.tenant_id == context.tenant_id
        and decision.principal_id == context.principal_id
        and decision.instance_id == instance.instance_id
        and decision.project_id == instance.project_id
        and decision.provider_resource_id == instance.provider_resource_id
        and decision.instance_fingerprint
        == capability_instance_fingerprint(
            instance,
            descriptor,
            policy_ref=context.policy_release,
        )
        and decision.descriptor_id == instance.descriptor_id
        and decision.operation_id == operation.operation_id
        and decision.operation_version == operation.version
        and decision.arguments_hash == canonical_json_hash(request.arguments)
        and decision.destination_hash == canonical_json_hash(resolved_destinations)
        and decision.policy_release == context.policy_release
        and decision.binding_id == (binding.binding_id if binding else None)
        and expires_at > datetime.now(UTC)
    )


def find_operation(
    discovery: DiscoveryResult,
    request: InvocationRequest,
    context: InvocationContext,
    *,
    provider_id: str,
    tenant_id: str | None,
) -> tuple[CapabilityInstance, OperationDescriptor]:
    if tenant_id is not None and context.tenant_id != tenant_id:
        raise PolicyError("Invocation tenant is outside the configured boundary", provider_id=provider_id)
    instance, binding = resolve_capability_target(
        discovery,
        request.target,
        provider_id=provider_id,
        policy_ref=context.policy_release,
    )
    if context.tenant_id != instance.tenant_id:
        raise PolicyError(
            "Invocation tenant is outside the capability boundary",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    if context.project_id != instance.project_id:
        raise PolicyError(
            "Invocation project is outside the capability boundary",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    if instance.readiness is not Readiness.READY:
        raise UnavailableError(
            instance.unavailable_reason or "Capability instance is not ready",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    if instance.health is not Readiness.READY:
        raise UnavailableError(
            "Capability instance health is not ready",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    if not instance.config_validated:
        raise PolicyError(
            "Capability instance configuration is not validated",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    descriptor = discovery.descriptor_for(instance)
    if instance.auth_mode not in descriptor.auth_modes:
        raise PolicyError(
            "Capability authentication mode is not supported by the descriptor",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    operation = next(
        (
            item
            for item in descriptor.operations
            if item.operation_id == request.operation_id
            and (binding is None or item.version == binding.operation_version)
        ),
        None,
    )
    if operation is None:
        raise ProviderValidationError(
            "Operation is not declared by the capability",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    if operation.maturity is not Maturity.GA:
        raise UnavailableError(
            "Only GA operations can be invoked",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    try:
        validate_json(operation.input_schema, request.arguments)
    except ValueError as exc:
        raise ProviderValidationError(
            str(exc),
            provider_id=provider_id,
            instance_id=instance.instance_id,
        ) from exc
    context.raise_if_cancelled_or_expired(provider_id=provider_id, instance_id=instance.instance_id)
    resolved_destinations = resolve_exact_destinations(
        instance,
        operation,
        request.arguments,
        destination_constraints=(binding.allowed_destination_constraints if binding is not None else None),
    )
    if operation.idempotency is Idempotency.CALLER_KEY and not request.idempotency_key:
        raise PolicyError(
            "An idempotency key is required",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    if operation.approval_policy in {ApprovalPolicy.REQUIRED, ApprovalPolicy.POLICY_EVALUATED}:
        decision = next(
            (
                candidate
                for candidate in context.approval_decisions
                if _approval_matches(
                    candidate,
                    context=context,
                    instance=instance,
                    descriptor=descriptor,
                    operation=operation,
                    request=request,
                    binding=binding,
                    resolved_destinations=resolved_destinations,
                )
            ),
            None,
        )
        if decision is None:
            raise PolicyError(
                "A current approval decision bound to this invocation is required",
                provider_id=provider_id,
                instance_id=instance.instance_id,
            )
        if not context.consume_approval(decision.decision_id):
            raise PolicyError(
                "Approval decision is unavailable or has already been consumed",
                provider_id=provider_id,
                instance_id=instance.instance_id,
            )
    return instance, operation


def audit_metadata(
    context: InvocationContext,
    *,
    provider_id: str,
    instance_id: str,
    operation_id: str,
    attempts: int,
    response: httpx.Response,
) -> Mapping[str, Any]:
    return {
        "provider_id": provider_id,
        "instance_id": instance_id,
        "operation_id": operation_id,
        "tenant_id": context.tenant_id,
        "principal_id": context.principal_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "attempts": attempts,
        "status_code": response.status_code,
        "latency_ms": response.extensions.get("provider_elapsed_ms"),
    }
