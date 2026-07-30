"""Generate the normalized connector facade OpenAPI document."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from research_assistant_core.connector_catalog import connector_definitions

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "infra" / "provider-specs" / "authored" / "research_connectors.json"


def _response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "source",
            "query",
            "records",
            "terms_url",
            "retrieved_from",
            "warnings",
            "notice",
        ],
        "properties": {
            "source": {"type": "string"},
            "query": {"type": "string"},
            "records": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "terms_url": {"type": "string", "format": "uri"},
            "retrieved_from": {"type": "string", "format": "uri"},
            "warnings": {"type": "array", "items": {"type": "string"}},
            "notice": {"type": "string"},
        },
    }


def _responses() -> dict[str, Any]:
    return {
        "200": {
            "description": "Normalized public metadata evidence.",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/ConnectorResult"}
                }
            },
        },
        "422": {"description": "Invalid connector input."},
        "502": {"description": "Upstream provider unavailable."},
    }


def connector_function_openapi() -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for connector in connector_definitions():
        for operation in connector.operations:
            if operation.operation_class == "delete":
                continue
            if operation.mcp_tool_name == "search":
                path = f"/v1/connectors/{connector.id}/search"
                parameters = [
                    {
                        "name": "query",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "minLength": 2, "maxLength": 500},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                            "default": 5,
                        },
                    },
                ]
            elif operation.mcp_tool_name == "lookup":
                path = f"/v1/connectors/{connector.id}/lookup"
                parameters = [
                    {
                        "name": "identifier",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string", "minLength": 1, "maxLength": 255},
                    }
                ]
            else:
                raise ValueError(
                    f"Unsupported normalized connector operation: {connector.id}/{operation.mcp_tool_name}"
                )
            paths[path] = {
                "get": {
                    "operationId": operation.id,
                    "summary": f"{connector.name} {operation.mcp_tool_name}",
                    "description": connector.description,
                    "parameters": parameters,
                    "responses": _responses(),
                    "tags": [connector.id],
                }
            }

    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Research Assistant normalized connectors",
            "version": "1.0.0",
            "description": (
                "Bounded deterministic normalization for public research metadata providers."
            ),
        },
        "servers": [{"url": "https://connector-functions.invalid/api"}],
        "paths": paths,
        "components": {"schemas": {"ConnectorResult": _response_schema()}},
    }


def main() -> None:
    document = connector_function_openapi()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT.relative_to(ROOT)} with {len(document['paths'])} operations.")


if __name__ == "__main__":
    main()