from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, cast
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .approvals import (
    ApprovalConsumptionAdapter,
    ApprovalConsumptionDisposition,
    ApprovalConsumptionRequest,
    ApprovalConsumptionResult,
    ApprovalReceipt,
)
from .errors import (
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
    DeadlineExceededError,
    DestinationDeniedError,
    HarnessError,
    IdempotencyInProgressError,
    IdempotencyReconciliationRequiredError,
    IdempotencyReplayDeniedError,
    IdempotencyRequiredError,
    IdempotencyResultMismatchError,
    IdempotencyStoreUnavailableError,
    InvocationError,
    IsolationError,
    StaleCapabilityBindingError,
)
from .idempotency import (
    ClaimDisposition,
    CompletedReplayMode,
    IdempotencyApprovalProvenance,
    IdempotencyClaim,
    IdempotencyKey,
    IdempotencyPolicy,
    IdempotencyRecord,
    IdempotencyState,
    IdempotencyStore,
    canonical_idempotency_digest,
)

CapabilityHandler = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]
CapabilityHandlerResolver = Callable[
    ["ProviderInstanceAttestation"],
    CapabilityHandler,
]
_PROVIDER_ATTESTATION = object()
_RUNTIME_ATTESTATION = object()
PROVIDER_CONTRACT_VERSION = "research-assistant.integration-provider.v6"
PROVIDER_CONTRACT_SCHEMA_DIGEST = (
    "354716da381fbb0d71ee58fbfccbc737066debaf238403964f28112898cdb24c"
)
PROVIDER_CONTRACT_ARTIFACT_DIGEST = PROVIDER_CONTRACT_SCHEMA_DIGEST


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
    idempotency_policy: IdempotencyPolicy = IdempotencyPolicy()
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
            if self.idempotency_policy.lease_seconds < self.timeout_seconds:
                raise ValueError("durable idempotency lease must cover the handler timeout")
            if (
                self.operation == OperationClass.WRITE_IRREVERSIBLE
                and self.idempotency_policy.completed_replay != CompletedReplayMode.DENY
            ):
                raise ValueError("irreversible capabilities cannot replay completed operations")
        return self


class DescriptorReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class OperationReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,127}$")
    version: str = Field(min_length=1, max_length=128)
    input_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class InstanceReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    instance_id: str = Field(min_length=1, max_length=256)
    provider_resource_id: str = Field(min_length=1, max_length=2048)
    discovered_provider_version: str = Field(min_length=1, max_length=128)
    discovered_resource_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
    )
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConfigurationReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str | None = Field(default=None, min_length=1, max_length=512)
    canonical_json: str = Field(min_length=2)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def digest_matches_canonical_configuration(self) -> ConfigurationReference:
        try:
            configuration = json.loads(self.canonical_json)
        except json.JSONDecodeError as exc:
            raise ValueError("capability configuration must be canonical JSON") from exc
        if self.canonical_json != _canonical_json(configuration):
            raise ValueError("capability configuration JSON is not canonical")
        if self.digest != _canonical_digest(configuration):
            raise ValueError("capability configuration digest does not match")
        return self


class ConnectionReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=512)
    auth_mode: str = Field(min_length=1, max_length=128)
    scopes: tuple[str, ...]
    authorization_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def authorization_is_canonical(self) -> ConnectionReference:
        if self.scopes != tuple(sorted(set(self.scopes))) or any(
            not scope for scope in self.scopes
        ):
            raise ValueError("connection scopes must be non-empty, sorted, and unique")
        expected = _canonical_digest(
            {
                "id": self.id,
                "auth_mode": self.auth_mode,
                "scopes": self.scopes,
            }
        )
        if self.authorization_digest != expected:
            raise ValueError("connection authorization digest does not match")
        return self


class PolicyReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=128)
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DestinationConstraints(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    constraints: tuple[str, ...]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def digest_matches_constraints(self) -> DestinationConstraints:
        if self.digest != _canonical_digest(self.constraints):
            raise ValueError("allowed destination digest does not match its constraints")
        return self


class CapabilityBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,127}$")
    provider_contract_version: str = Field(min_length=1, max_length=128)
    provider_contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    descriptor_ref: DescriptorReference
    operations_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_ref: OperationReference
    instance_ref: InstanceReference
    configuration_ref: ConfigurationReference
    connection_ref: ConnectionReference
    policy_ref: PolicyReference
    allowed_destinations: DestinationConstraints
    tenant_scope: str | None = Field(default=None, min_length=1, max_length=256)
    project_scope: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def provider_contract_is_exact(self) -> CapabilityBinding:
        if self.provider_contract_version != PROVIDER_CONTRACT_VERSION:
            raise ValueError("capability binding requires the canonical provider v6 contract")
        if self.provider_contract_schema_digest != PROVIDER_CONTRACT_SCHEMA_DIGEST:
            raise ValueError("capability binding provider schema digest does not match v6")
        if (self.tenant_scope is None) != (self.project_scope is None):
            raise ValueError("capability binding tenant and project scopes must be supplied together")
        return self


def template_instance_fingerprint(binding: CapabilityBinding) -> str:
    payload = binding.model_dump(mode="json")
    payload["instance_ref"].pop("fingerprint")
    return _canonical_digest(payload)


def _canonical_digest(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class ToolRegistration:
    binding: CapabilityBinding
    tool_name: str
    handler: CapabilityHandler
    current_instance_fingerprint: str
    _attestation: object | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(self.current_instance_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in self.current_instance_fingerprint
        ):
            raise ValueError("Current capability instance fingerprint must be lowercase SHA-256")

    @property
    def runtime_attested(self) -> bool:
        return self._attestation is _RUNTIME_ATTESTATION


class ProviderInstanceAttestation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{1,127}$")
    provider_contract_version: str
    provider_contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    descriptor_ref: DescriptorReference
    operations_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_ref: OperationReference
    instance_ref: InstanceReference
    configuration_ref: ConfigurationReference
    connection_ref: ConnectionReference
    policy_ref: PolicyReference
    allowed_destinations: DestinationConstraints
    tenant_id: str
    project_id: str
    readiness: Literal[
        "READY",
        "UNAVAILABLE",
        "UNAUTHORIZED",
        "NEEDS_CONSENT",
        "MISCONFIGURED",
        "DEGRADED",
    ]
    health: Literal[
        "READY",
        "UNAVAILABLE",
        "UNAUTHORIZED",
        "NEEDS_CONSENT",
        "MISCONFIGURED",
        "DEGRADED",
    ]
    auth_ready: bool
    configuration_validated: bool
    maturity: Literal["GA", "PREVIEW", "UNKNOWN"]
    lifecycle: Literal["ACTIVE", "DEPRECATED", "RETIRED"]
    approval_expires_at: datetime | None = None


class ProviderContractAdapter(Protocol):
    contract_version: str
    contract_schema_digest: str

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
    handler_resolver: CapabilityHandlerResolver | None = None,
) -> ToolRegistration:
    try:
        binding = CapabilityBinding.model_validate(binding.model_dump(mode="json"))
    except ValidationError as exc:
        raise ConfigurationError(
            "Capability binding is not a valid canonical provider v6 binding",
        ) from exc
    if (
        adapter.contract_version != PROVIDER_CONTRACT_VERSION
        or adapter.contract_schema_digest != PROVIDER_CONTRACT_ARTIFACT_DIGEST
    ):
        raise ConfigurationError(
            "Provider adapter does not match the pinned provider v6 artifact",
            context={
                "expected_contract": PROVIDER_CONTRACT_VERSION,
                "expected_artifact_digest": PROVIDER_CONTRACT_ARTIFACT_DIGEST,
            },
        )
    attestation = adapter.discover_instance(
        binding.instance_ref.provider_id,
        binding.instance_ref.instance_id,
    )
    expected = {
        "binding_id": binding.binding_id,
        "provider_contract_version": binding.provider_contract_version,
        "provider_contract_schema_digest": binding.provider_contract_schema_digest,
        "instance_ref": binding.instance_ref,
        "descriptor_ref": binding.descriptor_ref,
        "operations_digest": binding.operations_digest,
        "operation_ref": binding.operation_ref,
        "configuration_ref": binding.configuration_ref,
        "connection_ref": binding.connection_ref,
        "policy_ref": binding.policy_ref,
        "allowed_destinations": binding.allowed_destinations,
    }
    mismatches = sorted(
        field for field, expected_value in expected.items() if getattr(attestation, field) != expected_value
    )
    if mismatches:
        if "instance_ref" in mismatches:
            raise StaleCapabilityBindingError(
                "Provider instance fingerprint changed and requires rebind",
                context={
                    "capability": binding.descriptor_ref.id,
                    "instance_ref": binding.instance_ref.instance_id,
                    "expected_fingerprint": binding.instance_ref.fingerprint,
                    "current_fingerprint": attestation.instance_ref.fingerprint,
                },
            )
        raise ConfigurationError(
            "Provider discovery does not match the pinned capability binding",
            context={
                "capability": binding.descriptor_ref.id,
                "mismatches": ",".join(sorted(set(mismatches))),
            },
        )
    for schema_digest in (
        binding.operation_ref.input_schema_digest,
        binding.operation_ref.output_schema_digest,
    ):
        if _canonical_digest(adapter.load_schema(schema_digest)) != schema_digest:
            raise ConfigurationError(
                "Provider schema content does not match its pinned digest",
                context={
                    "capability": binding.descriptor_ref.id,
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
            context={"capability": binding.descriptor_ref.id},
        )
    if not attestation.auth_ready:
        raise AuthorizationError(
            "Provider instance authentication is not ready",
            context={"capability": binding.descriptor_ref.id},
        )
    if attestation.readiness != "READY" or attestation.health != "READY":
        raise ConfigurationError(
            "Provider instance is not ready and healthy",
            context={
                "capability": binding.descriptor_ref.id,
                "readiness": attestation.readiness,
                "health": attestation.health,
            },
        )
    if not attestation.configuration_validated:
        raise ConfigurationError(
            "Provider instance configuration is not validated",
            context={"capability": binding.descriptor_ref.id},
        )
    if attestation.maturity != "GA" or attestation.lifecycle != "ACTIVE":
        raise ConfigurationError(
            "Provider instance is not GA and active",
            context={
                "capability": binding.descriptor_ref.id,
                "maturity": attestation.maturity,
                "lifecycle": attestation.lifecycle,
            },
        )
    current_time = now or datetime.now(UTC)
    if attestation.approval_expires_at is not None and attestation.approval_expires_at <= current_time:
        raise ApprovalRequiredError(
            "Provider instance approval expired",
            context={"capability": binding.descriptor_ref.id},
        )
    handler = handler_resolver(attestation) if handler_resolver is not None else adapter.resolve_handler(attestation)
    return ToolRegistration(
        binding=binding,
        tool_name=binding.operation_ref.id.rsplit(".", 1)[-1],
        handler=handler,
        current_instance_fingerprint=attestation.instance_ref.fingerprint,
        _attestation=_PROVIDER_ATTESTATION,
    )


def runtime_attested_registration(
    binding: CapabilityBinding,
    adapter: ProviderContractAdapter,
    *,
    tenant_id: str,
    project_id: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    handler_resolver: CapabilityHandlerResolver | None = None,
) -> ToolRegistration:
    initial = attach_provider_binding(
        binding,
        adapter,
        tenant_id=tenant_id,
        project_id=project_id,
        now=clock(),
        handler_resolver=handler_resolver,
    )

    async def invoke(payload: dict[str, Any]) -> dict[str, Any]:
        current = attach_provider_binding(
            binding,
            adapter,
            tenant_id=tenant_id,
            project_id=project_id,
            now=clock(),
            handler_resolver=handler_resolver,
        )
        outcome = current.handler(payload)
        if inspect.isawaitable(outcome):
            return await outcome
        return outcome

    return ToolRegistration(
        binding=binding,
        tool_name=initial.tool_name,
        handler=invoke,
        current_instance_fingerprint=initial.current_instance_fingerprint,
        _attestation=_RUNTIME_ATTESTATION,
    )


class InvocationContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    scopes: frozenset[str] = frozenset()
    destination: str | None = None
    approval_decision_id: str | None = Field(default=None, min_length=1, max_length=512)
    invocation_id: str | None = Field(default=None, min_length=1, max_length=512)
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
        if approval_required and (
            context.approval_decision_id is None
            or context.invocation_id is None
            or context.destination is None
        ):
            raise ApprovalRequiredError(
                "Capability requires an exact out-of-model approval reference",
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
        capability_id = registration.binding.descriptor_ref.id
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
        if registration.binding.instance_ref.fingerprint != registration.current_instance_fingerprint:
            raise StaleCapabilityBindingError(
                "Capability binding targets changed instance configuration",
                context={
                    "capability": descriptor.id,
                    "instance_ref": registration.binding.instance_ref.instance_id,
                    "expected_fingerprint": registration.binding.instance_ref.fingerprint,
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
            descriptor = self._descriptors[registration.binding.descriptor_ref.id]
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
        idempotency_store: IdempotencyStore | None = None,
        approval_adapter: ApprovalConsumptionAdapter | None = None,
        release_id: str | None = None,
        allow_test_idempotency_store: bool = False,
        allow_test_approval_adapter: bool = False,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if max_cached_results < 1:
            raise ValueError("max_cached_results must be at least 1")
        if release_id is not None and (
            len(release_id) != 71
            or not release_id.startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in release_id[7:])
        ):
            raise ValueError("release_id must be a canonical SHA-256 release identity")
        self._registry = registry
        self._policy = policy or CapabilityPolicy()
        self._sleep = sleep
        self._monotonic = monotonic
        self._max_cached_results = max_cached_results
        self._idempotency_store = idempotency_store
        self._approval_adapter = approval_adapter
        self._release_id = release_id
        self._allow_test_idempotency_store = allow_test_idempotency_store
        self._allow_test_approval_adapter = allow_test_approval_adapter
        self._utcnow = utcnow
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
        if (
            context.tenant_id != registration.binding.tenant_scope
            or context.project_id != registration.binding.project_scope
        ):
            raise IsolationError(
                "Authenticated invocation scope does not match the capability binding",
                context={"capability": capability.id},
            )
        self._policy.authorize(capability, context)
        if self._has_external_effect(capability) or capability.approval != ApprovalMode.NEVER:
            return await self._invoke_durable(
                capability,
                registration,
                payload,
                context,
            )
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

    @staticmethod
    def _has_external_effect(capability: CapabilityDescriptor) -> bool:
        return capability.operation in {
            OperationClass.WRITE_REVERSIBLE,
            OperationClass.WRITE_IRREVERSIBLE,
            OperationClass.PRIVILEGED,
        }

    async def _invoke_durable(
        self,
        capability: CapabilityDescriptor,
        registration: ToolRegistration,
        payload: dict[str, Any],
        context: InvocationContext,
    ) -> dict[str, Any]:
        store = self._idempotency_store
        if store is None or (
            not getattr(store, "is_durable", False) and not self._allow_test_idempotency_store
        ):
            raise IdempotencyStoreUnavailableError(
                "Externally consequential capabilities require an app-owned durable idempotency store",
                context={"capability": capability.id},
            )
        approval_adapter = self._approval_adapter
        if approval_adapter is None or (
            not getattr(approval_adapter, "is_durable", False)
            and not self._allow_test_approval_adapter
        ):
            raise ApprovalStoreUnavailableError(
                "Approval-gated capabilities require an app-owned durable approval adapter",
                context={"capability": capability.id},
            )
        if self._release_id is None:
            raise IdempotencyStoreUnavailableError(
                "Durable idempotency requires immutable release provenance",
                context={"capability": capability.id},
            )
        caller_key = cast(str, context.idempotency_key)
        argument_hash = cast(str, context.operation_fingerprint)
        destination = cast(str, context.destination)
        key = IdempotencyKey(
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            binding_digest=_canonical_digest(registration.binding.model_dump(mode="json")),
            operation_id=registration.binding.operation_ref.id,
            destination=destination,
            caller_key=caller_key,
            argument_hash=argument_hash,
        )
        try:
            claim = await store.claim(
                key,
                actor_id=context.principal_id,
                release_id=self._release_id,
                lease_seconds=capability.idempotency_policy.lease_seconds,
            )
        except HarnessError:
            raise
        except Exception as exc:
            raise IdempotencyStoreUnavailableError(
                "Durable idempotency claim failed closed",
                context={"capability": capability.id},
            ) from exc
        self._validate_claim(capability, key, claim, context)
        if claim.disposition == ClaimDisposition.IN_PROGRESS:
            raise IdempotencyInProgressError(
                "An equivalent external operation is already in progress",
                context={
                    "capability": capability.id,
                    "lease_expires_at": claim.record.lease_expires_at.isoformat(),
                },
            )
        if claim.disposition == ClaimDisposition.RECONCILIATION_REQUIRED:
            raise IdempotencyReconciliationRequiredError(
                "Prior external operation state requires deterministic reconciliation",
                context={
                    "capability": capability.id,
                    "state": claim.record.state,
                },
            )
        if claim.disposition == ClaimDisposition.COMPLETED:
            return await self._replay_completed(capability, store, claim.record)
        claim_token = cast(str, claim.claim_token)
        try:
            approval = await self._consume_approval(
                approval_adapter,
                capability,
                registration,
                context,
                key,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._fail_durable(
                    store,
                    claim.record,
                    claim_token,
                    "approval_consumption_cancelled",
                    suppress_store_error=True,
                )
            )
            raise
        except HarnessError as exc:
            await self._fail_durable(store, claim.record, claim_token, exc.code)
            raise
        try:
            started = await store.mark_in_progress(
                key,
                claim_token=claim_token,
                expected_version=claim.record.version,
                irreversible=capability.operation == OperationClass.WRITE_IRREVERSIBLE,
                approval=approval,
            )
            self._validate_transition(
                capability,
                claim.record,
                started,
                expected_state="in_progress",
                irreversible=capability.operation == OperationClass.WRITE_IRREVERSIBLE,
                approval=approval,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._fail_durable(
                    store,
                    claim.record,
                    claim_token,
                    "start_transition_cancelled",
                    suppress_store_error=True,
                )
            )
            raise
        except HarnessError as exc:
            try:
                await self._fail_durable(
                    store,
                    claim.record,
                    claim_token,
                    exc.code,
                )
            except IdempotencyReconciliationRequiredError as reconciliation:
                raise reconciliation from exc
            raise
        except Exception as exc:
            start_error = IdempotencyStoreUnavailableError(
                "Durable idempotency start transition failed closed",
                context={"capability": capability.id},
            )
            try:
                await self._fail_durable(
                    store,
                    claim.record,
                    claim_token,
                    start_error.code,
                )
            except IdempotencyReconciliationRequiredError as reconciliation:
                raise reconciliation from start_error
            raise start_error from exc
        try:
            timeout = self._remaining_timeout(capability, context)
            async with asyncio.timeout(timeout):
                result = await self._invoke_handler(registration.handler, payload)
        except TimeoutError as exc:
            timeout_error = DeadlineExceededError(
                "Capability invocation exceeded its deadline",
                context={"capability": capability.id},
            )
            await self._fail_durable(store, started, claim_token, timeout_error.code)
            raise timeout_error from exc
        except asyncio.CancelledError:
            await asyncio.shield(
                self._fail_durable(
                    store,
                    started,
                    claim_token,
                    "cancelled",
                    suppress_store_error=True,
                )
            )
            raise
        except HarnessError as exc:
            await self._fail_durable(store, started, claim_token, exc.code)
            raise
        except Exception as exc:
            invocation_error = InvocationError(
                "Capability handler failed",
                context={"capability": capability.id, "exception": type(exc).__name__},
            )
            await self._fail_durable(store, started, claim_token, invocation_error.code)
            raise invocation_error from exc
        result_hash = canonical_idempotency_digest(result)
        try:
            completed = await store.complete(
                key,
                claim_token=claim_token,
                expected_version=started.version,
                result=result,
                result_hash=result_hash,
            )
        except Exception as exc:
            raise IdempotencyReconciliationRequiredError(
                "External operation completed but durable result commit is uncertain",
                context={"capability": capability.id},
            ) from exc
        self._validate_transition(
            capability,
            started,
            completed,
            expected_state="completed",
        )
        if completed.result_hash != result_hash:
            raise IdempotencyResultMismatchError(
                "Durable completion record does not match the handler result",
                context={"capability": capability.id},
            )
        return result

    def _validate_claim(
        self,
        capability: CapabilityDescriptor,
        key: IdempotencyKey,
        claim: IdempotencyClaim,
        context: InvocationContext,
    ) -> None:
        if not isinstance(claim, IdempotencyClaim) or not isinstance(
            claim.disposition,
            ClaimDisposition,
        ):
            raise IdempotencyReconciliationRequiredError(
                "Durable store returned an invalid claim contract",
                context={"capability": capability.id},
            )
        record = claim.record
        if not isinstance(record, IdempotencyRecord):
            raise IdempotencyReconciliationRequiredError(
                "Durable store returned an invalid record contract",
                context={"capability": capability.id},
            )
        if record.key != key:
            raise IdempotencyReconciliationRequiredError(
                "Durable claim returned a record for another operation",
                context={"capability": capability.id},
            )
        expected_states = {
            ClaimDisposition.ACQUIRED: {"claimed"},
            ClaimDisposition.IN_PROGRESS: {"claimed", "in_progress"},
            ClaimDisposition.COMPLETED: {"completed"},
            ClaimDisposition.RECONCILIATION_REQUIRED: {"claimed", "in_progress", "failed"},
        }
        if record.state not in expected_states[claim.disposition]:
            raise IdempotencyReconciliationRequiredError(
                "Durable claim disposition does not match its persisted state",
                context={"capability": capability.id},
            )
        if not self._state_is_consistent(
            record,
            irreversible=capability.operation == OperationClass.WRITE_IRREVERSIBLE,
        ):
            raise IdempotencyReconciliationRequiredError(
                "Durable claim record violates its state invariants",
                context={"capability": capability.id},
            )
        active_lease = self._lease_is_active(record)
        if claim.disposition in {ClaimDisposition.ACQUIRED, ClaimDisposition.IN_PROGRESS} and not active_lease:
            raise IdempotencyReconciliationRequiredError(
                "Durable claim lease expired before execution",
                context={"capability": capability.id},
            )
        if (
            claim.disposition == ClaimDisposition.RECONCILIATION_REQUIRED
            and record.state != "failed"
            and active_lease
        ):
            raise IdempotencyReconciliationRequiredError(
                "Durable reconciliation disposition is inconsistent with its lease",
                context={"capability": capability.id},
            )
        if claim.disposition == ClaimDisposition.ACQUIRED and (
            record.actor_id != context.principal_id or record.release_id != self._release_id
        ):
            raise IdempotencyReconciliationRequiredError(
                "Durable claim provenance does not match the current actor and release",
                context={"capability": capability.id},
            )
        if claim.disposition == ClaimDisposition.ACQUIRED:
            if (
                claim.claim_token is None
                or hashlib.sha256(claim.claim_token.encode("utf-8")).hexdigest()
                != record.claim_token_hash
            ):
                raise IdempotencyReconciliationRequiredError(
                    "Durable claim ownership token does not match the persisted record",
                    context={"capability": capability.id},
                )
        elif claim.claim_token is not None:
            raise IdempotencyReconciliationRequiredError(
                "Non-acquired durable claim returned an ownership token",
                context={"capability": capability.id},
            )

    def _validate_transition(
        self,
        capability: CapabilityDescriptor,
        previous: IdempotencyRecord,
        current: IdempotencyRecord,
        *,
        expected_state: str,
        irreversible: bool | None = None,
        approval: IdempotencyApprovalProvenance | None = None,
    ) -> None:
        if not isinstance(previous, IdempotencyRecord) or not isinstance(
            current,
            IdempotencyRecord,
        ):
            raise IdempotencyReconciliationRequiredError(
                "Durable store returned an invalid transition contract",
                context={"capability": capability.id},
            )
        if (
            current.key != previous.key
            or current.state != expected_state
            or current.version == previous.version
            or current.claim_token_hash != previous.claim_token_hash
            or current.actor_id != previous.actor_id
            or current.release_id != previous.release_id
            or current.claimed_at != previous.claimed_at
            or (
                expected_state != "in_progress"
                and current.approval != previous.approval
            )
        ):
            raise IdempotencyReconciliationRequiredError(
                "Durable idempotency transition failed provenance validation",
                context={"capability": capability.id},
            )
        if expected_state == "in_progress" and (
            current.started_at is None
            or not self._lease_is_active(current)
            or current.lease_expires_at != previous.lease_expires_at
            or current.irreversible_started != irreversible
            or current.reconciliation_required
            or current.completed_at is not None
            or current.result_hash is not None
            or current.result_ref is not None
            or current.failure_code is not None
            or current.approval != approval
            or previous.approval is not None
        ):
            raise IdempotencyReconciliationRequiredError(
                "Durable start transition is not safe to execute",
                context={"capability": capability.id},
            )
        if expected_state == "completed" and (
            current.started_at != previous.started_at
            or current.irreversible_started != previous.irreversible_started
            or current.completed_at is None
            or current.result_hash is None
            or current.result_ref is None
            or current.failure_code is not None
            or current.reconciliation_required
        ):
            raise IdempotencyReconciliationRequiredError(
                "Durable completion transition is missing execution provenance",
                context={"capability": capability.id},
            )

    def _lease_is_active(self, record: IdempotencyRecord) -> bool:
        try:
            return record.lease_expires_at > self._utcnow()
        except TypeError:
            return False

    @staticmethod
    def _state_is_consistent(
        record: IdempotencyRecord,
        *,
        irreversible: bool,
    ) -> bool:
        if record.state == IdempotencyState.CLAIMED:
            return (
                record.started_at is None
                and record.completed_at is None
                and not record.irreversible_started
                and record.result_hash is None
                and record.result_ref is None
                and record.failure_code is None
                and not record.reconciliation_required
                and record.approval is None
            )
        if record.state == IdempotencyState.IN_PROGRESS:
            return (
                record.started_at is not None
                and record.completed_at is None
                and record.irreversible_started == irreversible
                and record.result_hash is None
                and record.result_ref is None
                and record.failure_code is None
                and not record.reconciliation_required
                and record.approval is not None
            )
        if record.state == IdempotencyState.COMPLETED:
            return (
                record.started_at is not None
                and record.completed_at is not None
                and record.result_hash is not None
                and record.result_ref is not None
                and record.failure_code is None
                and not record.reconciliation_required
                and record.approval is not None
            )
        return (
            record.state == IdempotencyState.FAILED
            and record.failure_code is not None
            and record.completed_at is None
            and record.result_hash is None
            and record.result_ref is None
            and record.reconciliation_required
        )

    async def _consume_approval(
        self,
        adapter: ApprovalConsumptionAdapter,
        capability: CapabilityDescriptor,
        registration: ToolRegistration,
        context: InvocationContext,
        key: IdempotencyKey,
    ) -> IdempotencyApprovalProvenance:
        request = ApprovalConsumptionRequest(
            approval_decision_id=cast(str, context.approval_decision_id),
            binding_id=registration.binding.binding_id,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            actor_id=context.principal_id,
            scopes=tuple(sorted(context.scopes)),
            binding_digest=key.binding_digest,
            instance_fingerprint=registration.binding.instance_ref.fingerprint,
            operation_id=registration.binding.operation_ref.id,
            operation_version=registration.binding.operation_ref.version,
            argument_hash=key.argument_hash,
            destination=key.destination,
            policy_id=registration.binding.policy_ref.id,
            policy_version=registration.binding.policy_ref.version,
            policy_digest=registration.binding.policy_ref.digest,
            release_id=cast(str, self._release_id),
            invocation_id=cast(str, context.invocation_id),
            idempotency_key_digest=key.digest,
        )
        try:
            timeout = self._remaining_timeout(capability, context)
            async with asyncio.timeout(timeout):
                result = await adapter.consume(request)
        except TimeoutError as exc:
            raise ApprovalConsumptionUncertainError(
                "Approval consumption outcome is uncertain",
                context={"capability": capability.id},
            ) from exc
        except asyncio.CancelledError:
            raise
        except HarnessError:
            raise
        except Exception as exc:
            raise ApprovalStoreUnavailableError(
                "Durable approval consumption failed closed",
                context={"capability": capability.id},
            ) from exc
        receipt = self._validate_approval_result(capability, request, result)
        return IdempotencyApprovalProvenance(
            approval_decision_id=receipt.approval_decision_id,
            request_digest=receipt.request_digest,
            receipt_digest=receipt.digest,
            approval_version=receipt.approval_version,
            consumption_id=receipt.consumption_id,
            consumption_version=receipt.consumption_version,
            approver_id=receipt.approver_id,
            consumed_at=receipt.consumed_at,
        )

    def _validate_approval_result(
        self,
        capability: CapabilityDescriptor,
        request: ApprovalConsumptionRequest,
        result: ApprovalConsumptionResult,
    ) -> ApprovalReceipt:
        if not isinstance(result, ApprovalConsumptionResult):
            raise ApprovalResultInvalidError(
                "Approval adapter returned an invalid result contract",
                context={"capability": capability.id},
            )
        try:
            validated = ApprovalConsumptionResult.model_validate(
                result.model_dump(mode="json")
            )
        except (AttributeError, ValidationError) as exc:
            raise ApprovalResultInvalidError(
                "Approval adapter returned an invalid result contract",
                context={"capability": capability.id},
            ) from exc
        result = validated
        if (
            result.approval_decision_id != request.approval_decision_id
            or result.request_digest != request.digest
        ):
            raise ApprovalResultInvalidError(
                "Approval adapter returned an invalid result contract",
                context={"capability": capability.id},
            )
        if result.disposition != ApprovalConsumptionDisposition.CONSUMED:
            errors: dict[ApprovalConsumptionDisposition, type[HarnessError]] = {
                ApprovalConsumptionDisposition.DENIED: ApprovalDeniedError,
                ApprovalConsumptionDisposition.EXPIRED: ApprovalExpiredError,
                ApprovalConsumptionDisposition.NOT_FOUND: ApprovalRequiredError,
                ApprovalConsumptionDisposition.MISMATCH: ApprovalMismatchError,
                ApprovalConsumptionDisposition.ALREADY_CONSUMED: ApprovalAlreadyConsumedError,
                ApprovalConsumptionDisposition.REVOKED: ApprovalRevokedError,
            }
            raise errors[result.disposition](
                "Approval was not consumable for the exact capability invocation",
                context={
                    "capability": capability.id,
                    "reason": cast(str, result.reason_code),
                },
            )
        receipt = result.receipt
        if (
            not isinstance(receipt, ApprovalReceipt)
            or receipt.approval_decision_id != request.approval_decision_id
            or receipt.request_digest != request.digest
            or receipt.approval_version != result.approval_version
            or not receipt.one_time
            or receipt.consumed_at > self._utcnow()
            or receipt.expires_at <= self._utcnow()
        ):
            raise ApprovalResultInvalidError(
                "Approval receipt failed exact-binding validation",
                context={"capability": capability.id},
            )
        return receipt

    async def _replay_completed(
        self,
        capability: CapabilityDescriptor,
        store: IdempotencyStore,
        record: IdempotencyRecord,
    ) -> dict[str, Any]:
        if record.irreversible_started:
            raise IdempotencyReconciliationRequiredError(
                "Persisted irreversible operations cannot be replayed",
                context={"capability": capability.id},
            )
        result_ref = cast(str, record.result_ref)
        result_hash = cast(str, record.result_hash)
        replay = capability.idempotency_policy.completed_replay
        if replay == CompletedReplayMode.DENY:
            raise IdempotencyReplayDeniedError(
                "Completed external operation replay is denied by policy",
                context={
                    "capability": capability.id,
                    "result_ref": result_ref,
                },
            )
        if replay == CompletedReplayMode.RETURN_REFERENCE:
            return {
                "idempotency": {
                    "result_ref": result_ref,
                    "result_hash": result_hash,
                }
            }
        try:
            result = await store.load_result(result_ref)
        except HarnessError:
            raise
        except Exception as exc:
            raise IdempotencyStoreUnavailableError(
                "Durable idempotency result lookup failed closed",
                context={"capability": capability.id},
            ) from exc
        if result is None or canonical_idempotency_digest(result) != result_hash:
            raise IdempotencyResultMismatchError(
                "Replayed result does not match durable result provenance",
                context={"capability": capability.id},
            )
        return result

    @staticmethod
    async def _fail_durable(
        store: IdempotencyStore,
        started: IdempotencyRecord,
        claim_token: str,
        failure_code: str,
        *,
        suppress_store_error: bool = False,
    ) -> None:
        try:
            failed = await store.fail(
                started.key,
                claim_token=claim_token,
                expected_version=started.version,
                failure_code=failure_code,
            )
            if not isinstance(failed, IdempotencyRecord) or (
                failed.key != started.key
                or failed.state != "failed"
                or failed.version == started.version
                or failed.claim_token_hash != started.claim_token_hash
                or failed.actor_id != started.actor_id
                or failed.release_id != started.release_id
                or failed.claimed_at != started.claimed_at
                or failed.started_at != started.started_at
                or failed.irreversible_started != started.irreversible_started
                or failed.completed_at is not None
                or failed.result_hash is not None
                or failed.result_ref is not None
                or failed.failure_code != failure_code
                or not failed.reconciliation_required
                or failed.approval != started.approval
            ):
                raise IdempotencyReconciliationRequiredError(
                    "Durable failure transition failed provenance validation"
                )
        except Exception as exc:
            if suppress_store_error:
                return
            raise IdempotencyReconciliationRequiredError(
                "External operation failure could not be durably recorded"
            ) from exc

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
