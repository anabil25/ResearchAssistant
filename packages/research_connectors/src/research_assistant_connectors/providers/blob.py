"""Azure Blob Storage REST provider."""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any
from urllib.parse import quote

import httpx
from defusedxml import ElementTree

from ._http import (
    auth_headers,
    request_signing_credential,
    require_endpoint,
    safe_url,
    send,
    stable_resource_id,
)
from .config import BlobConfig
from .contracts import (
    ApprovalPolicy,
    AuthMode,
    CapabilityBinding,
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
    ProviderValidationError,
    Readiness,
    UnauthorizedError,
    ValidationReport,
    audit_metadata,
    capability_instance,
    discovery_result,
    find_operation,
    official_provenance,
    operation_allows_retry,
    resolve_capability_target,
)

PROVIDER_ID = "azure_blob_storage"
DOCS = (
    "https://learn.microsoft.com/rest/api/storageservices/blob-service-rest-api",
    "https://learn.microsoft.com/rest/api/storageservices/versioning-for-the-azure-storage-services",
)
PROVENANCE = official_provenance(
    DOCS,
    source_version="Blob service REST 2025-11-05",
    last_verified_at="2026-07-23T08:37:02Z",
)


def _operation(
    operation_id: str,
    operation_class: OperationClass,
    input_schema: dict[str, Any],
    idempotency: Idempotency,
) -> OperationDescriptor:
    return OperationDescriptor(
        operation_id,
        "1.0.0",
        Maturity.GA,
        input_schema,
        {},
        operation_class,
        ApprovalPolicy.REQUIRED if operation_class is OperationClass.WRITE_IRREVERSIBLE else ApprovalPolicy.NEVER,
        idempotency=idempotency,
        least_privilege_scopes=("https://storage.azure.com/.default",),
        least_privilege_roles=("Storage Blob Data Reader",)
        if operation_class is OperationClass.READ
        else ("Storage Blob Data Contributor",),
        docs=DOCS,
    )


LIST = _operation(
    "blob.blobs.list",
    OperationClass.READ,
    {
        "type": "object",
        "properties": {"prefix": {"type": "string"}},
        "additionalProperties": False,
    },
    Idempotency.INHERENT,
)
GET = _operation(
    "blob.get",
    OperationClass.READ,
    {"type": "object", "required": ["blob"], "properties": {"blob": {"type": "string"}}, "additionalProperties": False},
    Idempotency.INHERENT,
)
PUT = _operation(
    "blob.put",
    OperationClass.WRITE_IRREVERSIBLE,
    {
        "type": "object",
        "required": ["blob", "content_base64"],
        "properties": {
            "blob": {"type": "string"},
            "content_base64": {"type": "string"},
            "content_type": {"type": "string"},
        },
        "additionalProperties": False,
    },
    Idempotency.INHERENT,
)


def _container_capability(
    name: str,
    readiness: Readiness,
    reason: str | None,
    evidence: tuple[str, ...],
    endpoint: str,
) -> CapabilityInstance:
    return capability_instance(
        provider_id=PROVIDER_ID,
        instance_id=stable_resource_id("blob.container", name),
        family="azure_storage",
        resource_kind="blob_container",
        name=name,
        readiness=readiness,
        auth_modes=(AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.SHARED_KEY),
        tenant_boundary="configured Microsoft Entra tenant",
        data_boundary="configured storage account and discovered container",
        operations=(
            LIST,
            GET,
            replace(
                PUT,
                external_side_effect=True,
                side_effect_destinations=(f"{endpoint.rstrip('/')}/{quote(name, safe='')}",),
            ),
        ),
        provenance=PROVENANCE,
        status_evidence=evidence,
        unavailable_reason=reason,
        configuration={"container": name},
    )


class AzureBlobProvider:
    def __init__(self, config: BlobConfig) -> None:
        self._config = config
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "azure_storage",
            "Azure Blob Storage",
            "Discovers containers and performs bounded blob list, GET, and idempotent PUT operations.",
            (AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.SHARED_KEY),
            PROVENANCE,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _headers(
        self,
        context: InvocationContext,
        *,
        method: str,
        url: str,
        content_length: int = 0,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "x-ms-date": format_datetime(datetime.now(tz=UTC), usegmt=True),
            "x-ms-version": self._config.api_version,
        }
        if extra_headers:
            headers.update(extra_headers)
        if self._config.auth.mode is AuthMode.SHARED_KEY:
            signer = request_signing_credential(context, provider_id=PROVIDER_ID)
            headers["Authorization"] = signer.authorization(
                method=method,
                url=url,
                headers=headers,
                content_length=content_length,
            )
        else:
            headers.update(auth_headers(self._config.auth, context, provider_id=PROVIDER_ID))
        return headers

    def _validate_configuration(self, context: InvocationContext) -> ValidationReport:
        if not self._config.endpoint:
            return ValidationReport(Readiness.MISCONFIGURED, ("Blob endpoint is not configured.",))
        if not self._config.tenant_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("Blob tenant boundary is required.",))
        if self._config.tenant_id and context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            endpoint = require_endpoint(self._config.endpoint)
            self._headers(context, method="GET", url=endpoint)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        return ValidationReport(Readiness.READY)

    @staticmethod
    def _xml_names(content: bytes, tag: str) -> tuple[str, ...]:
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise ProviderValidationError("Storage returned invalid XML", provider_id=PROVIDER_ID) from exc
        return tuple(element.text for element in root.findall(f".//{tag}") if element.text)

    def _discover_instances(self, context: InvocationContext) -> tuple[CapabilityInstance, ...]:
        validation = self._validate_configuration(context)
        if validation.readiness is not Readiness.READY:
            return (
                _container_capability(
                    "unconfigured",
                    validation.readiness,
                    "; ".join(validation.reasons),
                    ("No container discovery request was sent.",),
                    self._config.endpoint or "unconfigured:blob-endpoint",
                ),
            )
        endpoint = require_endpoint(self._config.endpoint)
        list_params = {"comp": "list"}
        signed_url = str(httpx.URL(endpoint, params=list_params))
        response, _ = send(
            context,
            provider_id=PROVIDER_ID,
            method="GET",
            url=endpoint,
            headers=self._headers(context, method="GET", url=signed_url),
            params=list_params,
            idempotent=True,
            consent_on_forbidden=True,
        )
        return tuple(
            _container_capability(
                name,
                Readiness.READY,
                None,
                ("Container returned by successful Blob service discovery.",),
                endpoint,
            )
            for name in self._xml_names(response.content, "Container/Name")
        )

    def discover(self, context: InvocationContext) -> DiscoveryResult:
        return discovery_result(self._discover_instances(context))

    def validate(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> ValidationReport:
        instance, _ = resolve_capability_target(self.discover(context), target, provider_id=PROVIDER_ID)
        return ValidationReport(
            instance.readiness,
            () if instance.readiness is Readiness.READY else (instance.unavailable_reason or "Not ready",),
        )

    def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport:
        instance, _ = resolve_capability_target(self.discover(context), target, provider_id=PROVIDER_ID)
        return HealthReport(instance.health or instance.readiness, instance.status_evidence)

    @staticmethod
    def _blob_path(container: str, blob: str) -> str:
        if not blob or blob.startswith("/") or any(part in {"", ".", ".."} for part in blob.split("/")):
            raise ProviderValidationError("Blob name is not a safe relative path", provider_id=PROVIDER_ID)
        return f"/{quote(container, safe='')}/{quote(blob, safe='/')}"

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        instance, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        endpoint = require_endpoint(self._config.endpoint)
        container = str(instance.configuration["container"])
        params: dict[str, Any] | None = None
        content: bytes | None = None
        method = "GET"
        if operation.operation_id == "blob.blobs.list":
            url = safe_url(endpoint, f"/{quote(container, safe='')}")
            params = {"restype": "container", "comp": "list"}
            if prefix := request.arguments.get("prefix"):
                params["prefix"] = str(prefix)
        else:
            url = safe_url(endpoint, self._blob_path(container, str(request.arguments["blob"])))
        if operation.operation_id == "blob.put":
            method = "PUT"
            try:
                content = base64.b64decode(str(request.arguments["content_base64"]), validate=True)
            except ValueError as exc:
                raise ProviderValidationError("content_base64 is invalid", provider_id=PROVIDER_ID) from exc
        signed_url = str(httpx.URL(url, params=params)) if params else url
        extra_headers = None
        if operation.operation_id == "blob.put":
            extra_headers = {
                "x-ms-blob-type": "BlockBlob",
                "Content-Type": str(request.arguments.get("content_type", "application/octet-stream")),
            }
        headers = self._headers(
            context,
            method=method,
            url=signed_url,
            content_length=len(content or b""),
            extra_headers=extra_headers,
        )
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method=method,
            url=url,
            headers=headers,
            params=params,
            content=content,
            timeout=operation.timeout_seconds,
            max_retries=operation.max_retries,
            idempotent=operation_allows_retry(
                operation,
                idempotency_key=request.idempotency_key,
            ),
            consent_on_forbidden=True,
        )
        if operation.operation_id == "blob.blobs.list":
            output: Any = {"blobs": self._xml_names(response.content, "Blob/Name")}
        elif operation.operation_id == "blob.get":
            output = {
                "content_base64": base64.b64encode(response.content).decode(),
                "etag": response.headers.get("etag"),
            }
        else:
            output = {"etag": response.headers.get("etag"), "version_id": response.headers.get("x-ms-version-id")}
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
