"""Fixed-destination webhook provider."""

from __future__ import annotations

import json
from typing import Any

from ._http import auth_headers, require_endpoint, send, signing_credential
from .config import WebhookConfig
from .contracts import (
    ApprovalPolicy,
    AuthMode,
    CapabilityDescriptor,
    HealthReport,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    Maturity,
    OperationDescriptor,
    ProviderDescriptor,
    Readiness,
    Risk,
    UnauthorizedError,
    ValidationReport,
    audit_metadata,
    find_operation,
    plain_json,
)

PROVIDER_ID = "webhook"
DOCS = ("https://www.rfc-editor.org/rfc/rfc9110",)


class WebhookProvider:
    def __init__(self, config: WebhookConfig) -> None:
        self._config = config
        operation = OperationDescriptor(
            config.operation_id,
            Maturity.GA,
            {"type": "object"},
            {},
            Risk.EXTERNAL_SIDE_EFFECT,
            ApprovalPolicy.REQUIRED,
            idempotency=Idempotency.REQUIRED,
            docs=DOCS,
        )
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "webhook",
            "Webhook",
            "Invokes one explicitly configured GA webhook operation at a fixed URL.",
            (AuthMode.NONE, AuthMode.OAUTH, AuthMode.API_KEY, AuthMode.SIGNATURE),
            DOCS,
            (
                CapabilityDescriptor(
                    PROVIDER_ID,
                    f"webhook.{config.operation_id}",
                    "webhook",
                    "fixed_webhook",
                    config.operation_id,
                    Readiness.MISCONFIGURED,
                    False,
                    (config.auth.mode,),
                    "configured tenant",
                    "single configured destination URL",
                    (operation,),
                    DOCS,
                    ("Configuration has not yet been validated.",),
                    unavailable_reason="Webhook configuration has not yet been validated",
                ),
            ),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def validate(self, context: InvocationContext) -> ValidationReport:
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

    def discover(self, context: InvocationContext) -> tuple[CapabilityDescriptor, ...]:
        validation = self.validate(context)
        operation = self._descriptor.capabilities[0].operations[0]
        reason = None if validation.readiness is Readiness.READY else "; ".join(validation.reasons)
        return (
            CapabilityDescriptor(
                PROVIDER_ID,
                f"webhook.{self._config.operation_id}",
                "webhook",
                "fixed_webhook",
                self._config.operation_id,
                validation.readiness,
                validation.readiness is Readiness.READY,
                (self._config.auth.mode,) + ((AuthMode.SIGNATURE,) if self._config.signing_algorithm else ()),
                "configured tenant",
                "single configured destination URL",
                (operation,),
                DOCS,
                ("Fixed destination and credential abstraction validated.",),
                unavailable_reason=reason,
            ),
        )

    def health(self, context: InvocationContext) -> HealthReport:
        validation = self.validate(context)
        if validation.readiness is not Readiness.READY or self._config.health_method is None:
            return HealthReport(validation.readiness, validation.reasons or ("No live health method configured.",))
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
        capability, operation = find_operation(
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
            idempotent=True,
        )
        content_type = response.headers.get("content-type", "")
        output: Any = response.json() if "json" in content_type else response.text
        return InvocationResult(
            PROVIDER_ID,
            capability.capability_id,
            operation.operation_id,
            response.status_code,
            output,
            audit_metadata(
                context,
                provider_id=PROVIDER_ID,
                capability_id=capability.capability_id,
                operation_id=operation.operation_id,
                attempts=attempts,
                response=response,
            ),
        )
