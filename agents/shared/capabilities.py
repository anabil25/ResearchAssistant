from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, cast
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import (
    ApprovalRequiredError,
    AuthorizationError,
    CapabilityNotFoundError,
    ConfigurationError,
    DeadlineExceededError,
    DestinationDeniedError,
    HarnessError,
    IdempotencyRequiredError,
    InvocationError,
    StaleCapabilityBindingError,
)

CapabilityHandler = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


class OperationClass(StrEnum):
    PURE = "pure"
    READ = "read"
    WRITE_REVERSIBLE = "write_reversible"
    WRITE_IRREVERSIBLE = "write_irreversible"
    PRIVILEGED = "privileged"


class ApprovalMode(StrEnum):
    NEVER = "never"
    REQUIRED = "required"
    ALWAYS = "always"


class IdempotencyMode(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=1, ge=1, le=5)
    delays_seconds: tuple[float, ...] = ()

    @model_validator(mode="after")
    def delays_cover_retries(self) -> RetryPolicy:
        if len(self.delays_seconds) < self.max_attempts - 1:
            raise ValueError("retry delays must cover every retry")
        if any(delay < 0 or delay > 60 for delay in self.delays_seconds):
            raise ValueError("retry delays must be between 0 and 60 seconds")
        return self


class CapabilityDescriptor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    operation: OperationClass
    required_scopes: frozenset[str] = frozenset()
    allowed_destinations: tuple[str, ...] = ()
    side_effect_destinations: tuple[str, ...] = ()
    approval: ApprovalMode = ApprovalMode.NEVER
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    idempotency: IdempotencyMode = IdempotencyMode.NONE
    retry: RetryPolicy = RetryPolicy()
    redact_fields: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def write_controls(self) -> CapabilityDescriptor:
        if self.operation in {
            OperationClass.WRITE_REVERSIBLE,
            OperationClass.WRITE_IRREVERSIBLE,
            OperationClass.PRIVILEGED,
        }:
            if self.approval == ApprovalMode.NEVER:
                raise ValueError("side-effecting capabilities require deterministic approval")
            if self.idempotency != IdempotencyMode.REQUIRED:
                raise ValueError("side-effecting capabilities require idempotency")
            if not self.side_effect_destinations:
                raise ValueError("side-effecting capabilities require explicit destinations")
        return self


class CapabilityBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    descriptor_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    descriptor_version: str = Field(min_length=1, max_length=128)
    operation_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,127}$")
    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    instance_id: str = Field(min_length=1, max_length=256)
    instance_ref: str = Field(min_length=1, max_length=512)
    instance_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_contract_version: str = Field(min_length=1, max_length=128)
    provider_contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pinned_provider_version: str = Field(min_length=1, max_length=128)
    input_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    config: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_ref: str | None = Field(default=None, min_length=1, max_length=512)
    connection_ref: str = Field(min_length=1, max_length=512)
    policy_ref: str = Field(min_length=1, max_length=512)
    destination_pins: tuple[str, ...]
    tenant_scope: str = Field(min_length=1, max_length=256)
    project_scope: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def config_has_one_source(self) -> CapabilityBinding:
        if self.config and self.config_ref is not None:
            raise ValueError("capability binding config and config_ref are mutually exclusive")
        return self

    @property
    def capability_id(self) -> str:
        return self.descriptor_id


def template_instance_fingerprint(binding: CapabilityBinding) -> str:
    return _canonical_digest(
        binding.model_dump(
            mode="json",
            exclude={"instance_fingerprint"},
        )
    )


def resolved_instance_fingerprint(
    binding: CapabilityBinding,
    descriptor: CapabilityDescriptor,
    *,
    project_endpoint: str,
    destination_endpoint: str | None,
) -> str:
    return _canonical_digest(
        {
            "binding": binding.model_dump(
                mode="json",
                exclude={"instance_fingerprint"},
            ),
            "descriptor": descriptor.model_dump(mode="json"),
            "runtime_instance": {
                "project_endpoint": project_endpoint.rstrip("/"),
                "destination_endpoint": (
                    destination_endpoint.rstrip("/")
                    if destination_endpoint is not None
                    else None
                ),
            },
        }
    )


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    binding: CapabilityBinding
    tool_name: str
    handler: CapabilityHandler
    current_instance_fingerprint: str

    def __post_init__(self) -> None:
        if (
            len(self.current_instance_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in self.current_instance_fingerprint)
        ):
            raise ValueError("Current capability instance fingerprint must be lowercase SHA-256")


class ProviderInstanceAttestation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str
    provider_contract_version: str
    provider_contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    descriptor_id: str
    descriptor_version: str
    operation_id: str
    instance_id: str
    instance_ref: str
    instance_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    discovered_version: str
    input_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_ref: str | None = None
    connection_ref: str
    policy_ref: str
    destination_pins: tuple[str, ...]
    tenant_id: str
    project_id: str
    readiness: str
    auth_ready: bool
    maturity: Literal["GA", "PREVIEW", "UNKNOWN"]
    lifecycle: Literal["ACTIVE", "DEPRECATED", "RETIRED"]
    approval_expires_at: datetime | None = None


class ProviderContractAdapter(Protocol):
    contract_version: str
    contract_schema_digest: str
    trusted_legacy_derivation: bool

    def discover_instance(
        self,
        provider_id: str,
        instance_id: str,
    ) -> ProviderInstanceAttestation: ...

    def load_schema(self, schema_digest: str) -> dict[str, Any]: ...

    def resolve_handler(
        self,
        attestation: ProviderInstanceAttestation,
    ) -> CapabilityHandler: ...


def attach_provider_binding(
    binding: CapabilityBinding,
    adapter: ProviderContractAdapter,
    *,
    tenant_id: str,
    project_id: str,
    now: datetime | None = None,
) -> ToolRegistration:
    attestation = adapter.discover_instance(
        binding.provider_id,
        binding.instance_id,
    )
    expected = {
        "provider_id": binding.provider_id,
        "provider_contract_version": binding.provider_contract_version,
        "provider_contract_schema_digest": binding.provider_contract_schema_digest,
        "descriptor_id": binding.descriptor_id,
        "descriptor_version": binding.descriptor_version,
        "operation_id": binding.operation_id,
        "instance_id": binding.instance_id,
        "instance_ref": binding.instance_ref,
        "instance_fingerprint": binding.instance_fingerprint,
        "discovered_version": binding.pinned_provider_version,
        "input_schema_digest": binding.input_schema_digest,
        "output_schema_digest": binding.output_schema_digest,
        "config_digest": binding.config_digest,
        "config_ref": binding.config_ref,
        "connection_ref": binding.connection_ref,
        "policy_ref": binding.policy_ref,
        "destination_pins": binding.destination_pins,
    }
    actual = attestation.model_dump(
        include=set(expected),
    )
    mismatches = sorted(
        field
        for field, expected_value in expected.items()
        if actual[field] != expected_value
    )
    if (
        adapter.contract_version != binding.provider_contract_version
        or adapter.contract_schema_digest != binding.provider_contract_schema_digest
    ):
        mismatches.append("adapter_contract")
    if mismatches:
        if "instance_fingerprint" in mismatches:
            raise StaleCapabilityBindingError(
                "Provider instance fingerprint changed and requires rebind",
                context={
                    "capability": binding.capability_id,
                    "instance_ref": binding.instance_ref,
                    "expected_fingerprint": binding.instance_fingerprint,
                    "current_fingerprint": attestation.instance_fingerprint,
                },
            )
        raise ConfigurationError(
            "Provider discovery does not match the pinned capability binding",
            context={
                "capability": binding.capability_id,
                "mismatches": ",".join(sorted(set(mismatches))),
            },
        )
    if (
        binding.provider_contract_version.startswith("integration-provider.v1")
        and not adapter.trusted_legacy_derivation
    ):
        raise ConfigurationError(
            "Legacy provider instances are not attachable without trusted pin attestation",
            context={"capability": binding.capability_id},
        )
    for schema_digest in (
        binding.input_schema_digest,
        binding.output_schema_digest,
    ):
        if _canonical_digest(adapter.load_schema(schema_digest)) != schema_digest:
            raise ConfigurationError(
                "Provider schema content does not match its pinned digest",
                context={
                    "capability": binding.capability_id,
                    "schema_digest": schema_digest,
                },
            )
    if (
        attestation.tenant_id != binding.tenant_scope
        or attestation.project_id != binding.project_scope
        or binding.tenant_scope != tenant_id
        or binding.project_scope != project_id
    ):
        raise AuthorizationError(
            "Provider instance is outside the authorized tenant or project",
            context={"capability": binding.capability_id},
        )
    if not attestation.auth_ready:
        raise AuthorizationError(
            "Provider instance authentication is not ready",
            context={"capability": binding.capability_id},
        )
    if attestation.readiness != "available":
        raise ConfigurationError(
            "Provider instance is not available",
            context={
                "capability": binding.capability_id,
                "readiness": attestation.readiness,
            },
        )
    if attestation.maturity != "GA" or attestation.lifecycle != "ACTIVE":
        raise ConfigurationError(
            "Provider instance is not GA and active",
            context={
                "capability": binding.capability_id,
                "maturity": attestation.maturity,
                "lifecycle": attestation.lifecycle,
            },
        )
    current_time = now or datetime.now(UTC)
    if (
        attestation.approval_expires_at is not None
        and attestation.approval_expires_at <= current_time
    ):
        raise ApprovalRequiredError(
            "Provider instance approval expired",
            context={"capability": binding.capability_id},
        )
    handler = adapter.resolve_handler(attestation)
    return ToolRegistration(
        binding=binding,
        tool_name=binding.operation_id.rsplit(".", 1)[-1],
        handler=handler,
        current_instance_fingerprint=attestation.instance_fingerprint,
    )


class InvocationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    scopes: frozenset[str] = frozenset()
    destination: str | None = None
    approved_capabilities: frozenset[str] = frozenset()
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)
    operation_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    deadline_monotonic: float | None = None


class CapabilityPolicy:
    def authorize(self, capability: CapabilityDescriptor, context: InvocationContext) -> None:
        missing = capability.required_scopes - context.scopes
        if missing:
            raise AuthorizationError(
                "Principal lacks required capability scopes",
                context={"capability": capability.id, "missing_scopes": ",".join(sorted(missing))},
            )
        if capability.allowed_destinations and context.destination not in capability.allowed_destinations:
            raise DestinationDeniedError(
                "Capability destination is not allowlisted",
                context={"capability": capability.id},
            )
        if (
            capability.operation
            in {
                OperationClass.WRITE_REVERSIBLE,
                OperationClass.WRITE_IRREVERSIBLE,
                OperationClass.PRIVILEGED,
            }
            and context.destination not in capability.side_effect_destinations
        ):
            raise DestinationDeniedError(
                "Capability side-effect destination is not allowlisted",
                context={"capability": capability.id},
            )
        approval_required = capability.approval in {
            ApprovalMode.REQUIRED,
            ApprovalMode.ALWAYS,
        }
        if approval_required and capability.id not in context.approved_capabilities:
            raise ApprovalRequiredError(
                "Capability requires an out-of-model approval",
                context={"capability": capability.id},
            )
        if capability.idempotency == IdempotencyMode.REQUIRED and (
            not context.idempotency_key or not context.operation_fingerprint
        ):
            raise IdempotencyRequiredError(
                "Capability requires an idempotency key and canonical operation fingerprint",
                context={"capability": capability.id},
            )


class CapabilityRegistry:
    def __init__(self) -> None:
        self._descriptors: dict[str, CapabilityDescriptor] = {}
        self._tools: dict[str, ToolRegistration] = {}
        self._capability_tools: dict[str, list[str]] = {}

    def add_descriptor(self, descriptor: CapabilityDescriptor) -> None:
        if descriptor.id in self._descriptors:
            raise ValueError(f"Capability already registered: {descriptor.id}")
        self._descriptors[descriptor.id] = descriptor

    def register_tool(self, registration: ToolRegistration) -> None:
        capability_id = registration.binding.capability_id
        if capability_id not in self._descriptors:
            raise CapabilityNotFoundError(
                "Tool registration references an unknown capability",
                context={"capability": capability_id},
            )
        if registration.tool_name in self._tools:
            raise ValueError(f"Tool operation already registered: {registration.tool_name}")
        self._tools[registration.tool_name] = registration
        self._capability_tools.setdefault(capability_id, []).append(registration.tool_name)

    def resolve(
        self,
        capability_id: str,
    ) -> tuple[CapabilityDescriptor, ToolRegistration]:
        try:
            descriptor = self._descriptors[capability_id]
            tool_names = self._capability_tools[capability_id]
        except KeyError as exc:
            raise CapabilityNotFoundError(
                "Capability is not registered",
                context={"capability": capability_id},
            ) from exc
        if len(tool_names) != 1:
            raise CapabilityNotFoundError(
                "Capability has multiple operations; resolve an exact tool operation",
                context={"capability": capability_id},
            )
        registration = self._tools[tool_names[0]]
        self._validate_fingerprint(descriptor, registration)
        return descriptor, registration

    @staticmethod
    def _validate_fingerprint(
        descriptor: CapabilityDescriptor,
        registration: ToolRegistration,
    ) -> None:
        if registration.binding.instance_fingerprint != registration.current_instance_fingerprint:
            raise StaleCapabilityBindingError(
                "Capability binding targets changed instance configuration",
                context={
                    "capability": descriptor.id,
                    "instance_ref": registration.binding.instance_ref,
                    "expected_fingerprint": registration.binding.instance_fingerprint,
                    "current_fingerprint": registration.current_instance_fingerprint,
                },
            )

    def definitions(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._descriptors.values())

    def resolve_operation(
        self,
        tool_name: str,
    ) -> tuple[CapabilityDescriptor, ToolRegistration]:
        try:
            registration = self._tools[tool_name]
            descriptor = self._descriptors[registration.binding.capability_id]
        except KeyError as exc:
            raise CapabilityNotFoundError(
                "Tool operation is not registered",
                context={"operation": tool_name},
            ) from exc
        self._validate_fingerprint(descriptor, registration)
        return descriptor, registration


class CapabilityExecutor:
    def __init__(
        self,
        registry: CapabilityRegistry,
        *,
        policy: CapabilityPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        max_cached_results: int = 1024,
    ) -> None:
        if max_cached_results < 1:
            raise ValueError("max_cached_results must be at least 1")
        self._registry = registry
        self._policy = policy or CapabilityPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._max_cached_results = max_cached_results
        self._results: OrderedDict[tuple[str, str, str, str, str], dict[str, Any]] = OrderedDict()
        self._locks: WeakValueDictionary[tuple[str, str, str, str, str], asyncio.Lock] = WeakValueDictionary()

    async def invoke_operation(
        self,
        tool_name: str,
        payload: dict[str, Any],
        context: InvocationContext,
    ) -> dict[str, Any]:
        capability, registration = self._registry.resolve_operation(tool_name)
        return await self._invoke_resolved(
            capability,
            registration,
            payload,
            context,
        )

    async def invoke(
        self,
        capability_id: str,
        payload: dict[str, Any],
        context: InvocationContext,
    ) -> dict[str, Any]:
        capability, registration = self._registry.resolve(capability_id)
        return await self._invoke_resolved(
            capability,
            registration,
            payload,
            context,
        )

    async def _invoke_resolved(
        self,
        capability: CapabilityDescriptor,
        registration: ToolRegistration,
        payload: dict[str, Any],
        context: InvocationContext,
    ) -> dict[str, Any]:
        self._policy.authorize(capability, context)
        cache_key = self._cache_key(capability, context)
        if cache_key is None:
            return await self._attempt(
                capability,
                registration.handler,
                payload,
                context,
            )
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            if cache_key in self._results:
                self._results.move_to_end(cache_key)
                return self._results[cache_key]
            result = await self._attempt(
                capability,
                registration.handler,
                payload,
                context,
            )
            self._results[cache_key] = result
            while len(self._results) > self._max_cached_results:
                self._results.popitem(last=False)
            return result

    def _cache_key(
        self,
        capability: CapabilityDescriptor,
        context: InvocationContext,
    ) -> tuple[str, str, str, str, str] | None:
        if (
            capability.idempotency == IdempotencyMode.NONE
            or not context.idempotency_key
            or not context.operation_fingerprint
        ):
            return None
        return (
            context.tenant_id,
            context.principal_id,
            capability.id,
            context.idempotency_key,
            context.operation_fingerprint,
        )

    async def _attempt(
        self,
        capability: CapabilityDescriptor,
        handler: CapabilityHandler,
        payload: dict[str, Any],
        context: InvocationContext,
    ) -> dict[str, Any]:
        for attempt in range(capability.retry.max_attempts):
            timeout = self._remaining_timeout(capability, context)
            try:
                async with asyncio.timeout(timeout):
                    return await self._invoke_handler(handler, payload)
            except TimeoutError as exc:
                error: HarnessError = DeadlineExceededError(
                    "Capability invocation exceeded its deadline",
                    context={"capability": capability.id},
                )
                if attempt == capability.retry.max_attempts - 1:
                    raise error from exc
            except asyncio.CancelledError:
                raise
            except HarnessError as exc:
                error = exc
                if not exc.retryable or attempt == capability.retry.max_attempts - 1:
                    raise
            except Exception as exc:
                raise InvocationError(
                    "Capability handler failed",
                    context={"capability": capability.id, "exception": type(exc).__name__},
                ) from exc
            delay = capability.retry.delays_seconds[attempt]
            if context.deadline_monotonic is not None and self._monotonic() + delay >= context.deadline_monotonic:
                raise DeadlineExceededError(
                    "Capability retry would exceed the invocation deadline",
                    context={"capability": capability.id},
                )
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover - RetryPolicy guarantees an attempt.

    @staticmethod
    async def _invoke_handler(
        handler: CapabilityHandler,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if inspect.iscoroutinefunction(handler):
            return cast(dict[str, Any], await handler(payload))
        value = await asyncio.to_thread(handler, payload)
        return await value if inspect.isawaitable(value) else value

    def _remaining_timeout(
        self,
        capability: CapabilityDescriptor,
        context: InvocationContext,
    ) -> float:
        if context.deadline_monotonic is None:
            return capability.timeout_seconds
        remaining = context.deadline_monotonic - self._monotonic()
        if remaining <= 0:
            raise DeadlineExceededError("Invocation deadline has expired")
        return min(capability.timeout_seconds, remaining)
