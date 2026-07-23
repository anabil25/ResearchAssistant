"""Microsoft Graph v1.0 SharePoint and OneDrive provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from urllib.parse import quote

from ._http import (
    auth_headers,
    base64_encoded_length,
    binding_safe_endpoint,
    collection,
    decode_base64_limited,
    json_object,
    require_endpoint,
    safe_url,
    send,
    stable_resource_id,
)
from .config import AuthConfig, GraphConfig
from .contracts import (
    ApprovalPolicy,
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
    health_for_target,
    official_provenance,
    operation_allows_retry,
    validation_for_target,
)

PROVIDER_ID = "microsoft_graph"
DOCS = (
    "https://learn.microsoft.com/graph/api/driveitem-list-children?view=graph-rest-1.0",
    "https://learn.microsoft.com/graph/api/driveitem-put-content?view=graph-rest-1.0",
    "https://learn.microsoft.com/graph/permissions-selected-overview",
)
PROVENANCE = official_provenance(
    DOCS,
    source_version="Microsoft Graph v1.0",
    last_verified_at="2026-07-23T08:37:02Z",
)

SITE_GET = OperationDescriptor(
    "graph.site.get",
    "1.0.0",
    Maturity.GA,
    {"type": "object", "additionalProperties": False},
    {"type": "object"},
    OperationClass.READ,
    ApprovalPolicy.NEVER,
    idempotency=Idempotency.PROVIDER_NATIVE,
    least_privilege_scopes=("Sites.Read.All (delegated)",),
    least_privilege_roles=("Sites.Selected (application)",),
    docs=DOCS,
)
DRIVE_LIST = OperationDescriptor(
    "graph.drive.children.list",
    "1.0.0",
    Maturity.GA,
    {"type": "object", "additionalProperties": False},
    {"type": "object"},
    OperationClass.READ,
    ApprovalPolicy.NEVER,
    idempotency=Idempotency.PROVIDER_NATIVE,
    least_privilege_scopes=("Files.Read (delegated)",),
    least_privilege_roles=("Files.Read.All (application)",),
    docs=DOCS,
)
ITEM_GET = OperationDescriptor(
    "graph.item.get",
    "1.0.0",
    Maturity.GA,
    {"type": "object", "additionalProperties": False},
    {"type": "object"},
    OperationClass.READ,
    ApprovalPolicy.NEVER,
    idempotency=Idempotency.PROVIDER_NATIVE,
    least_privilege_scopes=("Files.Read (delegated)",),
    least_privilege_roles=("Files.Read.All (application)",),
    docs=DOCS,
)


def _content_put(max_upload_bytes: int) -> OperationDescriptor:
    return OperationDescriptor(
        "graph.drive.content.put",
        "1.1.0",
        Maturity.GA,
        {
            "type": "object",
            "required": ["path", "content_base64"],
            "properties": {
                "path": {"type": "string"},
                "content_base64": {
                    "type": "string",
                    "maxLength": base64_encoded_length(max_upload_bytes),
                },
            },
            "additionalProperties": False,
        },
        {"type": "object"},
        OperationClass.WRITE_IRREVERSIBLE,
        ApprovalPolicy.REQUIRED,
        idempotency=Idempotency.PROVIDER_NATIVE,
        least_privilege_scopes=("Files.ReadWrite (delegated)",),
        least_privilege_roles=("Files.ReadWrite.All (application)",),
        docs=DOCS,
    )


WORK_IQ = OperationDescriptor(
    "graph.work_iq.query",
    "1.0.0",
    Maturity.PREVIEW,
    {"type": "object"},
    {"type": "object"},
    OperationClass.READ,
    ApprovalPolicy.NEVER,
    docs=("https://learn.microsoft.com/microsoft-365/copilot/extensibility/work-iq/api-overview",),
)


def _capability(
    capability_id: str,
    name: str,
    resource_kind: str,
    operations: tuple[OperationDescriptor, ...],
    *,
    metadata: Mapping[str, Any],
    endpoint: str,
    max_upload_bytes: int,
    auth: AuthConfig,
) -> CapabilityRecord:
    safe_endpoint, endpoint_digest = binding_safe_endpoint(
        endpoint,
        invalid_label="invalid:graph-endpoint",
    )
    drive_id = metadata.get("drive_id")
    has_upload = any(operation.operation_id == "graph.drive.content.put" for operation in operations)
    configuration = dict(metadata)
    configuration["provider_endpoint"] = safe_endpoint
    configuration["provider_endpoint_digest"] = endpoint_digest
    if has_upload:
        configuration["max_upload_bytes"] = max_upload_bytes
    bound_operations = tuple(
        replace(
            operation,
            external_side_effect=True,
            side_effect_destinations=(
                f"{safe_endpoint}/drives/{quote(str(drive_id), safe='')}/root"
                f"#endpoint-sha256={endpoint_digest}",
            ),
        )
        if operation.operation_class is OperationClass.WRITE_IRREVERSIBLE
        else operation
        for operation in operations
    )
    return capability_instance(
        provider_id=PROVIDER_ID,
        instance_id=capability_id,
        family="microsoft_graph",
        resource_kind=resource_kind,
        name=name,
        readiness=Readiness.READY,
        auth_modes=(AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
        tenant_boundary="configured Microsoft Entra tenant",
        data_boundary="discovered Graph site/drive/item",
        resource_id=str(
            metadata.get("item_id") or metadata.get("drive_id") or metadata.get("site_id") or capability_id
        ),
        operations=bound_operations,
        provenance=PROVENANCE,
        status_evidence=("Resource returned by a successful Microsoft Graph v1.0 request.",),
        configuration=configuration,
        descriptor_metadata={"request_limits": {"max_upload_bytes": max_upload_bytes}} if has_upload else {},
        descriptor_version="1.1.0" if has_upload else "1.0.0",
        selected_auth_mode=auth.mode,
        connection_id=auth.connection_ref,
        connection_scopes=auth.connection_scopes,
    )


class MicrosoftGraphProvider:
    def __init__(self, config: GraphConfig) -> None:
        self._config = config
        self._work_iq = capability_instance(
            provider_id=PROVIDER_ID,
            instance_id="graph.work_iq.preview",
            family="microsoft_graph",
            resource_kind="work_iq",
            name="Work IQ",
            readiness=Readiness.UNAVAILABLE,
            auth_modes=(AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
            tenant_boundary="configured Microsoft Entra tenant",
            data_boundary="preview service",
            resource_id="work_iq",
            operations=(WORK_IQ,),
            provenance=official_provenance(
                WORK_IQ.docs,
                source_version="Work IQ preview",
                last_verified_at="2026-07-23T08:37:02Z",
            ),
            status_evidence=("Service status: preview; provider policy blocks attachment.",),
            unavailable_reason="Work IQ is preview and is not attachable",
            selected_auth_mode=config.auth.mode,
            connection_id=config.auth.connection_ref,
            connection_scopes=config.auth.connection_scopes,
        )
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "microsoft_graph",
            "Microsoft Graph",
            "Discovers SharePoint sites, drives, and items through Microsoft Graph v1.0.",
            (AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
            PROVENANCE,
            (self._work_iq.descriptor,),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _validate_configuration(self, context: InvocationContext) -> ValidationReport:
        if not self._config.endpoint:
            return ValidationReport(Readiness.MISCONFIGURED, ("Microsoft Graph endpoint is not configured.",))
        if not self._config.tenant_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("Microsoft Graph tenant boundary is required.",))
        if context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            require_endpoint(self._config.endpoint)
            auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        return ValidationReport(Readiness.READY)

    def _get(self, path: str, context: InvocationContext) -> dict[str, Any]:
        response, _ = send(
            context,
            provider_id=PROVIDER_ID,
            method="GET",
            url=safe_url(require_endpoint(self._config.endpoint), path),
            headers=auth_headers(self._config.auth, context, provider_id=PROVIDER_ID),
            idempotent=True,
            consent_on_forbidden=True,
        )
        return json_object(response, provider_id=PROVIDER_ID)

    def _discover_instances(self, context: InvocationContext) -> tuple[CapabilityRecord, ...]:
        validation = self._validate_configuration(context)
        safe_endpoint, endpoint_digest = binding_safe_endpoint(
            self._config.endpoint,
            invalid_label="invalid:graph-endpoint",
        )
        if validation.readiness is not Readiness.READY:
            operation = SITE_GET
            return (
                self._work_iq,
                capability_instance(
                    provider_id=PROVIDER_ID,
                    instance_id="graph.configuration",
                    family="microsoft_graph",
                    resource_kind="tenant",
                    name="Microsoft Graph configuration",
                    readiness=validation.readiness,
                    auth_modes=(self._config.auth.mode,),
                    tenant_boundary="configured Microsoft Entra tenant",
                    data_boundary="Microsoft Graph v1.0",
                    resource_id="graph",
                    operations=(operation,),
                    provenance=PROVENANCE,
                    status_evidence=("No Microsoft Graph discovery request was sent.",),
                    unavailable_reason="; ".join(validation.reasons),
                    configuration={
                        "provider_endpoint": safe_endpoint,
                        "provider_endpoint_digest": endpoint_digest,
                    },
                    selected_auth_mode=self._config.auth.mode,
                    connection_id=self._config.auth.connection_ref,
                    connection_scopes=self._config.auth.connection_scopes,
                ),
            )
        capabilities: list[CapabilityRecord] = [self._work_iq]
        sites = collection(self._get(self._config.sites_path, context))
        for site in sites:
            site_id = str(site.get("id") or "")
            if not site_id:
                continue
            capabilities.append(
                _capability(
                    stable_resource_id("graph.site", site_id),
                    str(site.get("displayName") or site.get("name") or site_id),
                    "sharepoint_site",
                    (SITE_GET,),
                    metadata={"site_id": site_id},
                    endpoint=require_endpoint(self._config.endpoint),
                    max_upload_bytes=self._config.max_upload_bytes,
                    auth=self._config.auth,
                )
            )
            drives = collection(self._get(f"/sites/{quote(site_id, safe='')}/drives", context))
            for drive in drives:
                drive_id = str(drive.get("id") or "")
                if not drive_id:
                    continue
                capabilities.extend(
                    (
                        _capability(
                            stable_resource_id("graph.drive.read", drive_id),
                            str(drive.get("name") or drive_id),
                            "drive",
                            (DRIVE_LIST,),
                            metadata={"drive_id": drive_id},
                            endpoint=require_endpoint(self._config.endpoint),
                            max_upload_bytes=self._config.max_upload_bytes,
                            auth=self._config.auth,
                        ),
                        _capability(
                            stable_resource_id("graph.drive.write", drive_id),
                            f"{drive.get('name') or drive_id} write",
                            "drive",
                            (_content_put(self._config.max_upload_bytes),),
                            metadata={"drive_id": drive_id},
                            endpoint=require_endpoint(self._config.endpoint),
                            max_upload_bytes=self._config.max_upload_bytes,
                            auth=self._config.auth,
                        ),
                    )
                )
                if not self._config.discover_items:
                    continue
                items = collection(self._get(f"/drives/{quote(drive_id, safe='')}/root/children", context))
                for item in items:
                    item_id = str(item.get("id") or "")
                    if item_id:
                        capabilities.append(
                            _capability(
                                stable_resource_id("graph.item", f"{drive_id}:{item_id}"),
                                str(item.get("name") or item_id),
                                "drive_item",
                                (ITEM_GET,),
                                metadata={"drive_id": drive_id, "item_id": item_id},
                                endpoint=require_endpoint(self._config.endpoint),
                                max_upload_bytes=self._config.max_upload_bytes,
                                auth=self._config.auth,
                            )
                        )
        return tuple(capabilities)

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

    @staticmethod
    def _safe_drive_path(path: str) -> str:
        if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ProviderValidationError("Drive path is not a safe relative path", provider_id=PROVIDER_ID)
        return quote(path, safe="/")

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        instance, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        method = "GET"
        content: bytes | None = None
        if operation.operation_id == "graph.site.get":
            path = f"/sites/{quote(str(instance.configuration['site_id']), safe='')}"
        elif operation.operation_id == "graph.drive.children.list":
            path = f"/drives/{quote(str(instance.configuration['drive_id']), safe='')}/root/children"
        elif operation.operation_id == "graph.item.get":
            path = (
                f"/drives/{quote(str(instance.configuration['drive_id']), safe='')}"
                f"/items/{quote(str(instance.configuration['item_id']), safe='')}"
            )
        else:
            method = "PUT"
            drive_id = quote(str(instance.configuration["drive_id"]), safe="")
            drive_path = self._safe_drive_path(str(request.arguments["path"]))
            path = f"/drives/{drive_id}/root:/{drive_path}:/content"
            content = decode_base64_limited(
                str(request.arguments["content_base64"]),
                max_bytes=int(instance.configuration["max_upload_bytes"]),
                provider_id=PROVIDER_ID,
            )
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method=method,
            url=safe_url(require_endpoint(self._config.endpoint), path),
            headers=auth_headers(self._config.auth, context, provider_id=PROVIDER_ID),
            content=content,
            timeout=operation.timeout_seconds,
            max_retries=operation.max_retries,
            idempotent=operation_allows_retry(
                operation,
                idempotency_key=request.idempotency_key,
            ),
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
