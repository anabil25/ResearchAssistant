"""Azure Functions HTTP provider with configurable discovery style."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ._http import auth_headers, collection, json_object, require_endpoint, safe_url, send, stable_resource_id
from .config import FunctionPolicy, FunctionsConfig
from .contracts import (
    AuthMode,
    CapabilityBinding,
    CapabilityInstance,
    CapabilityRecord,
    DiscoveryResult,
    HealthReport,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    OperationDescriptor,
    ProviderDescriptor,
    Readiness,
    UnauthorizedError,
    ValidationReport,
    audit_metadata,
    capability_instance,
    discovery_result,
    find_operation,
    health_for_target,
    official_provenance,
    operation_allows_retry,
    validation_for_target,
)

PROVIDER_ID = "azure_functions"
DOCS = (
    "https://learn.microsoft.com/azure/azure-functions/functions-bindings-http-webhook-trigger",
    "https://learn.microsoft.com/azure/azure-functions/function-keys-how-to",
)
PROVENANCE = official_provenance(
    DOCS,
    source_version="Azure Functions HTTP trigger GA",
    last_verified_at="2026-07-23T08:37:02Z",
)


def _function_capability(
    name: str,
    policy: FunctionPolicy,
    readiness: Readiness,
    reason: str | None,
    evidence: tuple[str, ...],
    destination: str,
) -> CapabilityRecord:
    return capability_instance(
        provider_id=PROVIDER_ID,
        instance_id=stable_resource_id("functions.http", name),
        family="azure_functions",
        resource_kind="http_function",
        name=name,
        readiness=readiness,
        auth_modes=(AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.API_KEY),
        tenant_boundary="configured Microsoft Entra tenant",
        data_boundary="configured Function App endpoint",
        resource_id=name,
        operations=(
            OperationDescriptor(
                operation_id="functions.http.invoke",
                version="1.0.0",
                maturity=policy.maturity,
                input_schema={"type": "object"},
                output_schema={},
                operation_class=policy.operation_class,
                approval_policy=policy.approval_policy,
                external_side_effect=True,
                side_effect_destinations=(destination or "unconfigured:function-app",),
                idempotency=policy.idempotency,
                least_privilege_scopes=("Function App application scope",),
                docs=DOCS,
            ),
        ),
        provenance=PROVENANCE,
        status_evidence=evidence,
        unavailable_reason=reason,
        configuration={"function_name": name},
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
            PROVENANCE,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _validate_configuration(self, context: InvocationContext) -> ValidationReport:
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

    def _discover_instances(self, context: InvocationContext) -> tuple[CapabilityRecord, ...]:
        validation = self._validate_configuration(context)
        policies = self._policies()
        if validation.readiness is not Readiness.READY:
            if not policies:
                policies = {"unconfigured": FunctionPolicy("unconfigured")}
            return tuple(
                _function_capability(
                    name,
                    policy,
                    validation.readiness,
                    "; ".join(validation.reasons),
                    ("No discovery request was sent.",),
                    self._config.endpoint or "unconfigured:function-app",
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
        discovered: list[CapabilityRecord] = []
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
                    self._config.endpoint or "unconfigured:function-app",
                )
            )
        return tuple(discovered)

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
            policy_ref=context.policy_release,
        )

    def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport:
        return health_for_target(
            self.discover(context),
            target,
            provider_id=PROVIDER_ID,
            policy_ref=context.policy_release,
        )

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        instance, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        name = str(instance.configuration["function_name"])
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
            idempotent=operation_allows_retry(
                operation,
                idempotency_key=request.idempotency_key,
            ),
            consent_on_forbidden=True,
        )
        content_type = response.headers.get("content-type", "")
        output: Any = json_object(response, provider_id=PROVIDER_ID) if "json" in content_type else response.text
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
