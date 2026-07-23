"""Immutable contracts for operational capability providers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
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
    REQUIRED = "required"


class Idempotency(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"
    INHERENT = "inherent"


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

    def __post_init__(self) -> None:
        if not self.official_url.startswith("https://") or not self.source_version:
            raise ValueError("Provenance requires an official HTTPS URL and source version")
        _validate_utc_timestamp(self.last_verified_at, path="provenance.last_verified_at")


def official_provenance(
    urls: tuple[str, ...],
    *,
    source_version: str,
    last_verified_at: str,
) -> tuple[ProvenanceRecord, ...]:
    return tuple(
        ProvenanceRecord(
            official_url=url,
            source_version=source_version,
            last_verified_at=last_verified_at,
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
    max_retries: int = 2
    idempotency: Idempotency = Idempotency.NONE
    least_privilege_scopes: tuple[str, ...] = ()
    least_privilege_roles: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()
    audit_events: tuple[str, ...] = ("provider.invoke",)

    def __post_init__(self) -> None:
        if not self.operation_id or not self.version or self.timeout_seconds <= 0 or self.max_retries < 0:
            raise ValueError("Operation identifiers, versions, timeouts, and retries must be valid")
        if any(not destination for destination in self.side_effect_destinations):
            raise ValueError("Side-effect destinations cannot be empty")
        if len(set(self.side_effect_destinations)) != len(self.side_effect_destinations):
            raise ValueError("Side-effect destinations must be unique")
        _validate_json_value(self.input_schema, path="input_schema")
        _validate_json_value(self.output_schema, path="output_schema")
        object.__setattr__(self, "input_schema", _freeze(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze(self.output_schema))


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    descriptor_id: str
    family: str
    resource_kind: str
    name: str
    auth_modes: tuple[AuthMode, ...]
    operations: tuple[OperationDescriptor, ...]
    provenance: tuple[ProvenanceRecord, ...]
    observability: tuple[str, ...] = ("correlation_id", "trace_id", "latency_ms", "status")
    audit: tuple[str, ...] = ("principal_id", "tenant_id", "provider_id", "instance_id", "operation_id")
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not self.descriptor_id
            or not self.family
            or not self.resource_kind
            or not self.operations
            or not self.provenance
        ):
            raise ValueError("Capability descriptor identity and operations are required")
        _validate_json_value(self.metadata, path="descriptor.metadata")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True, slots=True)
class CapabilityInstance:
    provider_id: str
    instance_id: str
    descriptor: CapabilityDescriptor
    name: str
    readiness: Readiness
    tenant_scope: str
    data_boundary: str
    resource_id: str | None = None
    connection_id: str | None = None
    health: Readiness | None = None
    discovered_version: str | None = None
    configuration: Mapping[str, Any] = field(default_factory=dict)
    configuration_fingerprint: str = ""
    status_evidence: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.provider_id or not self.instance_id or not self.name:
            raise ValueError("Capability instance identity is required")
        if self.readiness is Readiness.READY and self.unavailable_reason is not None:
            raise ValueError("Ready capability instances cannot have an unavailable reason")
        if self.readiness is not Readiness.READY and not self.unavailable_reason:
            raise ValueError("Non-ready capability instances require an unavailable reason")
        _validate_json_value(self.configuration, path="instance.configuration")
        object.__setattr__(self, "configuration", _freeze(self.configuration))
        fingerprint = canonical_json_hash(self.configuration)
        if self.configuration_fingerprint and self.configuration_fingerprint != fingerprint:
            raise ValueError("Capability instance configuration fingerprint does not match configuration")
        object.__setattr__(self, "configuration_fingerprint", fingerprint)
        object.__setattr__(self, "health", self.health or self.readiness)
        if self.discovered_version is None:
            versions = {operation.version for operation in self.descriptor.operations}
            object.__setattr__(
                self,
                "discovered_version",
                versions.pop() if len(versions) == 1 else "multiple",
            )

    @property
    def attachable_operation_ids(self) -> tuple[str, ...]:
        if self.readiness is not Readiness.READY:
            return ()
        return tuple(
            operation.operation_id for operation in self.descriptor.operations if operation.maturity is Maturity.GA
        )


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    descriptors: tuple[CapabilityDescriptor, ...]
    instances: tuple[CapabilityInstance, ...]
    warnings: tuple[str, ...]
    refreshed_at: str

    def __post_init__(self) -> None:
        _validate_utc_timestamp(self.refreshed_at, path="discovery.refreshed_at")
        descriptor_ids = {descriptor.descriptor_id for descriptor in self.descriptors}
        if len(descriptor_ids) != len(self.descriptors):
            raise ValueError("Discovery descriptor identities must be unique")
        if len({instance.instance_id for instance in self.instances}) != len(self.instances):
            raise ValueError("Discovery instance identities must be unique")
        if any(instance.descriptor.descriptor_id not in descriptor_ids for instance in self.instances):
            raise ValueError("Every discovered instance must reference a returned descriptor")
        if any(not warning for warning in self.warnings):
            raise ValueError("Discovery warnings cannot be empty")

    def __iter__(self) -> Iterator[CapabilityInstance]:
        return iter(self.instances)

    def __len__(self) -> int:
        return len(self.instances)

    def __getitem__(self, index: int) -> CapabilityInstance:
        return self.instances[index]


def discovery_result(
    instances: Sequence[CapabilityInstance],
    *,
    warnings: tuple[str, ...] = (),
    refreshed_at: str | None = None,
) -> DiscoveryResult:
    canonical_instances = tuple(instances)
    descriptors_by_id = {instance.descriptor.descriptor_id: instance.descriptor for instance in canonical_instances}
    return DiscoveryResult(
        descriptors=tuple(descriptors_by_id.values()),
        instances=canonical_instances,
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
    descriptor_id: str | None = None,
    descriptor_metadata: Mapping[str, Any] | None = None,
    resource_id: str | None = None,
    connection_id: str | None = None,
    health: Readiness | None = None,
    discovered_version: str | None = None,
) -> CapabilityInstance:
    descriptor = CapabilityDescriptor(
        descriptor_id=descriptor_id or instance_id,
        family=family,
        resource_kind=resource_kind,
        name=name,
        auth_modes=auth_modes,
        operations=operations,
        provenance=provenance,
        metadata=descriptor_metadata or {},
    )
    return CapabilityInstance(
        provider_id=provider_id,
        instance_id=instance_id,
        descriptor=descriptor,
        name=name,
        readiness=readiness,
        tenant_scope=tenant_boundary,
        data_boundary=data_boundary,
        resource_id=resource_id or instance_id,
        connection_id=connection_id,
        health=health,
        discovered_version=discovered_version,
        configuration=configuration or {},
        status_evidence=status_evidence,
        unavailable_reason=unavailable_reason,
    )


def capability_instance_fingerprint(instance: CapabilityInstance) -> str:
    descriptor = instance.descriptor
    payload = {
        "provider_id": instance.provider_id,
        "instance_id": instance.instance_id,
        "descriptor": {
            "descriptor_id": descriptor.descriptor_id,
            "family": descriptor.family,
            "resource_kind": descriptor.resource_kind,
            "name": descriptor.name,
            "auth_modes": [mode.value for mode in descriptor.auth_modes],
            "operations": [
                {
                    "operation_id": operation.operation_id,
                    "version": operation.version,
                    "maturity": operation.maturity.value,
                    "input_schema": plain_json(operation.input_schema),
                    "output_schema": plain_json(operation.output_schema),
                    "operation_class": operation.operation_class.value,
                    "approval_policy": operation.approval_policy.value,
                    "external_side_effect": operation.external_side_effect,
                    "side_effect_destinations": list(operation.side_effect_destinations),
                    "timeout_seconds": operation.timeout_seconds,
                    "max_retries": operation.max_retries,
                    "idempotency": operation.idempotency.value,
                    "least_privilege_scopes": list(operation.least_privilege_scopes),
                    "least_privilege_roles": list(operation.least_privilege_roles),
                }
                for operation in descriptor.operations
            ],
            "metadata": plain_json(descriptor.metadata),
        },
        "tenant_scope": instance.tenant_scope,
        "data_boundary": instance.data_boundary,
        "resource_id": instance.resource_id,
        "connection_id": instance.connection_id,
        "discovered_version": instance.discovered_version,
        "configuration": plain_json(instance.configuration),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    binding_id: str
    agent_id: str
    instance_id: str
    descriptor_id: str
    operation_id: str
    operation_version: str
    instance_fingerprint: str
    configuration: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(
            (
                self.binding_id,
                self.agent_id,
                self.instance_id,
                self.descriptor_id,
                self.operation_id,
                self.operation_version,
                self.instance_fingerprint,
            )
        ):
            raise ValueError("Capability binding identifiers and operation version are required")
        if len(self.instance_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.instance_fingerprint
        ):
            raise ValueError("Capability binding instance fingerprint must be lowercase SHA-256")
        _validate_json_value(self.configuration, path="binding.configuration")
        object.__setattr__(self, "configuration", _freeze(self.configuration))


@runtime_checkable
class ToolHandler(Protocol):
    def __call__(
        self,
        arguments: Mapping[str, Any],
        context: InvocationContext,
    ) -> InvocationResult: ...


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
                self.descriptor_id,
                self.operation_id,
                self.operation_version,
                self.policy_release,
            )
        ):
            raise ValueError("Approval decision identity and policy bindings are required")
        for field_name, value in (
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
    credential: object | None = field(repr=False)
    transport: httpx.Client
    correlation_id: str
    trace_id: str
    sleep: Callable[[float], None]
    approval_decisions: tuple[ApprovalDecision, ...] = ()
    policy_release: str = "agent-studio-v1"

    def __post_init__(self) -> None:
        if not all(
            (
                self.tenant_id,
                self.principal_id,
                self.correlation_id,
                self.trace_id,
                self.policy_release,
            )
        ):
            raise ValueError("Tenant, principal, correlation, trace, and policy release identifiers are required")


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


def validate_binding(
    instance: CapabilityInstance,
    binding: CapabilityBinding,
) -> OperationDescriptor:
    if binding.instance_id != instance.instance_id:
        raise ValueError("Capability binding targets a different instance")
    if binding.descriptor_id != instance.descriptor.descriptor_id:
        raise ValueError("Capability binding targets a different descriptor")
    if binding.instance_fingerprint != capability_instance_fingerprint(instance):
        raise ValueError("Capability binding targets changed instance configuration")
    if instance.readiness is not Readiness.READY:
        raise ValueError("Capability binding requires a ready instance")
    operation = next(
        (
            item
            for item in instance.descriptor.operations
            if item.operation_id == binding.operation_id and item.version == binding.operation_version
        ),
        None,
    )
    if operation is None:
        raise ValueError("Capability binding operation or version is not declared")
    if operation.maturity is not Maturity.GA:
        raise ValueError("Only GA operations can be bound")
    return operation


def resolve_capability_target(
    discovery: DiscoveryResult,
    target: CapabilityInstance | CapabilityBinding,
    *,
    provider_id: str,
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
            validate_binding(current, target)
        except ValueError as exc:
            raise PolicyError(
                str(exc),
                provider_id=provider_id,
                instance_id=current.instance_id,
            ) from exc
        return current, target
    if capability_instance_fingerprint(current) != capability_instance_fingerprint(target):
        raise PolicyError(
            "Capability instance changed since it was selected",
            provider_id=provider_id,
            instance_id=current.instance_id,
        )
    return current, None


def validation_for_target(
    discovery: DiscoveryResult,
    target: CapabilityInstance | CapabilityBinding,
    *,
    provider_id: str,
) -> ValidationReport:
    instance, _ = resolve_capability_target(discovery, target, provider_id=provider_id)
    return ValidationReport(
        instance.readiness,
        () if instance.readiness is Readiness.READY else (instance.unavailable_reason or "Not ready",),
    )


def health_for_target(
    discovery: DiscoveryResult,
    target: CapabilityInstance | CapabilityBinding,
    *,
    provider_id: str,
) -> HealthReport:
    instance, _ = resolve_capability_target(discovery, target, provider_id=provider_id)
    return HealthReport(instance.health or instance.readiness, instance.status_evidence)


def approval_decision(
    context: InvocationContext,
    *,
    target: CapabilityInstance | CapabilityBinding,
    instance: CapabilityInstance,
    operation: OperationDescriptor,
    arguments: Mapping[str, Any],
    decision_id: str,
    expires_at: str,
    approved: bool = True,
) -> ApprovalDecision:
    return ApprovalDecision(
        decision_id=decision_id,
        approved=approved,
        tenant_id=context.tenant_id,
        principal_id=context.principal_id,
        instance_id=instance.instance_id,
        descriptor_id=instance.descriptor.descriptor_id,
        operation_id=operation.operation_id,
        operation_version=operation.version,
        arguments_hash=canonical_json_hash(arguments),
        destination_hash=canonical_json_hash(operation.side_effect_destinations),
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
    return operation.idempotency is not Idempotency.NONE


def _approval_matches(
    decision: ApprovalDecision,
    *,
    context: InvocationContext,
    instance: CapabilityInstance,
    operation: OperationDescriptor,
    request: InvocationRequest,
    binding: CapabilityBinding | None,
) -> bool:
    expires_at = datetime.fromisoformat(decision.expires_at.replace("Z", "+00:00"))
    return (
        decision.approved
        and decision.tenant_id == context.tenant_id
        and decision.principal_id == context.principal_id
        and decision.instance_id == instance.instance_id
        and decision.descriptor_id == instance.descriptor.descriptor_id
        and decision.operation_id == operation.operation_id
        and decision.operation_version == operation.version
        and decision.arguments_hash == canonical_json_hash(request.arguments)
        and decision.destination_hash == canonical_json_hash(operation.side_effect_destinations)
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
    )
    if instance.readiness is not Readiness.READY:
        raise UnavailableError(
            instance.unavailable_reason or "Capability instance is not ready",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    operation = next(
        (item for item in instance.descriptor.operations if item.operation_id == request.operation_id),
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
    if operation.approval_policy is ApprovalPolicy.REQUIRED and not any(
        _approval_matches(
            decision,
            context=context,
            instance=instance,
            operation=operation,
            request=request,
            binding=binding,
        )
        for decision in context.approval_decisions
    ):
        raise PolicyError(
            "A current approval decision bound to this invocation is required",
            provider_id=provider_id,
            instance_id=instance.instance_id,
        )
    if operation.idempotency is Idempotency.REQUIRED and not request.idempotency_key:
        raise PolicyError(
            "An idempotency key is required",
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
