from __future__ import annotations

import json
from pathlib import Path

import pytest
from research_assistant_core.connector_catalog import connector_definitions, connector_ids
from research_assistant_core.provider_api_catalog import (
    ProviderApi,
    provider_api,
    provider_apis,
)

from scripts.provider_onboarding import (
    SHARED_TOOLBOX_NAME,
    mcp_endpoint,
    provider_connection_id,
    shared_toolbox_payload,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "infra" / "provider-specs" / "manifest.json"
_HTTP_METHODS = {"get", "post", "put", "patch", "head", "options"}


def _manifest() -> list[dict[str, object]]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_every_governed_connector_has_a_registered_provider_api() -> None:
    assert {provider.connector_id for provider in provider_apis()} == set(connector_ids())


def test_provider_apis_declare_unique_https_backed_identifiers() -> None:
    providers = provider_apis()

    assert len({provider.api_id for provider in providers}) == len(providers)
    assert len({provider.mcp_api_id for provider in providers}) == len(providers)
    assert len({provider.api_path for provider in providers}) == len(providers)
    assert len({provider.mcp_path for provider in providers}) == len(providers)
    assert len({provider.server_label for provider in providers}) == len(providers)
    for provider in providers:
        assert provider.server_url.startswith("https://"), provider.connector_id
        assert provider.documentation_url.startswith("https://"), provider.connector_id


def test_specs_are_filed_by_provenance() -> None:
    for provider in provider_apis():
        expected = "official" if provider.is_published else "authored"
        assert provider.spec_folder == expected, provider.connector_id
        assert provider.spec_file == f"infra/provider-specs/{expected}/{provider.connector_id}.json"
        assert (ROOT / provider.spec_file).is_file(), provider.connector_id


def test_authored_operations_are_read_only_and_uniquely_identified() -> None:
    for provider in provider_apis():
        if provider.is_published:
            assert provider.operations == (), provider.connector_id
            continue
        assert provider.operations, provider.connector_id
        operation_ids = [operation.operation_id for operation in provider.operations]
        assert len(set(operation_ids)) == len(operation_ids), provider.connector_id
        for operation in provider.operations:
            # Delete-class operations are never onboarded.
            assert operation.method in {"GET", "POST"}, operation.operation_id
            assert operation.path.startswith("/"), operation.operation_id
            assert operation.summary


def test_path_parameters_are_declared_for_every_templated_segment() -> None:
    for provider in provider_apis():
        for operation in provider.operations:
            templated = {
                segment[1:-1]
                for segment in operation.path.split("/")
                if segment.startswith("{") and segment.endswith("}")
            }
            declared = {
                parameter.name for parameter in operation.parameters if parameter.location == "path"
            }
            assert templated == declared, operation.operation_id


def test_provider_lookup_is_normalized_and_fails_closed() -> None:
    assert provider_api(" PubMed ").display_name == "NCBI PubMed E-utilities"
    with pytest.raises(ValueError, match="No provider API is registered"):
        provider_api("unregistered_source")


def test_generated_manifest_matches_the_catalog_and_exposes_no_deletes() -> None:
    by_connector = {entry["connectorId"]: entry for entry in _manifest()}

    assert set(by_connector) == {provider.connector_id for provider in provider_apis()}
    for provider in provider_apis():
        entry = by_connector[provider.connector_id]
        assert entry["apiId"] == provider.api_id
        assert entry["mcpApiId"] == provider.mcp_api_id
        assert entry["serverUrl"] == provider.server_url
        assert entry["serverLabel"] == provider.server_label
        assert entry["provenance"] == provider.spec_folder
        assert entry["specFile"] == provider.spec_file
        assert entry["apimFormat"] in {"openapi+json", "swagger-json"}
        assert entry["operationIds"], provider.connector_id

        document = json.loads((ROOT / str(entry["specFile"])).read_text(encoding="utf-8"))
        emitted: list[str] = []
        for api in entry["apis"]:
            document = json.loads((ROOT / str(api["specFile"])).read_text(encoding="utf-8"))
            for path_item in document["paths"].values():
                for method, operation in path_item.items():
                    assert method.lower() != "delete", provider.connector_id
                    if method.lower() in _HTTP_METHODS:
                        emitted.append(operation["operationId"])
        assert sorted(emitted) == entry["operationIds"], provider.connector_id


def test_generated_specs_target_the_documented_provider_backend() -> None:
    for entry in _manifest():
        for api in entry["apis"]:
            document = json.loads((ROOT / str(api["specFile"])).read_text(encoding="utf-8"))
            if "swagger" in document:
                resolved = f"https://{document['host']}{document['basePath']}".rstrip("/")
            else:
                # API Management imports OpenAPI 3.0.x only.
                assert str(document["openapi"]).startswith("3.0"), entry["connectorId"]
                resolved = document["servers"][0]["url"].rstrip("/")
            assert resolved == str(api["serverUrl"]).rstrip("/"), entry["connectorId"]


def test_api_identifiers_are_derived_without_underscores() -> None:
    provider = ProviderApi(
        connector_id="example_source",
        display_name="Example",
        documentation_url="https://example.test/docs",
        server_url="https://example.test",
        description="Example provider.",
    )

    assert provider.api_id == "provider-example-source-v1"
    assert provider.mcp_api_id == "provider-example-source-mcp-v1"
    assert provider.api_path == "providers/example-source"
    assert provider.mcp_path == "provider-example-source-mcp"
    assert provider.spec_folder == "authored"
    assert provider.is_published is False


def test_shared_toolbox_exposes_only_governed_connectors_behind_tool_search() -> None:
    connector_targets = {
        connector.id: f"https://gateway.example/{connector.apim_mcp_path}/mcp"
        for connector in connector_definitions()
    }

    payload = shared_toolbox_payload(connector_targets)
    tools = payload["tools"]
    types = [tool["type"] for tool in tools]
    mcp_labels = {tool["server_label"] for tool in tools if tool["type"] == "mcp"}

    assert types.count("web_search") == 1
    assert types.count("code_interpreter") == 1
    assert types.count("file_search") == 1
    # Tool Search keeps agent context flat across a tool surface larger than the 128-tool agent cap.
    assert types.count("toolbox_search") == 1
    # Foundry rejects a version with more than one tool lacking a name or server_label.
    unnamed = [tool for tool in tools if "name" not in tool and "server_label" not in tool]
    assert [tool["type"] for tool in unnamed] == ["toolbox_search"]
    connector_labels = {connector.id for connector in connector_definitions()}
    assert mcp_labels == connector_labels
    assert tools[0]["tool_configs"] == {"*": {"pin": True}}
    assert tools[1]["tool_configs"] == {"*": {"pin": True}}
    for tool in tools:
        if tool["type"] != "mcp":
            continue
        assert tool["server_url"].endswith("/mcp")
        assert tool["server_url"] == connector_targets[tool["server_label"]]
        assert tool["project_connection_id"].startswith("research-connector-")
        assert tool["tool_configs"]["*"]["pin"] is True


def test_shared_toolbox_requires_complete_connector_catalog() -> None:
    with pytest.raises(ValueError, match="complete governed connector catalog"):
        shared_toolbox_payload({})


def test_shared_toolbox_identifiers_are_stable() -> None:
    assert SHARED_TOOLBOX_NAME == "research-shared"
    assert provider_connection_id("europe_pmc") == "provider-europe-pmc-apim"
    assert mcp_endpoint("https://gateway.example/", "provider-ror-mcp") == (
        "https://gateway.example/provider-ror-mcp/mcp"
    )
