"""Immutable contracts for operational capability providers."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

import httpx
import rfc8785

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonSchema = Mapping[str, Any]
PROVIDER_CONTRACT_VERSION = "research-assistant.integration-provider.v7"
CANONICALIZATION_VERSION = "research-assistant.canonical-json.v1"
POLICY_REFERENCE_VERSION = "1.0.0"
_MAX_SAFE_INTEGER = (1 << 53) - 1


class Maturity(StrEnum):
    UNKNOWN = "unknown"
    GA = "ga"
    PREVIEW = "preview"


class Lifecycle(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
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


class ApprovalDecisionStatus(StrEnum):
    APPROVED = "approved"
    DENIED = "denied"
    REVOKED = "revoked"


class ApprovalUsePolicy(StrEnum):
    ONE_TIME = "one_time"
    BOUNDED_REUSABLE = "bounded_reusable"


class ApprovalConsumptionStatus(StrEnum):
    CONSUMED = "consumed"
    ALREADY_CONSUMED_BY_SAME_IDEMPOTENT_INVOCATION = (
        "already_consumed_by_same_idempotent_invocation"
    )
    EXPIRED = "expired"
    REVOKED = "revoked"
    DENIED = "denied"
    MISMATCH = "mismatch"
    UNAVAILABLE = "unavailable"


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
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int):
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise ValueError(f"{path} integers must be within the RFC 8785 portable range")
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


def canonical_json_bytes(value: Any) -> bytes:
    """Encode finite JSON with the RFC 8785 profile named by the provider contract."""
    _validate_json_value(value, path="hash_input")
    return rfc8785.dumps(plain_json(value))


def canonical_json_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


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
        "source_operation_id",
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
    operation_version: str
    maturity: Maturity
    input_schema: JsonSchema
    output_schema: JsonSchema
    operation_class: OperationClass
    approval_policy: ApprovalPolicy
    lifecycle: Lifecycle = Lifecycle.ACTIVE
    external_side_effect: bool = False
    side_effect_destinations: tuple[str, ...] = ()
    timeout_seconds: float = 20.0
    max_retries: int = 0
    idempotency: Idempotency = Idempotency.NONE
    least_privilege_scopes: tuple[str, ...] = ()
    least_privilege_roles: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()
    audit_events: tuple[str, ...] = ("provider.invoke",)
    policy_exception_ref: PolicyRef | None = None

    def __post_init__(self) -> None:
        if (
            not self.operation_id
            or not self.operation_version
            or self.timeout_seconds <= 0
            or not 0 <= self.max_retries <= 5
        ):
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
                    key=lambda item: (item.operation_id, item.operation_version),
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
        "operation_version": operation.operation_version,
        "maturity": operation.maturity.value,
        "lifecycle": operation.lifecycle.value,
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
        "policy_exception_ref": (
            {
                "policy_id": operation.policy_exception_ref.policy_id,
                "policy_version": operation.policy_exception_ref.policy_version,
                "policy_digest": operation.policy_exception_ref.policy_digest,
            }
            if operation.policy_exception_ref is not None
            else None
        ),
    }


def capability_operations_digest(descriptor: CapabilityDescriptor) -> str:
    return canonical_json_hash(
        [
            _operation_governance_payload(operation)
            for operation in sorted(
                descriptor.operations,
                key=lambda item: (item.operation_id, item.operation_version),
            )
        ]
    )


@dataclass(frozen=True, slots=True)
class DescriptorRef:
    descriptor_id: str
    descriptor_version: str
    descriptor_digest: str

    def __post_init__(self) -> None:
        if not self.descriptor_id or not self.descriptor_version:
            raise ValueError("Descriptor reference identity and version are required")
        _validate_sha256(self.descriptor_digest, path="descriptor_ref.descriptor_digest")


@dataclass(frozen=True, slots=True)
class OperationRef:
    operation_id: str
    operation_version: str
    input_schema_digest: str
    output_schema_digest: str

    def __post_init__(self) -> None:
        if not self.operation_id or not self.operation_version:
            raise ValueError("Operation reference identity and version are required")
        _validate_sha256(self.input_schema_digest, path="operation_ref.input_schema_digest")
        _validate_sha256(self.output_schema_digest, path="operation_ref.output_schema_digest")


@dataclass(frozen=True, slots=True)
class InstanceRef:
    provider_id: str
    instance_id: str
    discovered_version: str
    instance_fingerprint: str

    def __post_init__(self) -> None:
        if not self.provider_id or not self.instance_id or not self.discovered_version:
            raise ValueError("Instance reference provider, identity, and discovered version are required")
        _validate_sha256(self.instance_fingerprint, path="instance_ref.instance_fingerprint")


@dataclass(frozen=True, slots=True)
class ConfigurationRef:
    configuration_digest: str
    configuration_id: str | None = None

    def __post_init__(self) -> None:
        if self.configuration_id == "":
            raise ValueError("Configuration reference ID cannot be empty")
        _validate_sha256(self.configuration_digest, path="configuration_ref.configuration_digest")


@dataclass(frozen=True, slots=True)
class ConnectionRef:
    connection_id: str
    auth_mode: AuthMode
    authorization_digest: str

    def __post_init__(self) -> None:
        if not self.connection_id:
            raise ValueError("Connection reference ID is required")
        _validate_sha256(self.authorization_digest, path="connection_ref.authorization_digest")


@dataclass(frozen=True, slots=True)
class PolicyRef:
    policy_id: str
    policy_version: str
    policy_digest: str

    def __post_init__(self) -> None:
        if not self.policy_id or not self.policy_version:
            raise ValueError("Policy reference identity and version are required")
        _validate_sha256(self.policy_digest, path="policy_ref.policy_digest")


@dataclass(frozen=True, slots=True)
class AllowedDestinationsRef:
    constraints: tuple[str, ...]
    constraints_digest: str

    def __post_init__(self) -> None:
        if any(not constraint for constraint in self.constraints) or len(set(self.constraints)) != len(
            self.constraints
        ):
            raise ValueError("Allowed destination constraints must be non-empty and unique")
        object.__setattr__(self, "constraints", tuple(sorted(self.constraints)))
        _validate_sha256(self.constraints_digest, path="allowed_destinations_ref.constraints_digest")
        if canonical_json_hash(list(self.constraints)) != self.constraints_digest:
            raise ValueError("Allowed destination constraints digest does not match canonical constraints")


def descriptor_ref(descriptor: CapabilityDescriptor) -> DescriptorRef:
    return DescriptorRef(
        descriptor_id=descriptor.descriptor_id,
        descriptor_version=descriptor.descriptor_version,
        descriptor_digest=descriptor.descriptor_digest,
    )


def operation_ref(operation: OperationDescriptor) -> OperationRef:
    return OperationRef(
        operation_id=operation.operation_id,
        operation_version=operation.operation_version,
        input_schema_digest=operation.input_schema_digest,
        output_schema_digest=operation.output_schema_digest,
    )


def configuration_ref(instance: CapabilityInstance) -> ConfigurationRef:
    return ConfigurationRef(
        configuration_digest=instance.config_fingerprint,
        configuration_id=instance.configuration_id,
    )


def authorization_digest(instance: CapabilityInstance) -> str:
    return canonical_json_hash(
        {
            "connection_id": instance.connection_ref or "none",
            "connection_version": instance.connection_version,
            "auth_mode": instance.auth_mode.value,
            "identity_mode": instance.connection_identity_mode,
            "authorized_scopes": sorted(instance.connection_scopes),
            "authorized_roles": sorted(instance.connection_roles),
        }
    )


def connection_ref(instance: CapabilityInstance) -> ConnectionRef:
    return ConnectionRef(
        connection_id=instance.connection_ref or "none",
        auth_mode=instance.auth_mode,
        authorization_digest=authorization_digest(instance),
    )


def policy_reference(policy_id: str, *, policy_version: str = POLICY_REFERENCE_VERSION) -> PolicyRef:
    return PolicyRef(
        policy_id=policy_id,
        policy_version=policy_version,
        policy_digest=canonical_json_hash(
            {
                "policy_id": policy_id,
                "policy_version": policy_version,
            }
        ),
    )


def allowed_destinations_ref(constraints: Sequence[str]) -> AllowedDestinationsRef:
    canonical_constraints = tuple(sorted(constraints))
    return AllowedDestinationsRef(
        constraints=canonical_constraints,
        constraints_digest=canonical_json_hash(list(canonical_constraints)),
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
    connection_version: str
    auth_mode: AuthMode
    connection_identity_mode: str
    health: Readiness
    last_checked_at: str
    configuration: Mapping[str, Any] = field(default_factory=dict)
    config_fingerprint: str = ""
    configuration_id: str | None = None
    config_validated: bool = True
    connection_scopes: tuple[str, ...] = ()
    connection_roles: tuple[str, ...] = ()
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
                self.connection_version,
                self.connection_identity_mode,
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
        if self.configuration_id == "":
            raise ValueError("Capability instance configuration identity cannot be empty")
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
        if any(not role for role in self.connection_roles) or len(set(self.connection_roles)) != len(
            self.connection_roles
        ):
            raise ValueError("Instance connection roles must be non-empty and unique")
        object.__setattr__(self, "connection_scopes", tuple(sorted(self.connection_scopes)))
        object.__setattr__(self, "connection_roles", tuple(sorted(self.connection_roles)))
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
class DiscoveryWarning:
    reason_code: str
    message: str
    provider_id: str
    instance_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.reason_code
            or not self.provider_id
            or not self.message
            or len(self.message) > 512
            or any(character in self.message for character in "\r\n")
        ):
            raise ValueError("Discovery warnings require a code, provider, and sanitized single-line message")


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    descriptors: tuple[CapabilityDescriptor, ...]
    instances: tuple[CapabilityInstance, ...]
    warnings: tuple[DiscoveryWarning, ...]
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
    warnings: tuple[DiscoveryWarning, ...] = (),
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
    connection_version: str = "1",
    connection_identity_mode: str | None = None,
    connection_roles: tuple[str, ...] = (),
    descriptor_id: str | None = None,
    descriptor_metadata: Mapping[str, Any] | None = None,
    resource_id: str | None = None,
    connection_id: str | None = None,
    configuration_id: str | None = None,
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
    operation_versions = {operation.operation_version for operation in descriptor.operations}
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
        connection_version=connection_version,
        auth_mode=selected_auth_mode,
        connection_identity_mode=connection_identity_mode or selected_auth_mode.value,
        health=health or readiness,
        last_checked_at=last_checked_at or utc_now(),
        configuration=configuration or {},
        configuration_id=configuration_id,
        connection_scopes=connection_scopes,
        connection_roles=connection_roles,
        allowed_destination_constraints=allowed_destination_constraints,
        status_evidence=status_evidence,
        unavailable_reason=unavailable_reason,
    )
    return CapabilityRecord(descriptor=descriptor, instance=instance)


def capability_instance_fingerprint(
    instance: CapabilityInstance,
    descriptor: CapabilityDescriptor,
    *,
    policy_ref: str | PolicyRef,
) -> str:
    if (
        instance.descriptor_id != descriptor.descriptor_id
        or instance.descriptor_version != descriptor.descriptor_version
        or instance.descriptor_digest != descriptor.descriptor_digest
    ):
        raise ValueError("Capability fingerprint descriptor reference does not match the instance")
    resolved_policy_ref = (
        policy_ref if isinstance(policy_ref, PolicyRef) else policy_reference(policy_ref)
    )
    payload = {
        "canonicalization_version": CANONICALIZATION_VERSION,
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
                key=lambda item: (item.operation_id, item.operation_version),
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
            "connection_id": instance.connection_ref,
            "connection_version": instance.connection_version,
            "auth_mode": instance.auth_mode.value,
            "identity_mode": instance.connection_identity_mode,
            "connection_scopes": sorted(instance.connection_scopes),
            "connection_roles": sorted(instance.connection_roles),
            "authorization_digest": authorization_digest(instance),
        },
        "allowed_destination_constraints": sorted(instance.allowed_destination_constraints),
        "policy_ref": {
            "policy_id": resolved_policy_ref.policy_id,
            "policy_version": resolved_policy_ref.policy_version,
            "policy_digest": resolved_policy_ref.policy_digest,
        },
        "configuration": plain_json(instance.configuration),
        "configuration_id": instance.configuration_id,
    }
    return canonical_json_hash(payload)


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    binding_id: str
    logical_agent_id: str
    provider_contract_version: str
    canonicalization_version: str
    descriptor_ref: DescriptorRef
    operation_ref: OperationRef
    instance_ref: InstanceRef
    configuration_ref: ConfigurationRef
    connection_ref: ConnectionRef
    policy_ref: PolicyRef
    operations_digest: str
    provider_resource_id: str
    discovered_resource_version: str | None
    configuration: Mapping[str, Any]
    connection_version: str
    connection_identity_mode: str
    connection_scopes: tuple[str, ...]
    connection_roles: tuple[str, ...]
    allowed_destination_constraints: tuple[str, ...]
    allowed_destinations_digest: str
    tenant_scope: str
    project_scope: str

    def __post_init__(self) -> None:
        if not all(
            (
                self.binding_id,
                self.logical_agent_id,
                self.provider_resource_id,
                self.connection_version,
                self.connection_identity_mode,
                self.tenant_scope,
                self.project_scope,
            )
        ):
            raise ValueError("Capability binding identity and tenant/project scopes are required")
        if self.provider_contract_version != PROVIDER_CONTRACT_VERSION:
            raise ValueError("Capability binding provider contract version is unsupported")
        if self.canonicalization_version != CANONICALIZATION_VERSION:
            raise ValueError("Capability binding canonicalization version is unsupported")
        _validate_sha256(self.operations_digest, path="binding.operations_digest")
        _validate_json_value(self.configuration, path="binding.configuration")
        _validate_binding_safe_configuration(self.configuration)
        object.__setattr__(self, "configuration", _freeze(self.configuration))
        if canonical_json_hash(self.configuration) != self.configuration_ref.configuration_digest:
            raise ValueError("Capability binding config hash does not match canonical config")
        if any(not scope for scope in self.connection_scopes) or len(
            set(self.connection_scopes)
        ) != len(self.connection_scopes):
            raise ValueError("Capability binding connection scopes must be non-empty and unique")
        if any(not role for role in self.connection_roles) or len(
            set(self.connection_roles)
        ) != len(self.connection_roles):
            raise ValueError("Capability binding connection roles must be non-empty and unique")
        object.__setattr__(self, "connection_scopes", tuple(sorted(self.connection_scopes)))
        object.__setattr__(self, "connection_roles", tuple(sorted(self.connection_roles)))
        if any(not constraint for constraint in self.allowed_destination_constraints) or len(
            set(self.allowed_destination_constraints)
        ) != len(self.allowed_destination_constraints):
            raise ValueError("Capability binding destination constraints must be non-empty and unique")
        object.__setattr__(
            self,
            "allowed_destination_constraints",
            tuple(sorted(self.allowed_destination_constraints)),
        )
        _validate_sha256(
            self.allowed_destinations_digest,
            path="binding.allowed_destinations_digest",
        )
        if (
            canonical_json_hash(list(self.allowed_destination_constraints))
            != self.allowed_destinations_digest
        ):
            raise ValueError("Capability binding allowed destinations digest does not match constraints")


def capability_binding_payload(binding: CapabilityBinding) -> dict[str, Any]:
    has_connection = binding.connection_ref.connection_id != "none"
    return {
        "binding_id": binding.binding_id,
        "provider_contract_version": binding.provider_contract_version,
        "canonicalization_version": binding.canonicalization_version,
        "provider_id": binding.instance_ref.provider_id,
        "tenant_id": binding.tenant_scope,
        "project_id": binding.project_scope,
        "descriptor_id": binding.descriptor_ref.descriptor_id,
        "descriptor_version": binding.descriptor_ref.descriptor_version,
        "descriptor_digest": binding.descriptor_ref.descriptor_digest,
        "operation_id": binding.operation_ref.operation_id,
        "operation_version": binding.operation_ref.operation_version,
        "input_schema_digest": binding.operation_ref.input_schema_digest,
        "output_schema_digest": binding.operation_ref.output_schema_digest,
        "instance_id": binding.instance_ref.instance_id,
        "discovered_provider_version": binding.instance_ref.discovered_version,
        "discovered_resource_version": binding.discovered_resource_version,
        "instance_fingerprint": binding.instance_ref.instance_fingerprint,
        "configuration_id": binding.configuration_ref.configuration_id,
        "configuration_digest": binding.configuration_ref.configuration_digest,
        "connection_id": (
            binding.connection_ref.connection_id if has_connection else None
        ),
        "connection_auth_mode": (
            binding.connection_ref.auth_mode.value if has_connection else None
        ),
        "connection_authorization_digest": (
            binding.connection_ref.authorization_digest if has_connection else None
        ),
        "policy_id": binding.policy_ref.policy_id,
        "policy_version": binding.policy_ref.policy_version,
        "policy_digest": binding.policy_ref.policy_digest,
        "allowed_destination_constraints": list(binding.allowed_destination_constraints),
        "allowed_destinations_digest": binding.allowed_destinations_digest,
    }


def capability_binding(
    *,
    binding_id: str,
    logical_agent_id: str,
    instance: CapabilityInstance,
    descriptor: CapabilityDescriptor,
    operation: OperationDescriptor,
    policy_ref: PolicyRef,
) -> CapabilityBinding:
    destinations = instance.allowed_destination_constraints or operation.side_effect_destinations
    fingerprint = capability_instance_fingerprint(
        instance,
        descriptor,
        policy_ref=policy_ref,
    )
    binding = CapabilityBinding(
        binding_id=binding_id,
        logical_agent_id=logical_agent_id,
        provider_contract_version=PROVIDER_CONTRACT_VERSION,
        canonicalization_version=CANONICALIZATION_VERSION,
        descriptor_ref=descriptor_ref(descriptor),
        operation_ref=operation_ref(operation),
        instance_ref=InstanceRef(
            provider_id=instance.provider_id,
            instance_id=instance.instance_id,
            discovered_version=instance.discovered_provider_version,
            instance_fingerprint=fingerprint,
        ),
        configuration_ref=configuration_ref(instance),
        connection_ref=connection_ref(instance),
        policy_ref=policy_ref,
        operations_digest=capability_operations_digest(descriptor),
        provider_resource_id=instance.provider_resource_id,
        discovered_resource_version=instance.discovered_resource_version,
        configuration=instance.configuration,
        connection_version=instance.connection_version,
        connection_identity_mode=instance.connection_identity_mode,
        connection_scopes=instance.connection_scopes,
        connection_roles=instance.connection_roles,
        allowed_destination_constraints=tuple(destinations),
        allowed_destinations_digest=canonical_json_hash(list(sorted(destinations))),
        tenant_scope=instance.tenant_id,
        project_scope=instance.project_id,
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
    status: ApprovalDecisionStatus
    provider_contract_version: str
    tenant_id: str
    actor_id: str
    instance_id: str
    project_id: str
    provider_resource_id: str
    instance_fingerprint: str
    descriptor_id: str
    descriptor_version: str
    operation_id: str
    operation_version: str
    arguments_hash: str
    destination_hash: str
    issued_at: str
    expires_at: str
    policy_ref: PolicyRef
    binding_id: str | None = None
    use_policy: ApprovalUsePolicy = ApprovalUsePolicy.ONE_TIME
    max_uses: int = 1

    def __post_init__(self) -> None:
        if not all(
            (
                self.decision_id,
                self.provider_contract_version,
                self.tenant_id,
                self.actor_id,
                self.instance_id,
                self.project_id,
                self.provider_resource_id,
                self.descriptor_id,
                self.descriptor_version,
                self.operation_id,
                self.operation_version,
            )
        ):
            raise ValueError("Approval decision identity and policy bindings are required")
        if self.provider_contract_version != PROVIDER_CONTRACT_VERSION:
            raise ValueError("Approval decision provider contract version is unsupported")
        for field_name, value in (
            ("instance_fingerprint", self.instance_fingerprint),
            ("arguments_hash", self.arguments_hash),
            ("destination_hash", self.destination_hash),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"Approval decision {field_name} must be lowercase SHA-256")
        _validate_utc_timestamp(self.expires_at, path="approval.expires_at")
        _validate_utc_timestamp(self.issued_at, path="approval.issued_at")
        if datetime.fromisoformat(self.issued_at.replace("Z", "+00:00")) >= datetime.fromisoformat(
            self.expires_at.replace("Z", "+00:00")
        ):
            raise ValueError("Approval decision expiry must be after issuance")
        if (
            (self.use_policy is ApprovalUsePolicy.ONE_TIME
            and self.max_uses != 1)
            or (self.use_policy is ApprovalUsePolicy.BOUNDED_REUSABLE
            and self.max_uses < 2)
        ):
            raise ValueError("Approval decision use policy and maximum uses are inconsistent")


@dataclass(frozen=True, slots=True)
class ApprovalConsumptionRequest:
    decision_id: str
    provider_contract_version: str
    tenant_id: str
    project_id: str
    principal_id: str
    binding_id: str | None
    instance_fingerprint: str
    descriptor_id: str
    descriptor_version: str
    operation_id: str
    operation_version: str
    arguments_hash: str
    resolved_destination_hash: str
    policy_ref: PolicyRef
    release_id: str
    invocation_id: str
    idempotency_key: str | None
    use_policy: ApprovalUsePolicy
    max_uses: int

    def __post_init__(self) -> None:
        if not all(
            (
                self.decision_id,
                self.provider_contract_version,
                self.tenant_id,
                self.project_id,
                self.principal_id,
                self.instance_fingerprint,
                self.descriptor_id,
                self.descriptor_version,
                self.operation_id,
                self.operation_version,
                self.release_id,
                self.invocation_id,
            )
        ):
            raise ValueError("Approval consumption request identity and release bindings are required")
        if self.provider_contract_version != PROVIDER_CONTRACT_VERSION:
            raise ValueError("Approval consumption request provider contract version is unsupported")
        for field_name, value in (
            ("instance_fingerprint", self.instance_fingerprint),
            ("arguments_hash", self.arguments_hash),
            ("resolved_destination_hash", self.resolved_destination_hash),
        ):
            _validate_sha256(value, path=f"approval_consumption.{field_name}")
        if (
            (self.use_policy is ApprovalUsePolicy.ONE_TIME
            and self.max_uses != 1)
            or (self.use_policy is ApprovalUsePolicy.BOUNDED_REUSABLE
            and self.max_uses < 2)
        ):
            raise ValueError("Approval consumption use policy and maximum uses are inconsistent")


@dataclass(frozen=True, slots=True)
class ApprovalConsumptionResult:
    status: ApprovalConsumptionStatus
    consumption_record_id: str | None = None
    consumed_at: str | None = None

    def __post_init__(self) -> None:
        successful = self.status in {
            ApprovalConsumptionStatus.CONSUMED,
            ApprovalConsumptionStatus.ALREADY_CONSUMED_BY_SAME_IDEMPOTENT_INVOCATION,
        }
        if successful != bool(self.consumption_record_id and self.consumed_at):
            raise ValueError("Approval consumption results require immutable evidence only on success")
        if self.consumed_at is not None:
            _validate_utc_timestamp(self.consumed_at, path="approval_consumption.consumed_at")


ApprovalConsumptionPort = Callable[
    [ApprovalConsumptionRequest],
    Coroutine[Any, Any, ApprovalConsumptionResult],
]


async def _unavailable_approval_consumption(
    _request: ApprovalConsumptionRequest,
) -> ApprovalConsumptionResult:
    return ApprovalConsumptionResult(ApprovalConsumptionStatus.UNAVAILABLE)


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
    release_id: str
    invocation_id: str
    approval_decisions: tuple[ApprovalDecision, ...] = ()
    authorized_policy_exceptions: tuple[PolicyRef, ...] = ()
    policy_ref: PolicyRef = field(default_factory=lambda: policy_reference("agent-studio-v1"))
    deadline_at: str | None = None
    is_cancelled: Callable[[], bool] = field(default=lambda: False, repr=False)
    consume_approval: ApprovalConsumptionPort = field(
        default=_unavailable_approval_consumption,
        repr=False,
    )
    logical_agent_id: str | None = None

    def __post_init__(self) -> None:
        if not all(
            (
                self.tenant_id,
                self.principal_id,
                self.project_id,
                self.correlation_id,
                self.trace_id,
                self.release_id,
                self.invocation_id,
            )
        ):
            raise ValueError(
                "Tenant, principal, project, correlation, trace, release, and invocation identifiers are required"
            )
        if self.deadline_at is not None:
            _validate_utc_timestamp(self.deadline_at, path="invocation.deadline_at")
        if self.logical_agent_id == "":
            raise ValueError("Invocation logical agent identity cannot be empty")
        now = datetime.now(UTC)
        if any(
            decision.status is not ApprovalDecisionStatus.APPROVED
            or datetime.fromisoformat(decision.expires_at.replace("Z", "+00:00")) <= now
            for decision in self.approval_decisions
        ):
            raise ValueError("Executable invocation contexts may contain only active approved decisions")
        if len(set(self.authorized_policy_exceptions)) != len(self.authorized_policy_exceptions):
            raise ValueError("Authorized policy exception references must be unique")

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
        if (
            isinstance(self.target, CapabilityBinding)
            and self.target.operation_ref.operation_id != self.operation_id
        ):
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


class PlatformProvider(Protocol):
    """Synchronous management/test adapter; agent runtime uses bound ToolRegistration."""

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


class Provider(Protocol):
    """Canonical cancellation/deadline-aware runtime provider protocol."""

    @property
    def descriptor(self) -> ProviderDescriptor: ...

    async def discover(self, context: InvocationContext) -> DiscoveryResult: ...

    async def validate(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> ValidationReport: ...

    async def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport: ...

    async def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult: ...


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
    LIFECYCLE_NOT_ACTIVE = "lifecycle_not_active"
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
    if isinstance(value, str):
        length_constraints: tuple[
            tuple[str, Callable[[int, int], bool], str], ...
        ] = (
            ("minLength", lambda actual, limit: actual >= limit, "at least"),
            ("maxLength", lambda actual, limit: actual <= limit, "at most"),
        )
        for keyword, comparison, description in length_constraints:
            if keyword not in schema:
                continue
            limit = schema[keyword]
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                raise ValueError(f"{path} has an invalid {keyword} schema constraint")
            if not comparison(len(value), limit):
                raise ValueError(f"{path} must contain {description} {limit} characters")
        if "pattern" in schema:
            pattern = schema["pattern"]
            max_length = schema.get("maxLength")
            if (
                not isinstance(pattern, str)
                or len(pattern) > 256
                or not isinstance(max_length, int)
                or isinstance(max_length, bool)
                or not 0 <= max_length <= 4096
                or re.search(r"[(){}]|\\[1-9gk]|\\[AbBZ]|\\p|\(\?", pattern)
            ):
                raise ValueError(f"{path} has an unsupported pattern schema constraint")
            try:
                matched = re.search(pattern, value) is not None
            except re.error as exc:
                raise ValueError(f"{path} has an invalid pattern schema constraint") from exc
            if not matched:
                raise ValueError(f"{path} does not match the declared pattern")
    if isinstance(value, int | float) and not isinstance(value, bool):
        for keyword, comparison, description in (
            ("minimum", lambda actual, limit: actual >= limit, "at least"),
            ("maximum", lambda actual, limit: actual <= limit, "at most"),
            ("exclusiveMinimum", lambda actual, limit: actual > limit, "greater than"),
            ("exclusiveMaximum", lambda actual, limit: actual < limit, "less than"),
        ):
            if keyword not in schema:
                continue
            limit = schema[keyword]
            if not isinstance(limit, int | float) or isinstance(limit, bool) or not math.isfinite(limit):
                raise ValueError(f"{path} has an invalid {keyword} schema constraint")
            if not comparison(value, limit):
                raise ValueError(f"{path} must be {description} {limit}")
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
    policy_ref: PolicyRef | None,
) -> tuple[BindabilityDecision, ...]:
    descriptor = discovery.descriptor_for(instance)
    decisions: list[BindabilityDecision] = []
    for operation in descriptor.operations:
        reasons: list[BindabilityReason] = []
        if operation.maturity is not Maturity.GA:
            reasons.append(BindabilityReason.MATURITY_NOT_GA)
        if operation.lifecycle is not Lifecycle.ACTIVE:
            reasons.append(BindabilityReason.LIFECYCLE_NOT_ACTIVE)
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
    policy_ref: PolicyRef | None = None,
    logical_agent_id: str | None = None,
) -> OperationDescriptor:
    active_policy_ref = policy_ref or binding.policy_ref
    operation = next(
        (
            item
            for item in descriptor.operations
            if item.operation_id == binding.operation_ref.operation_id
            and item.operation_version == binding.operation_ref.operation_version
        ),
        None,
    )
    current_fingerprint = capability_instance_fingerprint(
        instance,
        descriptor,
        policy_ref=active_policy_ref,
    )
    if binding.instance_ref.instance_fingerprint != current_fingerprint:
        categories: list[BindingChangeCategory] = []
        if binding.instance_ref.provider_id != instance.provider_id:
            categories.append(BindingChangeCategory.PROVIDER)
        if binding.descriptor_ref != descriptor_ref(descriptor):
            categories.append(BindingChangeCategory.DESCRIPTOR)
        if (
            binding.operations_digest != capability_operations_digest(descriptor)
            or operation is None
            or binding.operation_ref != operation_ref(operation)
        ):
            categories.append(BindingChangeCategory.OPERATIONS)
        if (
            binding.instance_ref.instance_id != instance.instance_id
            or binding.instance_ref.discovered_version != instance.discovered_provider_version
            or binding.provider_resource_id != instance.provider_resource_id
            or binding.discovered_resource_version != instance.discovered_resource_version
        ):
            categories.append(BindingChangeCategory.INSTANCE)
        if (
            binding.tenant_scope != instance.tenant_id
            or binding.project_scope != instance.project_id
        ):
            categories.append(BindingChangeCategory.BOUNDARY)
        if (
            binding.connection_ref != connection_ref(instance)
            or binding.connection_version != instance.connection_version
            or binding.connection_identity_mode != instance.connection_identity_mode
            or binding.connection_scopes != instance.connection_scopes
            or binding.connection_roles != instance.connection_roles
        ):
            categories.append(BindingChangeCategory.CONNECTION)
        effective_destinations = instance.allowed_destination_constraints or (
            operation.side_effect_destinations if operation is not None else ()
        )
        if set(binding.allowed_destination_constraints) != set(effective_destinations):
            categories.append(BindingChangeCategory.DESTINATIONS)
        if binding.policy_ref != active_policy_ref:
            categories.append(BindingChangeCategory.POLICY)
        if (
            binding.configuration_ref != configuration_ref(instance)
            or binding.configuration != instance.configuration
        ):
            categories.append(BindingChangeCategory.CONFIGURATION)
        if not categories:
            categories.append(BindingChangeCategory.INSTANCE)
        raise StaleBindingError(
            provider_id=instance.provider_id,
            instance_id=instance.instance_id,
            old_fingerprint=binding.instance_ref.instance_fingerprint,
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
    if instance.auth_mode is not AuthMode.NONE and not instance.connection_ref:
        raise ValueError("Capability binding requires a stable authorized connection identity")
    if logical_agent_id is not None and binding.logical_agent_id != logical_agent_id:
        raise ValueError("Capability binding belongs to a different logical agent")
    if (
        binding.tenant_scope != instance.tenant_id
        or binding.project_scope != instance.project_id
    ):
        raise ValueError("Capability binding tenant or project scope does not match")
    if binding.instance_ref.provider_id != instance.provider_id:
        raise ValueError("Capability binding belongs to a different provider")
    if binding.instance_ref.instance_id != instance.instance_id:
        raise ValueError("Capability binding belongs to a different instance")
    if binding.descriptor_ref != descriptor_ref(descriptor):
        raise ValueError("Capability binding belongs to a different descriptor")
    if binding.operations_digest != capability_operations_digest(descriptor):
        raise ValueError("Capability binding operation set digest does not match")
    if binding.instance_ref.discovered_version != instance.discovered_provider_version:
        raise ValueError("Capability binding resource identity or discovered instance version does not match")
    if (
        binding.provider_resource_id != instance.provider_resource_id
        or binding.discovered_resource_version != instance.discovered_resource_version
    ):
        raise ValueError("Capability binding resource identity or discovered instance version does not match")
    if (
        binding.connection_ref != connection_ref(instance)
        or binding.connection_version != instance.connection_version
        or binding.connection_identity_mode != instance.connection_identity_mode
        or binding.connection_scopes != instance.connection_scopes
        or binding.connection_roles != instance.connection_roles
    ):
        raise ValueError("Capability binding connection reference or authorization scope does not match")
    if (
        binding.configuration_ref != configuration_ref(instance)
        or binding.configuration != instance.configuration
    ):
        raise ValueError("Capability binding configuration reference does not match")
    if binding.policy_ref != active_policy_ref:
        raise ValueError("Capability binding policy reference does not match")
    if operation is None:
        raise ValueError("Capability binding operation or version is not declared")
    if binding.operation_ref.input_schema_digest != operation.input_schema_digest:
        raise ValueError("Capability binding input schema digest does not match")
    if binding.operation_ref.output_schema_digest != operation.output_schema_digest:
        raise ValueError("Capability binding output schema digest does not match")
    if operation.maturity is not Maturity.GA:
        raise ValueError("Only GA operations can be bound")
    if operation.lifecycle is not Lifecycle.ACTIVE:
        raise ValueError("Only active operations can be bound")
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
    policy_ref: PolicyRef | None = None,
    logical_agent_id: str | None = None,
) -> tuple[CapabilityInstance, CapabilityBinding | None]:
    target_instance_id = (
        target.instance_ref.instance_id if isinstance(target, CapabilityBinding) else target.instance_id
    )
    current = next(
        (instance for instance in discovery.instances if instance.instance_id == target_instance_id),
        None,
    )
    if current is None:
        raise UnavailableError(
            "Capability instance is not present in current provider discovery",
            provider_id=provider_id,
            instance_id=target_instance_id,
        )
    if isinstance(target, CapabilityBinding):
        if logical_agent_id is None:
            raise PolicyError(
                "Bound capability resolution requires a trusted logical agent identity",
                provider_id=provider_id,
                instance_id=current.instance_id,
            )
        try:
            validate_binding(
                current,
                discovery.descriptor_for(current),
                target,
                policy_ref=policy_ref,
                logical_agent_id=logical_agent_id,
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
    active_policy_ref = policy_ref or policy_reference("unbound")
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
                "connection_id": target.connection_ref,
                "connection_version": target.connection_version,
                "auth_mode": target.auth_mode.value,
                "identity_mode": target.connection_identity_mode,
                "connection_scopes": sorted(target.connection_scopes),
                "connection_roles": sorted(target.connection_roles),
                "authorization_digest": authorization_digest(target),
                "allowed_destination_constraints": sorted(target.allowed_destination_constraints),
                "policy_ref": {
                    "policy_id": active_policy_ref.policy_id,
                    "policy_version": active_policy_ref.policy_version,
                    "policy_digest": active_policy_ref.policy_digest,
                },
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
            or current.connection_version != target.connection_version
            or current.auth_mode is not target.auth_mode
            or current.connection_identity_mode != target.connection_identity_mode
            or set(current.connection_scopes) != set(target.connection_scopes)
            or set(current.connection_roles) != set(target.connection_roles)
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
    policy_ref: PolicyRef | None = None,
    logical_agent_id: str | None = None,
) -> ValidationReport:
    instance, _ = resolve_capability_target(
        discovery,
        target,
        provider_id=provider_id,
        policy_ref=policy_ref,
        logical_agent_id=logical_agent_id,
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
    policy_ref: PolicyRef | None = None,
    logical_agent_id: str | None = None,
) -> HealthReport:
    instance, _ = resolve_capability_target(
        discovery,
        target,
        provider_id=provider_id,
        policy_ref=policy_ref,
        logical_agent_id=logical_agent_id,
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
    status: ApprovalDecisionStatus = ApprovalDecisionStatus.APPROVED,
) -> ApprovalDecision:
    if isinstance(target, CapabilityBinding) and context.logical_agent_id != target.logical_agent_id:
        raise PolicyError(
            "Approval context does not match the capability binding logical agent",
            provider_id=instance.provider_id,
            instance_id=instance.instance_id,
        )
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
        status=status,
        provider_contract_version=PROVIDER_CONTRACT_VERSION,
        tenant_id=context.tenant_id,
        actor_id=context.principal_id,
        instance_id=instance.instance_id,
        project_id=instance.project_id,
        provider_resource_id=instance.provider_resource_id,
        instance_fingerprint=capability_instance_fingerprint(
            instance,
            descriptor,
            policy_ref=context.policy_ref,
        ),
        descriptor_id=instance.descriptor_id,
        descriptor_version=instance.descriptor_version,
        operation_id=operation.operation_id,
        operation_version=operation.operation_version,
        arguments_hash=canonical_json_hash(arguments),
        destination_hash=canonical_json_hash(destinations),
        issued_at=utc_now(),
        expires_at=expires_at,
        policy_ref=context.policy_ref,
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
        decision.status is ApprovalDecisionStatus.APPROVED
        and decision.tenant_id == context.tenant_id
        and decision.actor_id == context.principal_id
        and decision.instance_id == instance.instance_id
        and decision.project_id == instance.project_id
        and decision.provider_resource_id == instance.provider_resource_id
        and decision.provider_contract_version == PROVIDER_CONTRACT_VERSION
        and decision.instance_fingerprint
        == capability_instance_fingerprint(
            instance,
            descriptor,
            policy_ref=context.policy_ref,
        )
        and decision.descriptor_id == instance.descriptor_id
        and decision.descriptor_version == instance.descriptor_version
        and decision.operation_id == operation.operation_id
        and decision.operation_version == operation.operation_version
        and decision.arguments_hash == canonical_json_hash(request.arguments)
        and decision.destination_hash == canonical_json_hash(resolved_destinations)
        and decision.policy_ref == context.policy_ref
        and decision.binding_id == (binding.binding_id if binding else None)
        and expires_at > datetime.now(UTC)
    )


def _validate_approval_consumption(
    result: ApprovalConsumptionResult,
    consumption_request: ApprovalConsumptionRequest,
    *,
    operation: OperationDescriptor,
    provider_id: str,
    instance_id: str,
) -> None:
    if result.status is ApprovalConsumptionStatus.CONSUMED:
        return
    if (
        result.status
        is ApprovalConsumptionStatus.ALREADY_CONSUMED_BY_SAME_IDEMPOTENT_INVOCATION
        and consumption_request.idempotency_key is not None
        and operation.idempotency is not Idempotency.NONE
        and operation.operation_class is not OperationClass.WRITE_IRREVERSIBLE
    ):
        return
    raise PolicyError(
        f"Approval consumption failed: {result.status.value}",
        provider_id=provider_id,
        instance_id=instance_id,
    )


async def _consume_approval_decision_async(
    context: InvocationContext,
    consumption_request: ApprovalConsumptionRequest,
    *,
    operation: OperationDescriptor,
    provider_id: str,
    instance_id: str,
) -> None:
    timeout_seconds = context.remaining_seconds(
        provider_id=provider_id,
        instance_id=instance_id,
    )
    if timeout_seconds is None:
        timeout_seconds = operation.timeout_seconds
    try:
        result = await asyncio.wait_for(
            context.consume_approval(consumption_request),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        raise ProviderTimeoutError(
            "Approval consumption exceeded the invocation deadline",
            provider_id=provider_id,
            instance_id=instance_id,
        ) from exc
    except OSError as exc:
        raise PolicyError(
            "Approval consumption store is unavailable",
            provider_id=provider_id,
            instance_id=instance_id,
        ) from exc
    if not isinstance(result, ApprovalConsumptionResult):
        raise PolicyError(
            "Approval consumption store returned an invalid result",
            provider_id=provider_id,
            instance_id=instance_id,
        )
    _validate_approval_consumption(
        result,
        consumption_request,
        operation=operation,
        provider_id=provider_id,
        instance_id=instance_id,
    )


def _consume_approval_decision(
    context: InvocationContext,
    consumption_request: ApprovalConsumptionRequest,
    *,
    operation: OperationDescriptor,
    provider_id: str,
    instance_id: str,
) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(
            _consume_approval_decision_async(
                context,
                consumption_request,
                operation=operation,
                provider_id=provider_id,
                instance_id=instance_id,
            )
        )
        return
    raise PolicyError(
        "Async provider invocation must await find_operation_async",
        provider_id=provider_id,
        instance_id=instance_id,
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
        policy_ref=context.policy_ref,
        logical_agent_id=context.logical_agent_id,
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
    if instance.auth_mode is not AuthMode.NONE and not instance.connection_ref:
        raise PolicyError(
            "Capability requires a stable authorized connection identity",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    operation = next(
        (
            item
            for item in descriptor.operations
            if item.operation_id == request.operation_id
            and (
                binding is None
                or item.operation_version == binding.operation_ref.operation_version
            )
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
    if operation.lifecycle is not Lifecycle.ACTIVE:
        raise UnavailableError(
            "Only active operations can be invoked",
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
        destination_constraints=(
            binding.allowed_destination_constraints if binding is not None else None
        ),
    )
    if operation.idempotency is Idempotency.CALLER_KEY and not request.idempotency_key:
        raise PolicyError(
            "An idempotency key is required",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    if operation.idempotency is not Idempotency.CALLER_KEY and request.idempotency_key is not None:
        raise PolicyError(
            "This operation does not accept a caller idempotency key",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    if (
        operation.policy_exception_ref is not None
        and operation.policy_exception_ref not in context.authorized_policy_exceptions
    ):
        raise PolicyError(
            "The operation policy exception is not independently authorized",
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
        _consume_approval_decision(
            context,
            ApprovalConsumptionRequest(
                decision_id=decision.decision_id,
                provider_contract_version=PROVIDER_CONTRACT_VERSION,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                principal_id=context.principal_id,
                binding_id=binding.binding_id if binding is not None else None,
                instance_fingerprint=decision.instance_fingerprint,
                descriptor_id=descriptor.descriptor_id,
                descriptor_version=descriptor.descriptor_version,
                operation_id=operation.operation_id,
                operation_version=operation.operation_version,
                arguments_hash=canonical_json_hash(request.arguments),
                resolved_destination_hash=canonical_json_hash(resolved_destinations),
                policy_ref=context.policy_ref,
                release_id=context.release_id,
                invocation_id=context.invocation_id,
                idempotency_key=request.idempotency_key,
                use_policy=decision.use_policy,
                max_uses=decision.max_uses,
            ),
            operation=operation,
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
        context.raise_if_cancelled_or_expired(
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    return instance, operation


async def find_operation_async(
    discovery: DiscoveryResult,
    request: InvocationRequest,
    context: InvocationContext,
    *,
    provider_id: str,
    tenant_id: str | None,
) -> tuple[CapabilityInstance, OperationDescriptor]:
    remaining = context.remaining_seconds(provider_id=provider_id)
    authorization = asyncio.to_thread(
        find_operation,
        discovery,
        request,
        context,
        provider_id=provider_id,
        tenant_id=tenant_id,
    )
    try:
        if remaining is None:
            return await authorization
        return await asyncio.wait_for(authorization, timeout=remaining)
    except TimeoutError as exc:
        raise ProviderTimeoutError(
            "Provider authorization exceeded the invocation deadline",
            provider_id=provider_id,
        ) from exc


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
