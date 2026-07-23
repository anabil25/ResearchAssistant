"""Fixed-destination webhook provider."""

from __future__ import annotations

import json
from typing import Any

from ._http import auth_headers, require_endpoint, send, signing_credential
from .config import WebhookConfig
from .contracts import (
    ApprovalPolicy,
    AuthMode,
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityInstance,
    DiscoveryResult,
    HealthReport,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    Maturity,
    OperationClass,
    OperationDescriptor,
    ProviderDescriptor,
    Readiness,
    UnauthorizedError,
    ValidationReport,
    audit_metadata,
    capability_instance,
    discovery_result,
    find_operation,
    official_provenance,
    operation_allows_retry,
    plain_json,
    resolve_capability_target,
    validation_for_target,
)

PROVIDER_ID = "webhook"
DOCS = ("https://www.rfc-editor.org/rfc/rfc9110",)
PROVENANCE = official_provenance(
    DOCS,
    source_version="RFC 9110",
    last_verified_at="2026-07-23T08:37:02Z",
)


class WebhookProvider:
    def __init__(self, config: WebhookConfig) -> None:
        self._config = config
        operation = OperationDescriptor(
            config.operation_id,
            "1.0.0",
            Maturity.GA,
            {"type": "object"},
            {},
            OperationClass.PRIVILEGED,
            ApprovalPolicy.REQUIRED,
            external_side_effect=True,
            side_effect_destinations=(config.destination_url or "unconfigured:webhook-destination",),
            idempotency=Idempotency.REQUIRED,
            docs=DOCS,
        )
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "webhook",
            "Webhook",
            "Invokes one explicitly configured GA webhook operation at a fixed URL.",
            (AuthMode.NONE, AuthMode.OAUTH, AuthMode.API_KEY, AuthMode.SIGNATURE),
            PROVENANCE,
            (
                CapabilityDescriptor(
                    descriptor_id=f"webhook.{config.operation_id}",
                    family="webhook",
                    resource_kind="fixed_webhook",
                    name=config.operation_id,
                    auth_modes=(config.auth.mode,),
                    operations=(operation,),
                    provenance=PROVENANCE,
                ),
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _validate_configuration(self, context: InvocationContext) -> ValidationReport:
        if not self._config.destination_url or not self._config.operation_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("Webhook destination and operation ID are required.",))
        if not self._config.tenant_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("Webhook tenant boundary is required.",))
        if self._config.method.upper() not in {"POST", "PUT", "PATCH"}:
            return ValidationReport(Readiness.MISCONFIGURED, ("Webhook method must be POST, PUT, or PATCH.",))
        if self._config.tenant_id and context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            require_endpoint(self._config.destination_url)
            auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
            if self._config.signing_algorithm:
                signing_credential(context, provider_id=PROVIDER_ID)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        return ValidationReport(Readiness.READY)

    def _discover_instances(self, context: InvocationContext) -> tuple[CapabilityInstance, ...]:
        validation = self._validate_configuration(context)
        operation = self._descriptor.capability_descriptors[0].operations[0]
        reason = None if validation.readiness is Readiness.READY else "; ".join(validation.reasons)
        return (
            capability_instance(
                provider_id=PROVIDER_ID,
                instance_id=f"webhook.{self._config.operation_id}",
                family="webhook",
                resource_kind="fixed_webhook",
                name=self._config.operation_id,
                readiness=validation.readiness,
                auth_modes=(self._config.auth.mode,)
                + ((AuthMode.SIGNATURE,) if self._config.signing_algorithm else ()),
                tenant_boundary="configured tenant",
                data_boundary="single configured destination URL",
                operations=(operation,),
                provenance=PROVENANCE,
                status_evidence=("Fixed destination and credential abstraction validated.",),
                unavailable_reason=reason,
            ),
        )

    def discover(self, context: InvocationContext) -> DiscoveryResult:
        return discovery_result(self._discover_instances(context))

    def validate(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> ValidationReport:
        return validation_for_target(self.discover(context), target, provider_id=PROVIDER_ID)

    def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport:
        instance, _ = resolve_capability_target(self.discover(context), target, provider_id=PROVIDER_ID)
        if instance.readiness is not Readiness.READY or self._config.health_method is None:
            return HealthReport(
                instance.health or instance.readiness,
                instance.status_evidence or ("No live health method configured.",),
            )
        response, _ = send(
            context,
            provider_id=PROVIDER_ID,
            method=self._config.health_method,
            url=require_endpoint(self._config.destination_url),
            headers=auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
            if not self._config.signing_algorithm
            else {},
            idempotent=True,
        )
        return HealthReport(Readiness.READY, (f"Health request returned HTTP {response.status_code}.",))

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        instance, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        payload = json.dumps(plain_json(request.arguments), separators=(",", ":"), sort_keys=True).encode()
        headers = auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = request.idempotency_key or ""
        if algorithm := self._config.signing_algorithm:
            signature = signing_credential(context, provider_id=PROVIDER_ID).sign(payload, algorithm=algorithm)
            headers[self._config.signature_header] = signature
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method=self._config.method.upper(),
            url=require_endpoint(self._config.destination_url),
            headers=headers,
            content=payload,
            timeout=operation.timeout_seconds,
            max_retries=operation.max_retries,
            idempotent=operation_allows_retry(
                operation,
                idempotency_key=request.idempotency_key,
            ),
        )
        content_type = response.headers.get("content-type", "")
        output: Any = response.json() if "json" in content_type else response.text
        return InvocationResult(
            PROVIDER_ID,
            instance.instance_id,
            operation.operation_id,
            response.status_code,
            output,
            audit_metadata(
                context,
                provider_id=PROVIDER_ID,
                instance_id=instance.instance_id,
                operation_id=operation.operation_id,
                attempts=attempts,
                response=response,
            ),
        )
