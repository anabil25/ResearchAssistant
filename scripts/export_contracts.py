from __future__ import annotations

import json
from pathlib import Path

from research_assistant_api.app import app
from research_assistant_connector_adapter.provider_api import contract_app as provider_app
from research_assistant_core.connector_catalog import connector_definitions

from scripts.build_connector_apim_spec import (
    connector_apim_openapi,
    connector_operation_policies,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "packages" / "contracts" / "openapi.json"
CONNECTOR_OUTPUT = ROOT / "infra" / "provider-specs" / "authored" / "research_connectors.json"
CONNECTOR_POLICY_OUTPUT = ROOT / "infra" / "connector-operation-policies.json"
PROVIDER_V7_OUTPUT = (
    ROOT / "packages" / "contracts" / "provider-adapter-openapi.v7.json"
)
CONNECTOR_MCP_CATALOG_OUTPUT = ROOT / "infra" / "connector-mcp-catalog.json"
CONNECTOR_MCP_TOOLS_OUTPUT = ROOT / "infra" / "connector-mcp-tools.json"
SPECIALIST_TOOLBOXES = {
    "literature": (
        ROOT / "infra" / "toolboxes" / "literature-toolbox.yaml",
        "Governed public literature discovery tools",
    ),
    "grant": (
        ROOT / "infra" / "toolboxes" / "grant-toolbox.yaml",
        "Governed public funding discovery tools",
    ),
    "matching": (
        ROOT / "infra" / "toolboxes" / "matching-toolbox.yaml",
        "Governed public researcher and organization discovery tools",
    ),
}


def connector_mcp_catalog() -> list[dict[str, object]]:
    return [
        {
            "id": connector.id,
            "apiId": connector.apim_mcp_api_id,
            "path": connector.apim_mcp_path,
            "displayName": f"{connector.name} MCP server",
            "description": connector.description,
            "credentialNamedValue": connector.credential.named_value,
            "tools": [
                {
                    "name": operation.apim_tool_name,
                    "displayName": operation.mcp_tool_name,
                    "operationId": operation.id,
                }
                for operation in connector.operations
                if operation.operation_class != "delete"
            ],
        }
        for connector in connector_definitions()
    ]


def connector_mcp_tools() -> list[dict[str, str]]:
    # Tool resource names are unique per APIM service; the MCP-facing name comes from displayName.
    return [
        {
            "apiId": connector.apim_mcp_api_id,
            "name": operation.apim_tool_name,
            "displayName": operation.mcp_tool_name,
            "description": connector.description,
            "operationId": operation.id,
        }
        for connector in connector_definitions()
        for operation in connector.operations
        if operation.operation_class != "delete"
    ]


def specialist_toolbox_yaml(agent_id: str, description: str) -> str:
    lines = [
        f"description: {description}",
        "tools:",
        "  - type: web_search",
        "    name: web_search",
        '    require_approval: "never"',
    ]
    for connector in connector_definitions():
        if agent_id not in connector.assigned_agents:
            continue
        lines.extend(
            (
                "  - type: mcp",
                f"    server_label: {connector.id}",
                f"    project_connection_id: {connector.toolbox_connection_id}",
                '    require_approval: "never"',
            )
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    CONNECTOR_OUTPUT.write_text(
        json.dumps(connector_apim_openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {CONNECTOR_OUTPUT.relative_to(ROOT)}")
    CONNECTOR_POLICY_OUTPUT.write_text(
        json.dumps(connector_operation_policies(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {CONNECTOR_POLICY_OUTPUT.relative_to(ROOT)}")
    PROVIDER_V7_OUTPUT.write_text(
        json.dumps(provider_app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {PROVIDER_V7_OUTPUT.relative_to(ROOT)}")
    CONNECTOR_MCP_CATALOG_OUTPUT.write_text(
        json.dumps(connector_mcp_catalog(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {CONNECTOR_MCP_CATALOG_OUTPUT.relative_to(ROOT)}")
    CONNECTOR_MCP_TOOLS_OUTPUT.write_text(
        json.dumps(connector_mcp_tools(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {CONNECTOR_MCP_TOOLS_OUTPUT.relative_to(ROOT)}")
    for agent_id, (path, description) in SPECIALIST_TOOLBOXES.items():
        path.write_text(
            specialist_toolbox_yaml(agent_id, description),
            encoding="utf-8",
        )
        print(f"Wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
