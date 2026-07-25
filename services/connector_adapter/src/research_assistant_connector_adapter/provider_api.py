"""Governed HTTP boundary for operational capability providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from research_assistant_connectors.providers import (
    AsyncProviderRegistry,
    CapabilityDescriptor,
    CapabilityInstance,
    DiscoveryWarning,
    HealthReport,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    OperationDescriptor,
    PolicyError,
    ProviderDescriptor,
    ProviderError,
    ProviderValidationError,
    StaleBindingError,
    UnavailableError,
    ValidationReport,
    allowed_destinations_ref,
    authorization_digest,
    bindability_decisions,
    capability_instance_fingerprint,
)
from research_assistant_connectors.providers import (
    CapabilityBinding as RuntimeCapabilityBinding,
)
from research_assistant_connectors.providers.contracts import (
    CANONICALIZATION_VERSION,
    PROVIDER_CONTRACT_VERSION,
    Provider,
    plain_json,
)

ProviderContextFactory = Callable[[str, Request], InvocationContext]
ProviderBindingResolver = Callable[[str, str, Request], RuntimeCapabilityBinding]
CAPABILITY_BINDING_SCHEMA_ID = (
    "urn:research-assistant:schema:integration-provider:v7:capability-binding"
)


class GovernedModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderInvokePayload(GovernedModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "instance_id": "provider.instance",
                    "operation_id": "search",
                    "arguments": {"query": "governed research"},
                    "idempotency_key": "request-001",
                }
            ]
        },
    )

    instance_id: str = Field(min_length=1, max_length=512)
    binding_id: str | None = Field(default=None, min_length=1, max_length=512)
    operation_id: str = Field(min_length=1, max_length=512)
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        pattern=r"^[!-~]+$",
    )


class ProvenanceResponse(GovernedModel):
    official_url: str
    source_version: str
    last_verified_at: str
    retirement_date: str | None = None


class OperationDescriptorResponse(GovernedModel):
    operation_id: str
    operation_version: str
    input_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    maturity: str
    lifecycle: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    operation_class: str
    approval_policy: str
    external_side_effect: bool
    side_effect_destinations: list[str]
    timeout_seconds: float
    max_retries: int
    idempotency: str
    least_privilege_scopes: list[str]
    least_privilege_roles: list[str]
    docs: list[str]
    audit_events: list[str]


class CapabilityDescriptorResponse(GovernedModel):
    descriptor_id: str
    descriptor_version: str
    descriptor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    family: str
    resource_kind: str
    name: str
    auth_modes: list[str]
    operations: list[OperationDescriptorResponse]
    provenance: list[ProvenanceResponse]
    observability: list[str]
    audit: list[str]
    metadata: dict[str, Any]


class ProviderDescriptorResponse(GovernedModel):
    provider_id: str
    family: str
    name: str
    description: str
    auth_modes: list[str]
    provenance: list[ProvenanceResponse]
    capability_descriptors: list[CapabilityDescriptorResponse]


class DiscoveryWarningResponse(GovernedModel):
    reason_code: str
    message: str
    provider_id: str
    instance_id: str | None = None


class ProviderCatalogResponse(GovernedModel):
    provider_contract_version: Literal["research-assistant.integration-provider.v7"]
    canonicalization_version: Literal["research-assistant.canonical-json.v1"]
    providers: list[ProviderDescriptorResponse]
    warnings: list[DiscoveryWarningResponse]


class BindabilityDecisionResponse(GovernedModel):
    operation_id: str
    bindable: bool
    reason_codes: list[str]


class CapabilityInstanceResponse(GovernedModel):
    provider_contract_version: Literal["research-assistant.integration-provider.v7"]
    provider_id: str
    instance_id: str
    descriptor_id: str
    descriptor_version: str
    descriptor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tenant_id: str
    project_id: str
    provider_resource_id: str
    discovered_provider_version: str
    discovered_resource_version: str | None
    name: str
    readiness: str
    health: str
    last_checked_at: str
    configuration: dict[str, Any]
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    connection_ref: str | None
    connection_version: str
    auth_mode: str
    connection_identity_mode: str
    connection_scopes: list[str]
    connection_roles: list[str]
    connection_authorization_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    bindability: list[BindabilityDecisionResponse]
    config_validated: bool
    allowed_destination_constraints: list[str]
    allowed_destinations_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status_evidence: list[str]
    unavailable_reason: str | None = None


class DiscoveryResultResponse(GovernedModel):
    provider_contract_version: Literal["research-assistant.integration-provider.v7"]
    canonicalization_version: Literal["research-assistant.canonical-json.v1"]
    provider_id: str
    descriptors: list[CapabilityDescriptorResponse]
    instances: list[CapabilityInstanceResponse]
    warnings: list[DiscoveryWarningResponse]
    refreshed_at: str


class CapabilityBinding(GovernedModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        json_schema_extra={
            "$id": CAPABILITY_BINDING_SCHEMA_ID,
            "examples": [
                {
                    "binding_id": "binding-001",
                    "provider_contract_version": "research-assistant.integration-provider.v7",
                    "canonicalization_version": "research-assistant.canonical-json.v1",
                    "provider_id": "provider",
                    "tenant_id": "tenant",
                    "project_id": "project",
                    "descriptor_id": "provider.capability",
                    "descriptor_version": "1.0.0",
                    "descriptor_digest": "0" * 64,
                    "operation_id": "search",
                    "operation_version": "1.0.0",
                    "input_schema_digest": "1" * 64,
                    "output_schema_digest": "2" * 64,
                    "instance_id": "provider.instance",
                    "discovered_provider_version": "1.0.0",
                    "discovered_resource_version": "2026-07-23",
                    "instance_fingerprint": "3" * 64,
                    "configuration_id": "configuration-001",
                    "configuration_digest": "4" * 64,
                    "connection_id": "connection-001",
                    "connection_auth_mode": "oauth",
                    "connection_authorization_digest": "5" * 64,
                    "policy_id": "agent-studio",
                    "policy_version": "1.0.0",
                    "policy_digest": "6" * 64,
                    "allowed_destination_constraints": [],
                    "allowed_destinations_digest": "7" * 64,
                }
            ],
        },
    )

    binding_id: str = Field(min_length=1, max_length=512)
    provider_contract_version: Literal["research-assistant.integration-provider.v7"]
    canonicalization_version: Literal["research-assistant.canonical-json.v1"]
    provider_id: str = Field(min_length=1, max_length=128)
    descriptor_id: str = Field(min_length=1, max_length=128)
    descriptor_version: str = Field(min_length=1, max_length=128)
    descriptor_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(min_length=1, max_length=128)
    operation_version: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=512)
    discovered_provider_version: str = Field(min_length=1, max_length=128)
    discovered_resource_version: str | None
    input_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    configuration_id: str | None
    configuration_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    connection_id: str | None
    connection_auth_mode: str | None
    connection_authorization_digest: str | None = Field(
        pattern=r"^[0-9a-f]{64}$",
    )
    policy_id: str = Field(min_length=1, max_length=512)
    policy_version: str = Field(min_length=1, max_length=128)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_destination_constraints: tuple[str, ...]
    allowed_destinations_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    instance_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ValidationResultResponse(GovernedModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "provider_id": "provider",
                    "instance_id": "provider.instance",
                    "binding_id": "binding-001",
                    "readiness": "ready",
                    "reasons": [],
                }
            ]
        }
    )

    provider_id: str
    instance_id: str
    binding_id: str | None
    readiness: str
    reasons: list[str]


class HealthResultResponse(GovernedModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "provider_id": "provider",
                    "instance_id": "provider.instance",
                    "binding_id": "binding-001",
                    "readiness": "ready",
                    "evidence": ["validated"],
                }
            ]
        }
    )

    provider_id: str
    instance_id: str
    binding_id: str | None
    readiness: str
    evidence: list[str]


class ProviderInvokeResultResponse(GovernedModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "provider_id": "provider",
                    "instance_id": "provider.instance",
                    "operation_id": "search",
                    "status_code": 200,
                    "output": {"items": []},
                    "audit_metadata": {"correlation_id": "correlation-001"},
                }
            ]
        }
    )

    provider_id: str
    instance_id: str
    operation_id: str
    status_code: int
    output: dict[str, Any]
    audit_metadata: dict[str, Any]


class ProviderErrorDetailResponse(GovernedModel):
    code: str
    message: str
    provider_id: str
    instance_id: str | None
    old_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    new_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    changed_categories: list[str] | None = None
    action: Literal["rebind_and_review"] | None = None


class ProviderErrorEnvelopeResponse(GovernedModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "error": {
                        "code": "validation",
                        "message": "The provider request is invalid.",
                        "provider_id": "provider",
                        "instance_id": "provider.instance",
                    }
                }
            ]
        }
    )

    error: ProviderErrorDetailResponse


class RequestValidationIssueResponse(GovernedModel):
    type: str
    loc: list[str | int]
    msg: str
    input: Any | None = None
    ctx: dict[str, Any] | None = None


class RequestValidationErrorResponse(GovernedModel):
    detail: list[RequestValidationIssueResponse]


class HttpErrorResponse(GovernedModel):
    detail: str


def _operation_json(operation: OperationDescriptor) -> dict[str, Any]:
    return {
        "operation_id": operation.operation_id,
        "operation_version": operation.operation_version,
        "input_schema_digest": operation.input_schema_digest,
        "output_schema_digest": operation.output_schema_digest,
        "maturity": operation.maturity.value,
        "lifecycle": operation.lifecycle.value,
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
        "docs": list(operation.docs),
        "audit_events": list(operation.audit_events),
    }


def _capability_json(capability: CapabilityDescriptor) -> dict[str, Any]:
    return {
        "descriptor_id": capability.descriptor_id,
        "descriptor_version": capability.descriptor_version,
        "descriptor_digest": capability.descriptor_digest,
        "family": capability.family,
        "resource_kind": capability.resource_kind,
        "name": capability.name,
        "auth_modes": [mode.value for mode in capability.auth_modes],
        "operations": [_operation_json(operation) for operation in capability.operations],
        "provenance": [
            {
                "official_url": record.official_url,
                "source_version": record.source_version,
                "last_verified_at": record.last_verified_at,
                "retirement_date": record.retirement_date,
            }
            for record in capability.provenance
        ],
        "observability": list(capability.observability),
        "audit": list(capability.audit),
        "metadata": plain_json(capability.metadata),
    }


def _instance_json(
    instance: CapabilityInstance,
    *,
    discovery: Any,
    context: InvocationContext,
) -> dict[str, Any]:
    descriptor = discovery.descriptor_for(instance)
    bindability = bindability_decisions(
        discovery,
        instance,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        policy_ref=context.policy_ref,
    )
    fingerprint = capability_instance_fingerprint(
        instance,
        descriptor,
        policy_ref=context.policy_ref,
    )
    destinations = allowed_destinations_ref(instance.allowed_destination_constraints)
    return {
        "provider_contract_version": PROVIDER_CONTRACT_VERSION,
        "provider_id": instance.provider_id,
        "instance_id": instance.instance_id,
        "descriptor_id": instance.descriptor_id,
        "descriptor_version": instance.descriptor_version,
        "descriptor_digest": instance.descriptor_digest,
        "tenant_id": instance.tenant_id,
        "project_id": instance.project_id,
        "provider_resource_id": instance.provider_resource_id,
        "discovered_provider_version": instance.discovered_provider_version,
        "discovered_resource_version": instance.discovered_resource_version,
        "name": instance.name,
        "readiness": instance.readiness.value,
        "health": instance.health.value,
        "last_checked_at": instance.last_checked_at,
        "configuration": plain_json(instance.configuration),
        "config_hash": instance.config_fingerprint,
        "connection_ref": instance.connection_ref,
        "connection_version": instance.connection_version,
        "auth_mode": instance.auth_mode.value,
        "connection_identity_mode": instance.connection_identity_mode,
        "connection_scopes": list(instance.connection_scopes),
        "connection_roles": list(instance.connection_roles),
        "connection_authorization_digest": authorization_digest(instance),
        "instance_fingerprint": fingerprint,
        "bindability": [
            {
                "operation_id": decision.operation_id,
                "bindable": decision.bindable,
                "reason_codes": [reason.value for reason in decision.reason_codes],
            }
            for decision in bindability
        ],
        "config_validated": instance.config_validated,
        "allowed_destination_constraints": list(destinations.constraints),
        "allowed_destinations_digest": destinations.constraints_digest,
        "status_evidence": list(instance.status_evidence),
        "unavailable_reason": instance.unavailable_reason,
    }


def _provider_json(provider: ProviderDescriptor) -> dict[str, Any]:
    return {
        "provider_id": provider.provider_id,
        "family": provider.family,
        "name": provider.name,
        "description": provider.description,
        "auth_modes": [mode.value for mode in provider.auth_modes],
        "provenance": [
            {
                "official_url": record.official_url,
                "source_version": record.source_version,
                "last_verified_at": record.last_verified_at,
                "retirement_date": record.retirement_date,
            }
            for record in provider.provenance
        ],
        "capability_descriptors": [_capability_json(capability) for capability in provider.capability_descriptors],
    }


def _report_json(report: ValidationReport | HealthReport) -> dict[str, Any]:
    if isinstance(report, ValidationReport):
        return {"readiness": report.readiness.value, "reasons": list(report.reasons)}
    return {"readiness": report.readiness.value, "evidence": list(report.evidence)}


def _result_json(result: InvocationResult, binding_id: str | None = None) -> dict[str, Any]:
    audit_metadata = dict(plain_json(result.audit_metadata))
    if binding_id is not None:
        audit_metadata["binding_id"] = binding_id
    return {
        "provider_id": result.provider_id,
        "instance_id": result.instance_id,
        "operation_id": result.operation_id,
        "status_code": result.status_code,
        "output": plain_json(result.output),
        "audit_metadata": audit_metadata,
    }


class ProviderService:
    def __init__(
        self,
        registry: AsyncProviderRegistry,
        context_factory: ProviderContextFactory | None = None,
        binding_resolver: ProviderBindingResolver | None = None,
        warnings: tuple[DiscoveryWarning, ...] = (),
    ) -> None:
        self._registry = registry
        self._context_factory = context_factory
        self._binding_resolver = binding_resolver
        self._warnings = warnings

    @property
    def registry(self) -> AsyncProviderRegistry:
        return self._registry

    async def catalog(self) -> dict[str, Any]:
        return {
            "provider_contract_version": PROVIDER_CONTRACT_VERSION,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "providers": [_provider_json(provider.descriptor) for provider in self._registry.providers.values()],
            "warnings": [
                {
                    "reason_code": warning.reason_code,
                    "message": warning.message,
                    "provider_id": warning.provider_id,
                    "instance_id": warning.instance_id,
                }
                for warning in self._warnings
            ],
        }

    def _provider_context(
        self,
        provider_id: str,
        request: Request,
    ) -> tuple[Provider, InvocationContext]:
        try:
            provider = self._registry.get(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration provider was not found.") from exc
        if self._context_factory is None:
            raise HTTPException(
                status_code=503,
                detail="Integration provider runtime is not configured.",
            )
        return provider, self._context_factory(provider_id, request)

    async def discover(self, provider_id: str, request: Request) -> dict[str, Any]:
        provider, context = self._provider_context(provider_id, request)
        result = await provider.discover(context)
        return {
            "provider_contract_version": PROVIDER_CONTRACT_VERSION,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "provider_id": provider_id,
            "descriptors": [_capability_json(descriptor) for descriptor in result.descriptors],
            "instances": [_instance_json(instance, discovery=result, context=context) for instance in result.instances],
            "warnings": [
                {
                    "reason_code": warning.reason_code,
                    "message": warning.message,
                    "provider_id": warning.provider_id,
                    "instance_id": warning.instance_id,
                }
                for warning in result.warnings
            ],
            "refreshed_at": result.refreshed_at,
        }

    @staticmethod
    async def _instance(
        provider: Provider,
        context: InvocationContext,
        instance_id: str,
    ) -> CapabilityInstance:
        result = await provider.discover(context)
        instance = next(
            (candidate for candidate in result.instances if candidate.instance_id == instance_id),
            None,
        )
        if instance is None:
            raise UnavailableError(
                "Capability instance is not present in current provider discovery",
                provider_id=provider.descriptor.provider_id,
                instance_id=instance_id,
            )
        return instance

    async def _target(
        self,
        provider: Provider,
        context: InvocationContext,
        instance_id: str,
        binding_id: str | None,
        request: Request,
    ) -> CapabilityInstance | RuntimeCapabilityBinding:
        if binding_id is None:
            return await self._instance(provider, context, instance_id)
        if self._binding_resolver is None:
            raise UnavailableError(
                "Capability binding runtime is not configured",
                provider_id=provider.descriptor.provider_id,
                instance_id=instance_id,
            )
        binding = self._binding_resolver(provider.descriptor.provider_id, binding_id, request)
        if binding.binding_id != binding_id or binding.instance_ref.instance_id != instance_id:
            raise PolicyError(
                "Resolved capability binding does not match the requested target",
                provider_id=provider.descriptor.provider_id,
                instance_id=instance_id,
            )
        return binding

    async def validate(
        self,
        provider_id: str,
        instance_id: str,
        request: Request,
        binding_id: str | None = None,
    ) -> dict[str, Any]:
        provider, context = self._provider_context(provider_id, request)
        target = await self._target(provider, context, instance_id, binding_id, request)
        target_instance_id = (
            target.instance_ref.instance_id
            if isinstance(target, RuntimeCapabilityBinding)
            else target.instance_id
        )
        return {
            "provider_id": provider_id,
            "instance_id": target_instance_id,
            "binding_id": binding_id,
            **_report_json(await provider.validate(target, context)),
        }

    async def health(
        self,
        provider_id: str,
        instance_id: str,
        request: Request,
        binding_id: str | None = None,
    ) -> dict[str, Any]:
        provider, context = self._provider_context(provider_id, request)
        target = await self._target(provider, context, instance_id, binding_id, request)
        target_instance_id = (
            target.instance_ref.instance_id
            if isinstance(target, RuntimeCapabilityBinding)
            else target.instance_id
        )
        return {
            "provider_id": provider_id,
            "instance_id": target_instance_id,
            "binding_id": binding_id,
            **_report_json(await provider.health(target, context)),
        }

    async def invoke(
        self,
        provider_id: str,
        payload: ProviderInvokePayload,
        request: Request,
    ) -> dict[str, Any]:
        provider, context = self._provider_context(provider_id, request)
        target = await self._target(
            provider,
            context,
            payload.instance_id,
            payload.binding_id,
            request,
        )
        try:
            invocation = InvocationRequest(
                target=target,
                operation_id=payload.operation_id,
                arguments=payload.arguments,
                idempotency_key=payload.idempotency_key,
            )
        except ValueError as exc:
            raise ProviderValidationError(
                str(exc),
                provider_id=provider_id,
                instance_id=payload.instance_id,
            ) from exc
        result = await provider.invoke(
            invocation,
            context,
        )
        return _result_json(result, payload.binding_id)


def provider_service(request: Request) -> ProviderService:
    service = request.app.state.provider_service
    if not isinstance(service, ProviderService):
        raise HTTPException(
            status_code=503,
            detail="Integration provider runtime is not configured.",
        )
    return service


PROVIDER_ROUTE_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ProviderErrorEnvelopeResponse, "description": "Provider authorization failed"},
    403: {"model": ProviderErrorEnvelopeResponse, "description": "Provider policy or consent denied"},
    404: {"model": HttpErrorResponse, "description": "Provider was not found"},
    409: {"model": ProviderErrorEnvelopeResponse, "description": "Capability binding is stale"},
    422: {
        "model": ProviderErrorEnvelopeResponse | RequestValidationErrorResponse,
        "description": "Provider or request validation failed",
    },
    429: {"model": ProviderErrorEnvelopeResponse, "description": "Provider rate limit exceeded"},
    502: {"model": ProviderErrorEnvelopeResponse, "description": "Provider upstream failed"},
    503: {
        "model": ProviderErrorEnvelopeResponse | HttpErrorResponse,
        "description": "Provider runtime is unavailable",
    },
    504: {"model": ProviderErrorEnvelopeResponse, "description": "Provider invocation timed out"},
}


router = APIRouter(prefix="/v1/providers", tags=["integration providers"])


@router.get(
    "",
    operation_id="listIntegrationProviders",
    response_model=ProviderCatalogResponse,
    response_model_exclude_none=True,
    responses=PROVIDER_ROUTE_RESPONSES,
)
async def list_providers(request: Request) -> Mapping[str, Any]:
    return await provider_service(request).catalog()


@router.get(
    "/{provider_id}/capabilities",
    operation_id="discoverIntegrationCapabilities",
    response_model=DiscoveryResultResponse,
    responses=PROVIDER_ROUTE_RESPONSES,
)
async def discover_capabilities(provider_id: str, request: Request) -> Mapping[str, Any]:
    return await provider_service(request).discover(provider_id, request)


@router.get(
    "/{provider_id}/instances/{instance_id}/validation",
    operation_id="validateIntegrationCapabilityInstance",
    response_model=ValidationResultResponse,
    responses=PROVIDER_ROUTE_RESPONSES,
)
async def validate_provider(
    provider_id: str,
    instance_id: str,
    request: Request,
    binding_id: str | None = None,
) -> Mapping[str, Any]:
    return await provider_service(request).validate(
        provider_id,
        instance_id,
        request,
        binding_id,
    )


@router.get(
    "/{provider_id}/instances/{instance_id}/health",
    operation_id="healthIntegrationCapabilityInstance",
    response_model=HealthResultResponse,
    responses=PROVIDER_ROUTE_RESPONSES,
)
async def provider_health(
    provider_id: str,
    instance_id: str,
    request: Request,
    binding_id: str | None = None,
) -> Mapping[str, Any]:
    return await provider_service(request).health(
        provider_id,
        instance_id,
        request,
        binding_id,
    )


@router.post(
    "/{provider_id}/invoke",
    operation_id="invokeIntegrationCapability",
    response_model=ProviderInvokeResultResponse,
    responses=PROVIDER_ROUTE_RESPONSES,
)
async def invoke_provider(
    provider_id: str,
    payload: ProviderInvokePayload,
    request: Request,
) -> Mapping[str, Any]:
    return await provider_service(request).invoke(
        provider_id,
        payload,
        request,
    )


PROVIDER_ERROR_STATUS = {
    "unauthorized": 401,
    "needs_consent": 403,
    "policy": 403,
    "stale_binding": 409,
    "validation": 422,
    "rate_limit": 429,
    "upstream": 502,
    "unavailable": 503,
    "timeout": 504,
}


def provider_error_response(error: ProviderError) -> JSONResponse:
    content: dict[str, Any] = {
        "error": {
            "code": error.code,
            "message": str(error),
            "provider_id": error.provider_id,
            "instance_id": error.instance_id,
        }
    }
    if isinstance(error, StaleBindingError):
        content["error"].update(
            {
                "old_fingerprint": error.old_fingerprint,
                "new_fingerprint": error.new_fingerprint,
                "changed_categories": [category.value for category in error.changed_categories],
                "action": "rebind_and_review",
            }
        )
    headers = {"Retry-After": str(max(0, int(error.retry_after)))} if error.retry_after is not None else None
    return JSONResponse(
        status_code=PROVIDER_ERROR_STATUS.get(error.code, 500),
        content=content,
        headers=headers,
    )


class ProviderContractApplication(FastAPI):
    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is not None:
            return self.openapi_schema
        specification = get_openapi(
            title=self.title,
            version=self.version,
            description=self.description,
            routes=self.routes,
        )
        binding_schema = CapabilityBinding.model_json_schema(
            ref_template="#/components/schemas/{model}",
        )
        binding_definitions = binding_schema.pop("$defs", {})
        schemas = specification.setdefault("components", {}).setdefault("schemas", {})
        schemas.update(binding_definitions)
        schemas["CapabilityBinding"] = binding_schema
        self.openapi_schema = specification
        return specification


contract_app = ProviderContractApplication(
    title="Research Assistant Integration Provider API",
    description="Governed discovery, health, validation, and invocation for Agent Studio providers.",
    version=PROVIDER_CONTRACT_VERSION,
)
contract_app.state.provider_service = ProviderService(AsyncProviderRegistry())
contract_app.include_router(router)


__all__ = [
    "CAPABILITY_BINDING_SCHEMA_ID",
    "ProviderBindingResolver",
    "ProviderContextFactory",
    "ProviderInvokePayload",
    "ProviderService",
    "contract_app",
    "provider_error_response",
    "router",
]
