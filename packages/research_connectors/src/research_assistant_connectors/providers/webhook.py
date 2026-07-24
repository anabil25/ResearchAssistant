"""Fixed-destination webhook provider."""

from __future__ import annotations

import json
import re
from typing import Any

from ._http import auth_headers, binding_safe_endpoint, require_endpoint, send, signing_credential
from .config import WebhookConfig
from .contracts import (
    ApprovalPolicy,
    AuthMode,
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityInstance,
    CapabilityRecord,
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
    canonical_json_hash,
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
HTTP_FIELD_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
TRANSPORT_CONTROLLED_HEADERS = {
    "connection",
    "content-length",
    "content-type",
    "expect",
    "host",
    "idempotency-key",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
DOCS = ("https://www.rfc-editor.org/rfc/rfc9110",)
PROVENANCE = official_provenance(
    DOCS,
    source_version="RFC 9110",
    last_verified_at="2026-07-23T08:37:02Z",
)


def _destination_policy(url: str | None) -> tuple[str, str]:
    sanitized, digest = binding_safe_endpoint(
        url,
        invalid_label="invalid:webhook-destination",
    )
    return sanitized, f"{sanitized}#url-sha256={digest}"


class WebhookProvider:
    def __init__(self, config: WebhookConfig) -> None:
        self._config = config
        self._auth_modes = (
            (config.auth.mode,)
            if not config.signing_algorithm or config.auth.mode is AuthMode.SIGNATURE
            else (config.auth.mode, AuthMode.SIGNATURE)
        )
        self._sanitized_destination, destination_policy = _destination_policy(config.destination_url)
        operation = OperationDescriptor(
            config.operation_id,
            "1.0.0",
            Maturity.GA,
            {"type": "object"},
            {},
            OperationClass.PRIVILEGED,
            ApprovalPolicy.REQUIRED,
            external_side_effect=True,
            side_effect_destinations=(destination_policy,),
            idempotency=Idempotency.CALLER_KEY,
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
                    auth_modes=self._auth_modes,
                    operations=(operation,),
                    provenance=PROVENANCE,
                    descriptor_version="1.1.0",
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
        if self._config.auth.mode is AuthMode.SIGNATURE and not self._config.signing_algorithm:
            return ValidationReport(
                Readiness.MISCONFIGURED,
                ("Signature authentication requires a signing algorithm.",),
            )
        if self._config.signing_algorithm:
            reserved_headers = set(TRANSPORT_CONTROLLED_HEADERS)
            if self._config.auth.mode in {AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.GITHUB_APP}:
                reserved_headers.add("authorization")
            elif self._config.auth.header_name:
                reserved_headers.add(self._config.auth.header_name.casefold())
            if (
                HTTP_FIELD_NAME.fullmatch(self._config.signature_header) is None
                or self._config.signature_header.casefold() in reserved_headers
            ):
                return ValidationReport(
                    Readiness.MISCONFIGURED,
                    ("Signature header is invalid or conflicts with a controlled request header.",),
                )
        if self._config.tenant_id and context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            require_endpoint(self._config.destination_url)
            auth_headers(
                self._config.auth,
                context,
                provider_id=PROVIDER_ID,
                allow_signature=True,
            )
            if self._config.signing_algorithm:
                signing_credential(context, provider_id=PROVIDER_ID)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        return ValidationReport(Readiness.READY)

    def _discover_instances(self, context: InvocationContext) -> tuple[CapabilityRecord, ...]:
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
                auth_modes=self._auth_modes,
                tenant_boundary="configured tenant",
                data_boundary="single configured destination URL",
                resource_id=self._sanitized_destination,
                operations=(operation,),
                provenance=PROVENANCE,
                status_evidence=("Fixed destination and credential abstraction validated.",),
                unavailable_reason=reason,
                configuration={
                    "provider_endpoint": self._sanitized_destination,
                    "provider_endpoint_digest": canonical_json_hash(self._sanitized_destination),
                    "method": self._config.method,
                    "health_method": self._config.health_method,
                    "signing_algorithm": self._config.signing_algorithm,
                    "signature_header": self._config.signature_header,
                    "auth_header_name": self._config.auth.header_name,
                },
                descriptor_version="1.1.0",
                selected_auth_mode=self._config.auth.mode,
                connection_id=self._config.auth.connection_ref,
                connection_scopes=self._config.auth.connection_scopes,
                connection_version=self._config.auth.connection_version,
                connection_identity_mode=self._config.auth.effective_identity_mode,
                connection_roles=self._config.auth.authorized_roles,
            ),
        )

    def discover(self, context: InvocationContext) -> DiscoveryResult:
        return discovery_result(
            self._discover_instances(context),
            tenant_id=self._config.tenant_id or context.tenant_id,
            project_id=context.project_id,
        )

    def validate(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> ValidationReport:
        return validation_for_target(
            self.discover(context),
            target,
            provider_id=PROVIDER_ID,
            policy_ref=context.policy_ref,
            logical_agent_id=context.logical_agent_id,
        )

    def _request_headers(self, context: InvocationContext, payload: bytes) -> dict[str, str]:
        headers = auth_headers(
            self._config.auth,
            context,
            provider_id=PROVIDER_ID,
            allow_signature=True,
        )
        if algorithm := self._config.signing_algorithm:
            headers[self._config.signature_header] = signing_credential(
                context,
                provider_id=PROVIDER_ID,
            ).sign(payload, algorithm=algorithm)
        return headers

    def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport:
        instance, _ = resolve_capability_target(
            self.discover(context),
            target,
            provider_id=PROVIDER_ID,
            policy_ref=context.policy_ref,
            logical_agent_id=context.logical_agent_id,
        )
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
            headers=self._request_headers(context, b""),
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
        headers = self._request_headers(context, payload)
        headers["Content-Type"] = "application/json"
        headers["Idempotency-Key"] = request.idempotency_key or ""
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
