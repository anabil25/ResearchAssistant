"""Microsoft Graph v1.0 SharePoint and OneDrive provider."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from ._http import auth_headers, collection, json_object, require_endpoint, safe_url, send, stable_resource_id
from .config import GraphConfig
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
    ProviderValidationError,
    Readiness,
    Risk,
    UnauthorizedError,
    ValidationReport,
    audit_metadata,
    find_operation,
)

PROVIDER_ID = "microsoft_graph"
DOCS = (
    "https://learn.microsoft.com/graph/api/driveitem-list-children?view=graph-rest-1.0",
    "https://learn.microsoft.com/graph/permissions-selected-overview",
)

SITE_GET = OperationDescriptor(
    "graph.site.get",
    Maturity.GA,
    {"type": "object", "additionalProperties": False},
    {"type": "object"},
    Risk.READ,
    ApprovalPolicy.NEVER,
    idempotency=Idempotency.INHERENT,
    least_privilege_scopes=("Sites.Read.All (delegated)",),
    least_privilege_roles=("Sites.Selected (application)",),
    docs=DOCS,
)
DRIVE_LIST = OperationDescriptor(
    "graph.drive.children.list",
    Maturity.GA,
    {"type": "object", "additionalProperties": False},
    {"type": "object"},
    Risk.READ,
    ApprovalPolicy.NEVER,
    idempotency=Idempotency.INHERENT,
    least_privilege_scopes=("Files.Read (delegated)",),
    least_privilege_roles=("Files.Read.All (application)",),
    docs=DOCS,
)
ITEM_GET = OperationDescriptor(
    "graph.item.get",
    Maturity.GA,
    {"type": "object", "additionalProperties": False},
    {"type": "object"},
    Risk.READ,
    ApprovalPolicy.NEVER,
    idempotency=Idempotency.INHERENT,
    least_privilege_scopes=("Files.Read (delegated)",),
    least_privilege_roles=("Files.Read.All (application)",),
    docs=DOCS,
)
CONTENT_PUT = OperationDescriptor(
    "graph.drive.content.put",
    Maturity.GA,
    {
        "type": "object",
        "required": ["path", "content_base64"],
        "properties": {"path": {"type": "string"}, "content_base64": {"type": "string"}},
        "additionalProperties": False,
    },
    {"type": "object"},
    Risk.WRITE,
    ApprovalPolicy.REQUIRED,
    idempotency=Idempotency.INHERENT,
    least_privilege_scopes=("Files.ReadWrite (delegated)",),
    least_privilege_roles=("Files.ReadWrite.All (application)",),
    docs=DOCS,
)
WORK_IQ = OperationDescriptor(
    "graph.work_iq.query",
    Maturity.PREVIEW,
    {"type": "object"},
    {"type": "object"},
    Risk.READ,
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
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        PROVIDER_ID,
        capability_id,
        "microsoft_graph",
        resource_kind,
        name,
        Readiness.READY,
        True,
        (AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
        "configured Microsoft Entra tenant",
        "discovered Graph site/drive/item",
        operations,
        DOCS,
        ("Resource returned by a successful Microsoft Graph v1.0 request.",),
        metadata=metadata,
    )


class MicrosoftGraphProvider:
    def __init__(self, config: GraphConfig) -> None:
        self._config = config
        work_iq = CapabilityDescriptor(
            PROVIDER_ID,
            "graph.work_iq.preview",
            "microsoft_graph",
            "work_iq",
            "Work IQ",
            Readiness.UNAVAILABLE,
            False,
            (AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
            "configured Microsoft Entra tenant",
            "preview service",
            (WORK_IQ,),
            WORK_IQ.docs,
            ("Service status: preview; provider policy blocks attachment.",),
            unavailable_reason="Work IQ is preview and is not attachable",
        )
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "microsoft_graph",
            "Microsoft Graph",
            "Discovers SharePoint sites, drives, and items through Microsoft Graph v1.0.",
            (AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
            DOCS,
            (work_iq,),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def validate(self, context: InvocationContext) -> ValidationReport:
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

    def discover(self, context: InvocationContext) -> tuple[CapabilityDescriptor, ...]:
        validation = self.validate(context)
        if validation.readiness is not Readiness.READY:
            operation = SITE_GET
            return (
                self._descriptor.capabilities[0],
                CapabilityDescriptor(
                    PROVIDER_ID,
                    "graph.configuration",
                    "microsoft_graph",
                    "tenant",
                    "Microsoft Graph configuration",
                    validation.readiness,
                    False,
                    (self._config.auth.mode,),
                    "configured Microsoft Entra tenant",
                    "Microsoft Graph v1.0",
                    (operation,),
                    DOCS,
                    ("No Microsoft Graph discovery request was sent.",),
                    unavailable_reason="; ".join(validation.reasons),
                ),
            )
        capabilities: list[CapabilityDescriptor] = [self._descriptor.capabilities[0]]
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
                        ),
                        _capability(
                            stable_resource_id("graph.drive.write", drive_id),
                            f"{drive.get('name') or drive_id} write",
                            "drive",
                            (CONTENT_PUT,),
                            metadata={"drive_id": drive_id},
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
                            )
                        )
        return tuple(capabilities)

    def health(self, context: InvocationContext) -> HealthReport:
        capabilities = self.discover(context)
        ready = sum(item.readiness is Readiness.READY for item in capabilities)
        readiness = Readiness.READY if ready else self.validate(context).readiness
        return HealthReport(readiness, (f"{ready} GA Graph capability descriptor(s) are ready.",))

    @staticmethod
    def _safe_drive_path(path: str) -> str:
        if not path or path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/")):
            raise ProviderValidationError("Drive path is not a safe relative path", provider_id=PROVIDER_ID)
        return quote(path, safe="/")

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        capability, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        method = "GET"
        content: bytes | None = None
        if operation.operation_id == "graph.site.get":
            path = f"/sites/{quote(str(capability.metadata['site_id']), safe='')}"
        elif operation.operation_id == "graph.drive.children.list":
            path = f"/drives/{quote(str(capability.metadata['drive_id']), safe='')}/root/children"
        elif operation.operation_id == "graph.item.get":
            path = (
                f"/drives/{quote(str(capability.metadata['drive_id']), safe='')}"
                f"/items/{quote(str(capability.metadata['item_id']), safe='')}"
            )
        else:
            method = "PUT"
            drive_id = quote(str(capability.metadata["drive_id"]), safe="")
            drive_path = self._safe_drive_path(str(request.arguments["path"]))
            path = f"/drives/{drive_id}/root:/{drive_path}:/content"
            try:
                content = base64.b64decode(str(request.arguments["content_base64"]), validate=True)
            except ValueError as exc:
                raise ProviderValidationError("content_base64 is invalid", provider_id=PROVIDER_ID) from exc
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method=method,
            url=safe_url(require_endpoint(self._config.endpoint), path),
            headers=auth_headers(self._config.auth, context, provider_id=PROVIDER_ID),
            content=content,
            timeout=operation.timeout_seconds,
            max_retries=operation.max_retries,
            idempotent=True,
            consent_on_forbidden=True,
        )
        return InvocationResult(
            PROVIDER_ID,
            capability.capability_id,
            operation.operation_id,
            response.status_code,
            json_object(response, provider_id=PROVIDER_ID),
            audit_metadata(
                context,
                provider_id=PROVIDER_ID,
                capability_id=capability.capability_id,
                operation_id=operation.operation_id,
                attempts=attempts,
                response=response,
            ),
        )
