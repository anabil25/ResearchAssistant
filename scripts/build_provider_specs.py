"""Build the committed OpenAPI specification set for every onboarded provider.

Providers that publish a machine-readable specification are fetched into
``infra/provider-specs/official``. The remainder are generated from the reviewed
catalog into ``infra/provider-specs/authored``. Both are normalized for API
Management import: delete operations are removed, the documented backend is
pinned, and every operation is given a stable operationId, which APIM requires
before an operation can be attached to an MCP server as a tool.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml
from research_assistant_core.provider_api_catalog import ProviderApi, provider_apis

ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = ROOT / "infra" / "provider-specs"
MANIFEST = SPEC_DIR / "manifest.json"

_HTTP_METHODS = {"get", "post", "put", "patch", "head", "options"}
_EXCLUDED_METHODS = {"delete"}
# API Management imports OpenAPI 3.0.x; 3.1 documents are pinned down on import.
_MAX_OPENAPI = "3.0.3"


def _load_document(body: bytes) -> dict[str, Any]:
    try:
        parsed: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        parsed = yaml.safe_load(body)
    if not isinstance(parsed, dict) or "paths" not in parsed:
        raise ValueError("Fetched document is not an OpenAPI or Swagger specification")
    return parsed


def _fallback_operation_id(provider: ProviderApi, method: str, path: str) -> str:
    words = [segment for segment in re.split(r"[^A-Za-z0-9]+", path) if segment]
    suffix = "".join(word[:1].upper() + word[1:] for word in words)
    prefix = re.sub(r"[^a-z0-9]", "", provider.connector_id.split("_")[0].lower())
    return f"{prefix}{method.capitalize()}{suffix}"


def _sanitize_operation_id(candidate: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", candidate).strip("-")
    return cleaned or "operation"


def _resolve_ref(document: dict[str, Any], ref: str) -> dict[str, Any] | None:
    if not ref.startswith("#/"):
        return None
    node: Any = document
    for segment in ref[2:].split("/"):
        segment = segment.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node if isinstance(node, dict) else None


def _inline_parameters(document: dict[str, Any], operation: dict[str, Any]) -> None:
    parameters = operation.get("parameters")
    if not isinstance(parameters, list):
        return
    is_swagger = "swagger" in document
    inlined: list[dict[str, Any]] = []
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        if "$ref" in parameter:
            resolved = _resolve_ref(document, str(parameter["$ref"]))
            if resolved is None:
                continue
            parameter = dict(resolved)
        if not isinstance(parameter.get("name"), str) or not isinstance(parameter.get("in"), str):
            continue

        if is_swagger:
            # Swagger 2.0 types live on the parameter; only body params carry a schema.
            if parameter["in"] == "body":
                parameter["schema"] = _inline_schema(document, parameter.get("schema"))
            else:
                declared = parameter.get("type")
                if declared == "array":
                    items = parameter.get("items")
                    parameter["items"] = {"type": "string"} if not isinstance(items, dict) else items
                elif not isinstance(declared, str):
                    parameter["type"] = "string"
                parameter.pop("schema", None)
        else:
            schema = parameter.get("schema")
            schema_type = schema.get("type") if isinstance(schema, dict) else None
            parameter["schema"] = {"type": schema_type if isinstance(schema_type, str) else "string"}
        parameter.pop("examples", None)
        _dedupe_enum(parameter)
        inlined.append(parameter)
    operation["parameters"] = inlined


def _dedupe_enum(parameter: dict[str, Any]) -> None:
    """JSON Schema requires unique enum values; some published specs repeat them."""
    values = parameter.get("enum")
    if not isinstance(values, list):
        return
    unique: list[Any] = []
    for value in values:
        if value not in unique:
            unique.append(value)
    if not unique:
        parameter.pop("enum", None)
        return
    parameter["enum"] = unique
    # APIM rejects a default that is not one of the enumerated values.
    if "default" in parameter and parameter["default"] not in unique:
        parameter.pop("default", None)


# APIM's importer rejects unknown schema keywords, so only these survive.
_SCHEMA_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "items",
        "required",
        "enum",
        "format",
        "description",
        "nullable",
        "default",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
    }
)
_MAX_SCHEMA_DEPTH = 6


def _inline_schema(
    document: dict[str, Any],
    schema: Any,
    *,
    depth: int = 0,
    seen: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Dereference a request schema so APIM can derive real MCP tool inputs.

    Recursion is bounded and self-referential models collapse to a plain object
    so that cyclic provider schemas cannot hang the build.
    """
    generic: dict[str, Any] = {"type": "object"}
    if not isinstance(schema, dict) or depth > _MAX_SCHEMA_DEPTH:
        return generic

    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref in seen:
            return generic
        target = _resolve_ref(document, ref)
        if target is None:
            return generic
        return _inline_schema(document, target, depth=depth + 1, seen=(*seen, ref))

    # APIM does not accept polymorphic composition; take the first usable branch.
    for keyword in ("allOf", "oneOf", "anyOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list) and branches:
            merged: dict[str, Any] = {"type": "object", "properties": {}}
            for branch in branches if keyword == "allOf" else branches[:1]:
                resolved = _inline_schema(document, branch, depth=depth + 1, seen=seen)
                properties = resolved.get("properties")
                if isinstance(properties, dict):
                    merged["properties"].update(properties)
            return merged if merged["properties"] else generic

    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in _SCHEMA_KEYWORDS:
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {
                name: _inline_schema(document, sub, depth=depth + 1, seen=seen)
                for name, sub in value.items()
            }
        elif key == "items":
            result[key] = _inline_schema(document, value, depth=depth + 1, seen=seen)
        elif key == "required" and isinstance(value, list):
            result[key] = [name for name in value if isinstance(name, str)]
        else:
            result[key] = value

    _dedupe_enum(result)
    if "type" not in result:
        result["type"] = "object" if "properties" in result else "string"
    return result or generic


def _simplify_for_apim(document: dict[str, Any]) -> dict[str, Any]:
    """Keep operations and parameters; drop model schemas.

    APIM derives MCP tool inputs from parameters and request bodies, and its
    importer rejects the non-standard schema keywords some published provider
    specifications contain.
    """
    generic = {"type": "object"}
    for item in document.get("paths", {}).values():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            _inline_parameters(document, operation)
            if isinstance(operation.get("requestBody"), dict):
                body = operation["requestBody"]
                content = body.get("content")
                schema: Any = None
                if isinstance(content, dict):
                    for media_type, media in content.items():
                        if isinstance(media, dict) and "json" in str(media_type).lower():
                            schema = media.get("schema")
                            break
                operation["requestBody"] = {
                    "required": bool(body.get("required", False)),
                    "content": {
                        "application/json": {"schema": _inline_schema(document, schema)},
                    },
                }
            responses = operation.get("responses")
            if isinstance(responses, dict):
                operation["responses"] = {
                    "200": {
                        "description": "Successful provider response.",
                        **(
                            {"schema": dict(generic)}
                            if "swagger" in document
                            else {"content": {"application/json": {"schema": dict(generic)}}}
                        ),
                    }
                }
            operation.pop("security", None)

    document.pop("definitions", None)
    document.pop("responses", None)
    document.pop("parameters", None)
    document.pop("securityDefinitions", None)
    document.pop("security", None)
    components = document.get("components")
    if isinstance(components, dict):
        for key in ("schemas", "responses", "parameters", "requestBodies", "securitySchemes"):
            components.pop(key, None)
        if not components:
            document.pop("components", None)
    return document


def _ensure_info(provider: ProviderApi, document: dict[str, Any]) -> None:
    """Some published specifications omit fields the APIM importer requires."""
    info = document.get("info")
    if not isinstance(info, dict):
        info = {}
        document["info"] = info
    if not isinstance(info.get("title"), str) or not info["title"].strip():
        info["title"] = provider.display_name
    if not isinstance(info.get("version"), str) or not str(info["version"]).strip():
        info["version"] = "1.0.0"
    else:
        info["version"] = str(info["version"])


def _normalize(
    provider: ProviderApi,
    document: dict[str, Any],
    *,
    server_url: str | None = None,
) -> dict[str, Any]:
    backend_url = server_url or provider.server_url
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise ValueError(f"{provider.connector_id}: specification has no paths object")

    seen: set[str] = set()
    for path, item in list(paths.items()):
        if not isinstance(item, dict):
            continue
        for method in list(item):
            lowered = method.lower()
            if lowered in _EXCLUDED_METHODS:
                del item[method]
                continue
            if lowered not in _HTTP_METHODS:
                continue
            operation = item[method]
            if not isinstance(operation, dict):
                continue
            candidate = _sanitize_operation_id(
                operation.get("operationId") or _fallback_operation_id(provider, lowered, path)
            )
            unique = candidate
            counter = 2
            while unique in seen:
                unique = f"{candidate}{counter}"
                counter += 1
            seen.add(unique)
            operation["operationId"] = unique
        if not any(method.lower() in _HTTP_METHODS for method in item):
            del paths[path]

    if "swagger" in document:
        # Swagger 2.0 resolves the backend from host/basePath/schemes, not servers.
        remainder = backend_url.split("://", 1)[1]
        host, _, base_path = remainder.partition("/")
        document["host"] = host
        document["basePath"] = f"/{base_path}" if base_path else "/"
        document["schemes"] = ["https"]
    else:
        version = str(document.get("openapi", _MAX_OPENAPI))
        if version.startswith("3.1"):
            document["openapi"] = _MAX_OPENAPI
        document["servers"] = [{"url": backend_url}]
    _ensure_info(provider, document)
    return _simplify_for_apim(document)


def _build_authored(
    provider: ProviderApi,
    *,
    operations: tuple[Any, ...] | None = None,
    server_url: str | None = None,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for operation in operations if operations is not None else provider.operations:
        entry = paths.setdefault(operation.path, {})
        body: dict[str, Any] = {
            "operationId": operation.operation_id,
            "summary": operation.summary,
            "parameters": [
                {
                    "name": parameter.name,
                    "in": parameter.location,
                    "required": parameter.required or parameter.location == "path",
                    "description": parameter.description,
                    "schema": {"type": parameter.schema_type},
                }
                for parameter in operation.parameters
            ],
            "responses": {
                "200": {
                    "description": "Successful provider response.",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                }
            },
        }
        if operation.accepts_json_body:
            body["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"type": "object"}}},
            }
        entry[operation.method.lower()] = body

    return {
        "openapi": _MAX_OPENAPI,
        "info": {
            "title": title or provider.display_name,
            "version": "1.0.0",
            "description": (
                f"{description or provider.description} Authored from {provider.documentation_url}"
            ),
        },
        "servers": [{"url": server_url or provider.server_url}],
        "paths": paths,
    }


def _operation_ids(document: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in document.get("paths", {}).values():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() in _HTTP_METHODS and isinstance(operation, dict):
                ids.append(operation["operationId"])
    return ids


def build() -> list[dict[str, Any]]:
    for folder in ("official", "authored"):
        (SPEC_DIR / folder).mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    with httpx.Client(
        timeout=httpx.Timeout(60.0, connect=20.0),
        follow_redirects=True,
        headers={"Accept": "application/json, application/yaml, text/yaml, */*"},
    ) as client:
        for provider in provider_apis():
            if provider.is_published:
                response = client.get(str(provider.published_spec_url))
                response.raise_for_status()
                document = _load_document(response.content)
                source = provider.published_spec_url
            else:
                if not provider.operations:
                    raise ValueError(f"{provider.connector_id}: no published spec and no authored operations")
                document = _build_authored(provider)
                source = provider.documentation_url

            document = _normalize(provider, document)
            operation_ids = _operation_ids(document)
            if not operation_ids:
                raise ValueError(f"{provider.connector_id}: specification exposes no operations")
            if len(set(operation_ids)) != len(operation_ids):
                raise ValueError(f"{provider.connector_id}: duplicate operationId detected")

            spec_path = ROOT / provider.spec_file
            # YAML sources decode dates natively; JSON has no such type.
            serialized = json.dumps(document, indent=2, sort_keys=True, default=str)
            spec_path.write_text(serialized + "\n", encoding="utf-8")

            apis = [
                {
                    "apiId": provider.api_id,
                    "apiPath": provider.api_path,
                    "displayName": provider.display_name,
                    "specFile": provider.spec_file,
                    "apimFormat": "swagger-json" if "swagger" in document else "openapi+json",
                    "serverUrl": provider.server_url,
                    "operationIds": sorted(operation_ids),
                }
            ]
            combined = list(operation_ids)

            for secondary in provider.secondary_apis:
                secondary_document = _normalize(
                    provider,
                    _build_authored(
                        provider,
                        operations=secondary.operations,
                        server_url=secondary.server_url,
                        title=f"{provider.display_name} ({secondary.suffix})",
                        description=secondary.description,
                    ),
                    server_url=secondary.server_url,
                )
                secondary_ids = _operation_ids(secondary_document)
                overlap = set(secondary_ids) & set(combined)
                if overlap:
                    raise ValueError(f"{provider.connector_id}: duplicate operationId across hosts: {sorted(overlap)}")
                secondary_path = ROOT / provider.secondary_spec_file(secondary.suffix)
                secondary_path.write_text(
                    json.dumps(secondary_document, indent=2, sort_keys=True, default=str) + "\n",
                    encoding="utf-8",
                )
                apis.append(
                    {
                        "apiId": provider.secondary_api_id(secondary.suffix),
                        "apiPath": provider.secondary_api_path(secondary.suffix),
                        # APIM rejects duplicate API display names service-wide.
                        "displayName": f"{provider.display_name} ({secondary.suffix})",
                        "specFile": provider.secondary_spec_file(secondary.suffix),
                        "apimFormat": "openapi+json",
                        "serverUrl": secondary.server_url,
                        "operationIds": sorted(secondary_ids),
                    }
                )
                combined.extend(secondary_ids)

            entries.append(
                {
                    "connectorId": provider.connector_id,
                    "displayName": provider.display_name,
                    "documentationUrl": provider.documentation_url,
                    "provenance": provider.spec_folder,
                    "specSource": source,
                    "specFile": provider.spec_file,
                    "apimFormat": apis[0]["apimFormat"],
                    "apiId": provider.api_id,
                    "apiPath": provider.api_path,
                    "apis": apis,
                    "mcpApiId": provider.mcp_api_id,
                    "mcpPath": provider.mcp_path,
                    "serverLabel": provider.server_label,
                    "serverUrl": provider.server_url,
                    "operationIds": sorted(combined),
                }
            )
            hosts = f" across {len(apis)} hosts" if len(apis) > 1 else ""
            print(f"{provider.connector_id:18} {provider.spec_folder:8} {len(combined):4} operations{hosts}")

    MANIFEST.write_text(json.dumps(entries, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    _assert_unique_identity(entries)
    pruned = _prune_orphaned_specs(entries)
    for path in pruned:
        print(f"pruned stale spec {path}")

    official = sum(1 for entry in entries if entry["provenance"] == "official")
    total = sum(len(entry["operationIds"]) for entry in entries)
    print(f"\n{len(entries)} providers ({official} official, {len(entries) - official} authored), {total} operations")
    return entries


def _assert_unique_identity(entries: list[dict[str, Any]]) -> None:
    """APIM enforces service-wide uniqueness on API name, path, and display name."""
    for field_name in ("apiId", "apiPath", "displayName"):
        seen: dict[str, str] = {}
        for entry in entries:
            for api in entry["apis"]:
                value = api[field_name]
                if value in seen:
                    raise ValueError(
                        f"Duplicate {field_name} '{value}' between "
                        f"'{seen[value]}' and '{entry['connectorId']}'"
                    )
                seen[value] = entry["connectorId"]

    mcp_ids = [entry["mcpApiId"] for entry in entries]
    if len(set(mcp_ids)) != len(mcp_ids):
        raise ValueError("Duplicate mcpApiId detected across providers")


def _prune_orphaned_specs(entries: list[dict[str, Any]]) -> list[str]:
    """Remove spec files left behind when a provider changes provenance."""
    expected = {ROOT / api["specFile"] for entry in entries for api in entry["apis"]}
    removed: list[str] = []
    for folder in ("official", "authored"):
        for path in (SPEC_DIR / folder).glob("*.json"):
            if path not in expected:
                path.unlink()
                removed.append(path.relative_to(ROOT).as_posix())
    return removed


if __name__ == "__main__":
    build()
    sys.exit(0)
