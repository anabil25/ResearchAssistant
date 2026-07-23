"""OpenAPI 3.0/3.1 operational provider with a fixed destination."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from ._http import auth_headers, json_object, require_endpoint, safe_url, send, stable_resource_id
from .config import OpenAPIConfig, OpenAPIOperationPolicy
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
    ProviderValidationError,
    Readiness,
    UnauthorizedError,
    ValidationReport,
    audit_metadata,
    capability_instance,
    find_operation,
)

PROVIDER_ID = "openapi"
DOCS = ("https://spec.openapis.org/oas/v3.1.1.html",)
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head")


def _resolve(document: Mapping[str, Any], value: Any) -> Any:
    if not isinstance(value, Mapping) or "$ref" not in value:
        return value
    reference = value["$ref"]
    if not isinstance(reference, str) or not reference.startswith("#/") or ".." in reference:
        raise ValueError("Only safe local OpenAPI references are supported")
    current: Any = document
    for part in reference[2:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, Mapping) or key not in current:
            raise ValueError("OpenAPI reference could not be resolved")
        current = current[key]
    return current


def _argument_schema(
    document: Mapping[str, Any], path_item: Mapping[str, Any], operation: Mapping[str, Any]
) -> dict[str, Any]:
    path_properties: dict[str, Any] = {}
    query_properties: dict[str, Any] = {}
    path_required: list[str] = []
    parameters = [*path_item.get("parameters", []), *operation.get("parameters", [])]
    for raw in parameters:
        parameter = _resolve(document, raw)
        if not isinstance(parameter, Mapping):
            continue
        location = parameter.get("in")
        name = parameter.get("name")
        schema = _resolve(document, parameter.get("schema", {}))
        if not isinstance(name, str) or not isinstance(schema, Mapping):
            continue
        reduced = {"type": schema.get("type", "string")}
        if location == "path":
            path_properties[name] = reduced
            if parameter.get("required") is True:
                path_required.append(name)
        elif location == "query":
            query_properties[name] = reduced
    properties: dict[str, Any] = {
        "path": {
            "type": "object",
            "properties": path_properties,
            "required": path_required,
            "additionalProperties": False,
        },
        "query": {"type": "object", "properties": query_properties, "additionalProperties": False},
    }
    request_body = _resolve(document, operation.get("requestBody", {}))
    if isinstance(request_body, Mapping):
        content = request_body.get("content", {})
        if isinstance(content, Mapping):
            media = content.get("application/json", {})
            if isinstance(media, Mapping):
                body_schema = _resolve(document, media.get("schema", {}))
                if isinstance(body_schema, Mapping):
                    properties["body"] = dict(body_schema)
    return {"type": "object", "properties": properties, "additionalProperties": False}


class OpenAPIProvider:
    def __init__(self, config: OpenAPIConfig) -> None:
        self._config = config
        self._document: dict[str, Any] | None = copy.deepcopy(config.document)
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "openapi",
            "OpenAPI",
            "Discovers operationId-bearing operations and invokes only the configured API origin.",
            (AuthMode.NONE, AuthMode.OAUTH, AuthMode.API_KEY),
            DOCS,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def validate(self, context: InvocationContext) -> ValidationReport:
        if not self._config.base_url:
            return ValidationReport(Readiness.MISCONFIGURED, ("OpenAPI base URL is not configured.",))
        if not self._config.tenant_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("OpenAPI tenant boundary is required.",))
        if self._document is None and not self._config.document_url:
            return ValidationReport(Readiness.MISCONFIGURED, ("OpenAPI document or document URL is required.",))
        if self._config.tenant_id and context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            require_endpoint(self._config.base_url)
            if self._config.document_url:
                require_endpoint(self._config.document_url)
            auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
            auth_headers(self._config.document_auth, context, provider_id=PROVIDER_ID)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        return ValidationReport(Readiness.READY)

    def _load_document(self, context: InvocationContext) -> dict[str, Any]:
        if self._document is not None:
            return self._document
        response, _ = send(
            context,
            provider_id=PROVIDER_ID,
            method="GET",
            url=require_endpoint(self._config.document_url),
            headers=auth_headers(self._config.document_auth, context, provider_id=PROVIDER_ID),
            idempotent=True,
        )
        self._document = json_object(response, provider_id=PROVIDER_ID)
        return self._document

    def _operations(self, context: InvocationContext) -> tuple[tuple[str, str, str, dict[str, Any]], ...]:
        document = self._load_document(context)
        version = document.get("openapi")
        if not isinstance(version, str) or not (version.startswith("3.0.") or version.startswith("3.1.")):
            raise ProviderValidationError("Only OpenAPI 3.0 and 3.1 documents are supported", provider_id=PROVIDER_ID)
        paths = document.get("paths")
        if not isinstance(paths, Mapping):
            raise ProviderValidationError("OpenAPI document must contain a paths object", provider_id=PROVIDER_ID)
        discovered = []
        seen: set[str] = set()
        for path, raw_path_item in paths.items():
            if not isinstance(path, str) or not path.startswith("/") or ".." in path or "://" in path:
                continue
            path_item = _resolve(document, raw_path_item)
            if not isinstance(path_item, Mapping):
                continue
            for method in HTTP_METHODS:
                operation = path_item.get(method)
                if not isinstance(operation, Mapping):
                    continue
                operation_id = operation.get("operationId")
                if not isinstance(operation_id, str) or not operation_id or operation_id in seen:
                    continue
                seen.add(operation_id)
                discovered.append(
                    (operation_id, method.upper(), path, _argument_schema(document, path_item, operation))
                )
        return tuple(discovered)

    def discover(self, context: InvocationContext) -> tuple[CapabilityInstance, ...]:
        validation = self.validate(context)
        if validation.readiness is not Readiness.READY:
            operation = OperationDescriptor(
                "openapi.invoke",
                "1.0.0",
                Maturity.GA,
                {"type": "object"},
                {},
                OperationClass.PRIVILEGED,
                ApprovalPolicy.REQUIRED,
                side_effect_destinations=(self._config.base_url or "unconfigured:openapi-endpoint",),
                docs=DOCS,
            )
            return (
                capability_instance(
                    provider_id=PROVIDER_ID,
                    instance_id="openapi.configuration",
                    family="openapi",
                    resource_kind="api",
                    name="OpenAPI endpoint",
                    readiness=validation.readiness,
                    auth_modes=(self._config.auth.mode,),
                    tenant_boundary="configured tenant",
                    data_boundary="configured base URL",
                    operations=(operation,),
                    provenance=DOCS,
                    status_evidence=("No OpenAPI document was loaded.",),
                    unavailable_reason="; ".join(validation.reasons),
                ),
            )
        policies = {policy.operation_id: policy for policy in self._config.operation_policies}
        capabilities = []
        for operation_id, method, path, schema in self._operations(context):
            default_class = (
                OperationClass.READ
                if method in {"GET", "HEAD"}
                else OperationClass.WRITE_IRREVERSIBLE
            )
            policy = policies.get(
                operation_id,
                OpenAPIOperationPolicy(
                    operation_id,
                    default_class,
                    ApprovalPolicy.NEVER
                    if default_class is OperationClass.READ
                    else ApprovalPolicy.REQUIRED,
                ),
            )
            idempotency = policy.idempotency
            if idempotency is None:
                idempotency = Idempotency.INHERENT if method in {"GET", "HEAD", "PUT", "DELETE"} else Idempotency.NONE
            capabilities.append(
                capability_instance(
                    provider_id=PROVIDER_ID,
                    instance_id=stable_resource_id("openapi.operation", operation_id),
                    family="openapi",
                    resource_kind="api_operation",
                    name=operation_id,
                    readiness=Readiness.READY,
                    auth_modes=(AuthMode.NONE, AuthMode.OAUTH, AuthMode.API_KEY),
                    tenant_boundary="configured tenant",
                    data_boundary="configured base URL; document server values are not invocation authority",
                    operations=(
                        OperationDescriptor(
                            operation_id,
                            "1.0.0",
                            Maturity.GA,
                            schema,
                            {},
                            policy.operation_class,
                            policy.approval_policy,
                            side_effect_destinations=()
                            if policy.operation_class
                            in {OperationClass.PURE, OperationClass.READ}
                            else (self._config.base_url or "unconfigured:openapi-endpoint",),
                            idempotency=idempotency,
                            docs=DOCS,
                        ),
                    ),
                    provenance=DOCS,
                    status_evidence=("operationId found in a validated OpenAPI 3.x document.",),
                    configuration={"method": method, "path": path, "source": "untrusted_openapi_document"},
                )
            )
        return tuple(capabilities)

    def health(self, context: InvocationContext) -> HealthReport:
        capabilities = self.discover(context)
        ready = all(item.readiness is Readiness.READY for item in capabilities)
        return HealthReport(
            Readiness.READY if ready else capabilities[0].readiness,
            (f"{len(capabilities)} operationId-bearing operation(s) discovered.",),
        )

    @staticmethod
    def _render_path(template: str, values: Mapping[str, Any]) -> str:
        rendered = template
        for name in {part.split("}", 1)[0] for part in template.split("{")[1:]}:
            if name not in values:
                raise ProviderValidationError(f"Path parameter {name} is required", provider_id=PROVIDER_ID)
            rendered = rendered.replace("{" + name + "}", quote(str(values[name]), safe=""))
        if "{" in rendered or "}" in rendered:
            raise ProviderValidationError("OpenAPI path template is malformed", provider_id=PROVIDER_ID)
        return rendered

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        instance, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        method = str(instance.configuration["method"])
        path = self._render_path(str(instance.configuration["path"]), request.arguments.get("path", {}))
        headers = auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        if request.idempotency_key and operation.idempotency is not Idempotency.NONE:
            headers["Idempotency-Key"] = request.idempotency_key
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method=method,
            url=safe_url(require_endpoint(self._config.base_url), path),
            headers=headers,
            params=request.arguments.get("query"),
            json_body=request.arguments.get("body"),
            timeout=operation.timeout_seconds,
            max_retries=operation.max_retries,
            idempotent=operation.idempotency is Idempotency.INHERENT
            or (
                operation.idempotency in {Idempotency.OPTIONAL, Idempotency.REQUIRED}
                and request.idempotency_key is not None
            ),
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
