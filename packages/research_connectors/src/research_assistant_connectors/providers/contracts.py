"""Immutable contracts for operational capability providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

import httpx

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
JsonSchema = Mapping[str, Any]


class Maturity(StrEnum):
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


class Risk(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


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


@dataclass(frozen=True, slots=True)
class OperationDescriptor:
    operation_id: str
    maturity: Maturity
    input_schema: JsonSchema
    output_schema: JsonSchema
    risk: Risk
    approval_policy: ApprovalPolicy
    timeout_seconds: float = 20.0
    max_retries: int = 2
    idempotency: Idempotency = Idempotency.NONE
    least_privilege_scopes: tuple[str, ...] = ()
    least_privilege_roles: tuple[str, ...] = ()
    docs: tuple[str, ...] = ()
    audit_events: tuple[str, ...] = ("provider.invoke",)

    def __post_init__(self) -> None:
        if not self.operation_id or self.timeout_seconds <= 0 or self.max_retries < 0:
            raise ValueError("Operation identifiers, timeouts, and retries must be valid")
        object.__setattr__(self, "input_schema", _freeze(self.input_schema))
        object.__setattr__(self, "output_schema", _freeze(self.output_schema))


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    provider_id: str
    capability_id: str
    family: str
    resource_kind: str
    name: str
    readiness: Readiness
    attachable: bool
    auth_modes: tuple[AuthMode, ...]
    tenant_boundary: str
    data_boundary: str
    operations: tuple[OperationDescriptor, ...]
    provenance: tuple[str, ...]
    status_evidence: tuple[str, ...]
    observability: tuple[str, ...] = ("correlation_id", "trace_id", "latency_ms", "status")
    audit: tuple[str, ...] = ("principal_id", "tenant_id", "provider_id", "capability_id", "operation_id")
    unavailable_reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider_id or not self.capability_id or not self.operations:
            raise ValueError("Capability identifiers and operations are required")
        is_ga = all(operation.maturity is Maturity.GA for operation in self.operations)
        if self.attachable and (not is_ga or self.readiness is not Readiness.READY):
            raise ValueError("Only ready GA capabilities are attachable")
        if self.readiness is Readiness.READY and self.unavailable_reason is not None:
            raise ValueError("Ready capabilities cannot have an unavailable reason")
        if self.readiness is not Readiness.READY and not self.unavailable_reason:
            raise ValueError("Non-ready capabilities require an unavailable reason")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    family: str
    name: str
    description: str
    auth_modes: tuple[AuthMode, ...]
    provenance: tuple[str, ...]
    capabilities: tuple[CapabilityDescriptor, ...] = ()

    def __post_init__(self) -> None:
        if not self.provider_id or not self.name or not self.provenance:
            raise ValueError("Provider identity and provenance are required")
        if any(capability.provider_id != self.provider_id for capability in self.capabilities):
            raise ValueError("Provider capabilities must use the provider identifier")


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
class InvocationContext:
    tenant_id: str
    principal_id: str
    approved_capability_ids: frozenset[str]
    credential: object | None = field(repr=False)
    transport: httpx.Client
    correlation_id: str
    trace_id: str
    sleep: Callable[[float], None]

    def __post_init__(self) -> None:
        if not all((self.tenant_id, self.principal_id, self.correlation_id, self.trace_id)):
            raise ValueError("Tenant, principal, correlation, and trace identifiers are required")


@dataclass(frozen=True, slots=True)
class InvocationRequest:
    capability_id: str
    operation_id: str
    arguments: Mapping[str, Any] = field(repr=False)
    idempotency_key: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.capability_id or not self.operation_id:
            raise ValueError("Capability and operation identifiers are required")
        if self.idempotency_key is not None and (
            not 1 <= len(self.idempotency_key) <= 256
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in self.idempotency_key)
        ):
            raise ValueError("Idempotency keys must contain 1-256 visible ASCII characters")
        object.__setattr__(self, "arguments", _freeze(self.arguments))


@dataclass(frozen=True, slots=True)
class InvocationResult:
    provider_id: str
    capability_id: str
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

    def discover(self, context: InvocationContext) -> tuple[CapabilityDescriptor, ...]: ...

    def validate(self, context: InvocationContext) -> ValidationReport: ...

    def health(self, context: InvocationContext) -> HealthReport: ...

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult: ...


class ProviderError(Exception):
    code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        capability_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.capability_id = capability_id
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


def find_operation(
    capabilities: tuple[CapabilityDescriptor, ...],
    request: InvocationRequest,
    context: InvocationContext,
    *,
    provider_id: str,
    tenant_id: str | None,
) -> tuple[CapabilityDescriptor, OperationDescriptor]:
    if tenant_id is not None and context.tenant_id != tenant_id:
        raise PolicyError("Invocation tenant is outside the configured boundary", provider_id=provider_id)
    capability = next((item for item in capabilities if item.capability_id == request.capability_id), None)
    if capability is None:
        raise UnavailableError(
            "Capability is not present in current provider discovery",
            provider_id=provider_id,
            capability_id=request.capability_id,
        )
    if capability.readiness is not Readiness.READY or not capability.attachable:
        raise UnavailableError(
            capability.unavailable_reason or "Capability is not ready",
            provider_id=provider_id,
            capability_id=capability.capability_id,
        )
    operation = next((item for item in capability.operations if item.operation_id == request.operation_id), None)
    if operation is None:
        raise ProviderValidationError(
            "Operation is not declared by the capability",
            provider_id=provider_id,
            capability_id=capability.capability_id,
        )
    if (
        operation.approval_policy is ApprovalPolicy.REQUIRED
        and capability.capability_id not in context.approved_capability_ids
    ):
        raise PolicyError(
            "Explicit capability approval is required",
            provider_id=provider_id,
            capability_id=capability.capability_id,
        )
    if operation.idempotency is Idempotency.REQUIRED and not request.idempotency_key:
        raise PolicyError(
            "An idempotency key is required",
            provider_id=provider_id,
            capability_id=capability.capability_id,
        )
    try:
        validate_json(operation.input_schema, request.arguments)
    except ValueError as exc:
        raise ProviderValidationError(
            str(exc),
            provider_id=provider_id,
            capability_id=capability.capability_id,
        ) from exc
    return capability, operation


def audit_metadata(
    context: InvocationContext,
    *,
    provider_id: str,
    capability_id: str,
    operation_id: str,
    attempts: int,
    response: httpx.Response,
) -> Mapping[str, Any]:
    return {
        "provider_id": provider_id,
        "capability_id": capability_id,
        "operation_id": operation_id,
        "tenant_id": context.tenant_id,
        "principal_id": context.principal_id,
        "correlation_id": context.correlation_id,
        "trace_id": context.trace_id,
        "attempts": attempts,
        "status_code": response.status_code,
        "latency_ms": response.extensions.get("provider_elapsed_ms"),
    }
