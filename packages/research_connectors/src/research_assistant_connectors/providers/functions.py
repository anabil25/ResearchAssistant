"""Azure Functions HTTP provider with configurable discovery style."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ._http import auth_headers, collection, json_object, require_endpoint, safe_url, send, stable_resource_id
from .config import FunctionPolicy, FunctionsConfig
from .contracts import (
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
    UnauthorizedError,
    ValidationReport,
    audit_metadata,
    find_operation,
)

PROVIDER_ID = "azure_functions"
DOCS = (
    "https://learn.microsoft.com/azure/azure-functions/functions-bindings-http-webhook-trigger",
    "https://learn.microsoft.com/azure/azure-functions/function-keys-how-to",
)


def _function_capability(
    name: str,
    policy: FunctionPolicy,
    readiness: Readiness,
    reason: str | None,
    evidence: tuple[str, ...],
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        provider_id=PROVIDER_ID,
        capability_id=stable_resource_id("functions.http", name),
        family="azure_functions",
        resource_kind="http_function",
        name=name,
        readiness=readiness,
        attachable=readiness is Readiness.READY,
        auth_modes=(AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.API_KEY),
        tenant_boundary="configured Microsoft Entra tenant",
        data_boundary="configured Function App endpoint",
        operations=(
            OperationDescriptor(
                operation_id="functions.http.invoke",
                maturity=Maturity.GA,
                input_schema={"type": "object"},
                output_schema={},
                risk=policy.risk,
                approval_policy=policy.approval_policy,
                idempotency=policy.idempotency,
                least_privilege_scopes=("Function App application scope",),
                docs=DOCS,
            ),
        ),
        provenance=DOCS,
        status_evidence=evidence,
        unavailable_reason=reason,
        metadata={"function_name": name},
    )


class AzureFunctionsProvider:
    def __init__(self, config: FunctionsConfig) -> None:
        self._config = config
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "azure_functions",
            "Azure Functions",
            "Discovers configured HTTP functions and invokes only fixed Function App routes.",
            (AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.API_KEY),
            DOCS,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def validate(self, context: InvocationContext) -> ValidationReport:
        if not self._config.endpoint or not self._config.discovery_url:
            return ValidationReport(Readiness.MISCONFIGURED, ("Function endpoint and discovery URL are required.",))
        if not self._config.tenant_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("Function tenant boundary is required.",))
        if self._config.discovery_style not in {"http", "admin", "arm"}:
            return ValidationReport(Readiness.MISCONFIGURED, ("Discovery style must be http, admin, or arm.",))
        if "{name}" not in self._config.invoke_path_template:
            return ValidationReport(Readiness.MISCONFIGURED, ("Invoke path template must contain {name}.",))
        if self._config.tenant_id and context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            endpoint = require_endpoint(self._config.endpoint)
            discovery = require_endpoint(self._config.discovery_url)
            if self._config.discovery_style != "arm" and urlsplit(endpoint).netloc != urlsplit(discovery).netloc:
                raise ValueError("Non-ARM discovery must use the configured Function App origin")
            auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
            auth_headers(self._config.discovery_auth or self._config.auth, context, provider_id=PROVIDER_ID)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        return ValidationReport(Readiness.READY)

    def _policies(self) -> dict[str, FunctionPolicy]:
        return {policy.name: policy for policy in self._config.function_policies}

    def _discovery_headers(self, context: InvocationContext) -> dict[str, str]:
        return auth_headers(self._config.discovery_auth or self._config.auth, context, provider_id=PROVIDER_ID)

    def discover(self, context: InvocationContext) -> tuple[CapabilityDescriptor, ...]:
        validation = self.validate(context)
        policies = self._policies()
        if validation.readiness is not Readiness.READY:
            return tuple(
                _function_capability(
                    name,
                    policy,
                    validation.readiness,
                    "; ".join(validation.reasons),
                    ("No discovery request was sent.",),
                )
                for name, policy in policies.items()
            )
        response, _ = send(
            context,
            provider_id=PROVIDER_ID,
            method="GET",
            url=require_endpoint(self._config.discovery_url),
            headers=self._discovery_headers(context),
            idempotent=True,
            consent_on_forbidden=self._config.discovery_style == "arm",
        )
        items = collection(json_object(response, provider_id=PROVIDER_ID))
        discovered: list[CapabilityDescriptor] = []
        for item in items:
            raw_name = str(item.get("name") or item.get("id") or "")
            name = raw_name.rsplit("/", 1)[-1] if self._config.discovery_style == "arm" else raw_name
            if not name or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for character in name
            ):
                continue
            policy = policies.get(name, FunctionPolicy(name))
            discovered.append(
                _function_capability(
                    name,
                    policy,
                    Readiness.READY,
                    None,
                    (f"Function returned by successful {self._config.discovery_style} discovery.",),
                )
            )
        return tuple(discovered)

    def health(self, context: InvocationContext) -> HealthReport:
        capabilities = self.discover(context)
        validation = self.validate(context)
        readiness = Readiness.READY if validation.readiness is Readiness.READY else validation.readiness
        return HealthReport(readiness, (f"{len(capabilities)} function(s) discovered.",))

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        capability, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        name = str(capability.metadata["function_name"])
        path = self._config.invoke_path_template.format(name=name)
        headers = auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        if request.idempotency_key and operation.idempotency is not Idempotency.NONE:
            headers["Idempotency-Key"] = request.idempotency_key
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method="POST",
            url=safe_url(require_endpoint(self._config.endpoint), path),
            headers=headers,
            json_body=dict(request.arguments),
            timeout=operation.timeout_seconds,
            max_retries=operation.max_retries,
            idempotent=operation.idempotency is Idempotency.INHERENT
            or (operation.idempotency is Idempotency.REQUIRED and request.idempotency_key is not None),
            consent_on_forbidden=True,
        )
        content_type = response.headers.get("content-type", "")
        output: Any = json_object(response, provider_id=PROVIDER_ID) if "json" in content_type else response.text
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
