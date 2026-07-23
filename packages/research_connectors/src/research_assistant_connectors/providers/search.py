"""Azure AI Search operational provider."""

from __future__ import annotations

from urllib.parse import quote

from ._http import auth_headers, collection, json_object, require_endpoint, safe_url, send, stable_resource_id
from .config import SearchConfig
from .contracts import (
    ApprovalPolicy,
    AuthMode,
    CapabilityInstance,
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
    find_operation,
)

PROVIDER_ID = "azure_ai_search"
DOCS = (
    "https://learn.microsoft.com/rest/api/searchservice/indexes/list",
    "https://learn.microsoft.com/rest/api/searchservice/documents/search-post",
)
SEARCH_INPUT = {
    "type": "object",
    "required": ["search"],
    "properties": {
        "search": {"type": "string"},
        "top": {"type": "integer"},
        "filter": {"type": "string"},
        "select": {"type": "string"},
    },
    "additionalProperties": False,
}


def _search_capability(
    capability_id: str,
    name: str,
    readiness: Readiness,
    *,
    reason: str | None,
    evidence: tuple[str, ...],
    index_name: str | None = None,
) -> CapabilityInstance:
    return capability_instance(
        provider_id=PROVIDER_ID,
        instance_id=capability_id,
        family="azure_ai_search",
        resource_kind="search_index",
        name=name,
        readiness=readiness,
        auth_modes=(AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.API_KEY),
        tenant_boundary="configured Microsoft Entra tenant",
        data_boundary="configured Search service and discovered index",
        operations=(
            OperationDescriptor(
                operation_id="search.documents.query",
                version="1.0.0",
                maturity=Maturity.GA,
                input_schema=SEARCH_INPUT,
                output_schema={"type": "object"},
                operation_class=OperationClass.READ,
                approval_policy=ApprovalPolicy.NEVER,
                idempotency=Idempotency.INHERENT,
                least_privilege_scopes=("https://search.azure.com/.default",),
                least_privilege_roles=("Search Index Data Reader",),
                docs=DOCS,
            ),
        ),
        provenance=DOCS,
        status_evidence=evidence,
        unavailable_reason=reason,
        configuration={"index_name": index_name} if index_name else {},
    )


class AzureAISearchProvider:
    def __init__(self, config: SearchConfig) -> None:
        self._config = config
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "azure_ai_search",
            "Azure AI Search",
            "Discovers indexes and executes bounded document search requests.",
            (AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.API_KEY),
            DOCS,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def validate(self, context: InvocationContext) -> ValidationReport:
        if not self._config.endpoint:
            return ValidationReport(Readiness.MISCONFIGURED, ("Search endpoint is not configured.",))
        if not self._config.tenant_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("Search tenant boundary is required.",))
        if self._config.tenant_id and context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            require_endpoint(self._config.endpoint)
            auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        return ValidationReport(Readiness.READY)

    def discover(self, context: InvocationContext) -> tuple[CapabilityInstance, ...]:
        validation = self.validate(context)
        if validation.readiness is not Readiness.READY:
            return (
                _search_capability(
                    "search.indexes",
                    "Azure AI Search indexes",
                    validation.readiness,
                    reason="; ".join(validation.reasons),
                    evidence=("No index discovery request was sent.",),
                ),
            )
        endpoint = require_endpoint(self._config.endpoint)
        response, _ = send(
            context,
            provider_id=PROVIDER_ID,
            method="GET",
            url=safe_url(endpoint, "/indexes"),
            headers=auth_headers(self._config.auth, context, provider_id=PROVIDER_ID),
            params={"api-version": self._config.api_version, "$select": "name"},
            idempotent=True,
            consent_on_forbidden=True,
        )
        indexes = collection(json_object(response, provider_id=PROVIDER_ID))
        return tuple(
            _search_capability(
                stable_resource_id("search.index", name),
                name,
                Readiness.READY,
                reason=None,
                evidence=("Index returned by successful Search service discovery.",),
                index_name=name,
            )
            for item in indexes
            if (name := str(item.get("name") or ""))
        )

    def health(self, context: InvocationContext) -> HealthReport:
        capabilities = self.discover(context)
        return HealthReport(
            Readiness.READY
            if capabilities and all(item.readiness is Readiness.READY for item in capabilities)
            else Readiness.DEGRADED,
            (f"{len(capabilities)} index capability descriptor(s) discovered.",),
        )

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        instance, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        index_name = instance.configuration["index_name"]
        endpoint = require_endpoint(self._config.endpoint)
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method="POST",
            url=safe_url(endpoint, f"/indexes/{quote(str(index_name), safe='')}/docs/search"),
            headers=auth_headers(self._config.auth, context, provider_id=PROVIDER_ID),
            params={"api-version": self._config.api_version},
            json_body=dict(request.arguments),
            timeout=operation.timeout_seconds,
            max_retries=operation.max_retries,
            idempotent=True,
            consent_on_forbidden=True,
        )
        return InvocationResult(
            PROVIDER_ID,
            instance.instance_id,
            operation.operation_id,
            response.status_code,
            json_object(response, provider_id=PROVIDER_ID),
            audit_metadata(
                context,
                provider_id=PROVIDER_ID,
                instance_id=instance.instance_id,
                operation_id=operation.operation_id,
                attempts=attempts,
                response=response,
            ),
        )
