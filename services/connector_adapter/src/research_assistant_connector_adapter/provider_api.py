"""Governed HTTP boundary for operational capability providers."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from research_assistant_connectors.providers import (
    CapabilityDescriptor,
    CapabilityInstance,
    HealthReport,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    OperationDescriptor,
    ProviderDescriptor,
    ProviderError,
    ProviderRegistry,
    ValidationReport,
    capability_instance_fingerprint,
)
from research_assistant_connectors.providers.contracts import plain_json

ProviderContextFactory = Callable[[str, Request], InvocationContext]


class ProviderInvokePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: str = Field(min_length=1, max_length=512)
    operation_id: str = Field(min_length=1, max_length=512)
    arguments: dict[str, Any] = Field(default_factory=dict)


def _operation_json(operation: OperationDescriptor) -> dict[str, Any]:
    return {
        "operation_id": operation.operation_id,
        "version": operation.version,
        "maturity": operation.maturity.value,
        "input_schema": plain_json(operation.input_schema),
        "output_schema": plain_json(operation.output_schema),
        "operation_class": operation.operation_class.value,
        "approval_policy": operation.approval_policy.value,
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
        "family": capability.family,
        "resource_kind": capability.resource_kind,
        "name": capability.name,
        "auth_modes": [mode.value for mode in capability.auth_modes],
        "operations": [_operation_json(operation) for operation in capability.operations],
        "provenance": list(capability.provenance),
        "observability": list(capability.observability),
        "audit": list(capability.audit),
        "metadata": plain_json(capability.metadata),
    }


def _instance_json(instance: CapabilityInstance) -> dict[str, Any]:
    return {
        "provider_id": instance.provider_id,
        "instance_id": instance.instance_id,
        "descriptor_id": instance.descriptor.descriptor_id,
        "instance_fingerprint": capability_instance_fingerprint(instance),
        "name": instance.name,
        "readiness": instance.readiness.value,
        "attachable_operation_ids": list(instance.attachable_operation_ids),
        "tenant_boundary": instance.tenant_boundary,
        "data_boundary": instance.data_boundary,
        "configuration": plain_json(instance.configuration),
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
        "provenance": list(provider.provenance),
        "capability_descriptors": [
            _capability_json(capability)
            for capability in provider.capability_descriptors
        ],
    }


def _report_json(report: ValidationReport | HealthReport) -> dict[str, Any]:
    if isinstance(report, ValidationReport):
        return {"readiness": report.readiness.value, "reasons": list(report.reasons)}
    return {"readiness": report.readiness.value, "evidence": list(report.evidence)}


def _result_json(result: InvocationResult) -> dict[str, Any]:
    return {
        "provider_id": result.provider_id,
        "instance_id": result.instance_id,
        "operation_id": result.operation_id,
        "status_code": result.status_code,
        "output": plain_json(result.output),
        "audit_metadata": plain_json(result.audit_metadata),
    }


class ProviderService:
    def __init__(
        self,
        registry: ProviderRegistry,
        context_factory: ProviderContextFactory | None = None,
    ) -> None:
        self._registry = registry
        self._context_factory = context_factory

    def catalog(self) -> dict[str, Any]:
        return {
            "schema_version": "research-assistant.integration-provider.v2",
            "providers": [
                _provider_json(provider.descriptor)
                for provider in self._registry.providers.values()
            ],
        }

    def _provider_context(
        self,
        provider_id: str,
        request: Request,
    ) -> tuple[Any, InvocationContext]:
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

    def discover(self, provider_id: str, request: Request) -> dict[str, Any]:
        provider, context = self._provider_context(provider_id, request)
        instances = provider.discover(context)
        descriptors = {
            instance.descriptor.descriptor_id: instance.descriptor
            for instance in instances
        }
        return {
            "provider_id": provider_id,
            "descriptors": [
                _capability_json(descriptor)
                for descriptor in descriptors.values()
            ],
            "instances": [_instance_json(instance) for instance in instances],
        }

    def validate(self, provider_id: str, request: Request) -> dict[str, Any]:
        provider, context = self._provider_context(provider_id, request)
        return {"provider_id": provider_id, **_report_json(provider.validate(context))}

    def health(self, provider_id: str, request: Request) -> dict[str, Any]:
        provider, context = self._provider_context(provider_id, request)
        return {"provider_id": provider_id, **_report_json(provider.health(context))}

    def invoke(
        self,
        provider_id: str,
        payload: ProviderInvokePayload,
        request: Request,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        provider, context = self._provider_context(provider_id, request)
        result = provider.invoke(
            InvocationRequest(
                instance_id=payload.instance_id,
                operation_id=payload.operation_id,
                arguments=payload.arguments,
                idempotency_key=idempotency_key,
            ),
            context,
        )
        return _result_json(result)


def provider_service(request: Request) -> ProviderService:
    service = request.app.state.provider_service
    if not isinstance(service, ProviderService):
        raise HTTPException(
            status_code=503,
            detail="Integration provider runtime is not configured.",
        )
    return service


router = APIRouter(prefix="/v1/providers", tags=["integration providers"])


@router.get("", operation_id="listIntegrationProviders")
def list_providers(request: Request) -> Mapping[str, Any]:
    return provider_service(request).catalog()


@router.get("/{provider_id}/capabilities", operation_id="discoverIntegrationCapabilities")
def discover_capabilities(provider_id: str, request: Request) -> Mapping[str, Any]:
    return provider_service(request).discover(provider_id, request)


@router.get("/{provider_id}/validation", operation_id="validateIntegrationProvider")
def validate_provider(provider_id: str, request: Request) -> Mapping[str, Any]:
    return provider_service(request).validate(provider_id, request)


@router.get("/{provider_id}/health", operation_id="healthIntegrationProvider")
def provider_health(provider_id: str, request: Request) -> Mapping[str, Any]:
    return provider_service(request).health(provider_id, request)


@router.post("/{provider_id}/invoke", operation_id="invokeIntegrationCapability")
def invoke_provider(
    provider_id: str,
    payload: ProviderInvokePayload,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> Mapping[str, Any]:
    return provider_service(request).invoke(
        provider_id,
        payload,
        request,
        idempotency_key,
    )


PROVIDER_ERROR_STATUS = {
    "unauthorized": 401,
    "needs_consent": 403,
    "policy": 403,
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
    headers = (
        {"Retry-After": str(max(0, int(error.retry_after)))}
        if error.retry_after is not None
        else None
    )
    return JSONResponse(
        status_code=PROVIDER_ERROR_STATUS.get(error.code, 500),
        content=content,
        headers=headers,
    )


contract_app = FastAPI(
    title="Research Assistant Integration Provider API",
    description="Governed discovery, health, validation, and invocation for Agent Studio providers.",
    version="1.0.0",
)
contract_app.state.provider_service = ProviderService(ProviderRegistry())
contract_app.include_router(router)


__all__ = [
    "ProviderContextFactory",
    "ProviderInvokePayload",
    "ProviderService",
    "contract_app",
    "provider_error_response",
    "router",
]
