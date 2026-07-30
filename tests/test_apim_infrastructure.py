from __future__ import annotations

import json
from pathlib import Path

from research_assistant_core.connector_catalog import connector_definitions
from scripts.export_contracts import connector_mcp_catalog, connector_mcp_tools
from scripts.export_contracts import SPECIALIST_TOOLBOXES, specialist_toolbox_yaml

ROOT = Path(__file__).resolve().parents[1]


def test_generated_connector_mcp_catalog_matches_governed_operations() -> None:
    generated = connector_mcp_catalog()
    by_id = {entry["id"]: entry for entry in generated}

    assert set(by_id) == {connector.id for connector in connector_definitions()}
    for connector in connector_definitions():
        tools = by_id[connector.id]["tools"]
        assert tools == [
            {
                "name": operation.mcp_tool_name,
                "displayName": operation.mcp_tool_name,
                "operationId": operation.id,
            }
            for operation in connector.operations
            if operation.operation_class != "delete"
        ]


def test_generated_connector_mcp_tools_cover_every_non_delete_operation() -> None:
    generated = connector_mcp_tools()
    expected = [
        {
            "apiId": connector.apim_mcp_api_id,
            "name": operation.mcp_tool_name,
            "displayName": operation.mcp_tool_name,
            "description": connector.description,
            "operationId": operation.id,
        }
        for connector in connector_definitions()
        for operation in connector.operations
        if operation.operation_class != "delete"
    ]

    assert generated == expected


def test_generated_specialist_toolboxes_include_only_assigned_connectors() -> None:
    for agent_id, (_path, description) in SPECIALIST_TOOLBOXES.items():
        definition = specialist_toolbox_yaml(agent_id, description)

        assert "type: web_search" in definition
        for connector in connector_definitions():
            connection = f"project_connection_id: {connector.toolbox_connection_id}"
            assert (connection in definition) is (agent_id in connector.assigned_agents)


def test_apim_module_uses_supported_mcp_resource_model_and_policies() -> None:
    module = (
        ROOT / "infra" / "modules" / "api-management.bicep"
    ).read_text(encoding="utf-8")

    assert "Microsoft.ApiManagement/service@2024-05-01" in module
    assert "name: 'StandardV2'" in module
    assert "virtualNetworkType: 'None'" in module
    assert "virtualNetworkConfiguration" not in module
    assert "Microsoft.ApiManagement/service/apis@2025-09-01-preview" in module
    assert "type: 'mcp'" in module
    assert module.count(
        "Microsoft.ApiManagement/service/apis/tools@2025-09-01-preview"
    ) == 4
    assert "loadJsonContent('../../infra/connector-mcp-catalog.json')" in module
    assert "loadJsonContent('../../infra/connector-mcp-tools.json')" in module
    assert "resource sourceConnectorMcps" in module
    assert "resource sourceConnectorMcpTools" in module
    assert "resource sourceConnectorMcpPolicies" in module
    assert "output connectorMcpUrls array" in module
    assert "searchLiteratureMetadata" in module
    assert "searchGrantOpportunities" in module
    assert "searchMatchingMetadata" in module
    assert "validate-azure-ad-token" in module
    assert "validate-content" in module
    assert "rate-limit-by-key" in module
    assert "context.Response.Body" not in module
    assert "Microsoft.Insights/diagnosticSettings@2021-05-01-preview" in module
    assert "categoryGroup: 'allLogs'" in module
    assert "path: 'research-connectors'" in module
    assert "research-connectors/v1'" not in module


def test_apim_uses_the_public_network_in_the_poc_profile() -> None:
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(
        encoding="utf-8"
    )
    container_apps = (ROOT / "infra" / "modules" / "container-apps.bicep").read_text(
        encoding="utf-8"
    )

    assert "app-private-network.bicep" not in resources
    assert "infrastructureSubnetId" not in container_apps


def test_connector_adapter_is_identity_protected_and_wired_through_apim() -> None:
    container_apps = (
        ROOT / "infra" / "modules" / "container-apps.bicep"
    ).read_text(encoding="utf-8")
    resources = (
        ROOT / "infra" / "modules" / "resources.bicep"
    ).read_text(encoding="utf-8")
    azure_yaml = (ROOT / "azure.yaml").read_text(encoding="utf-8")

    assert "'azd-service-name': 'connector-adapter'" in container_apps
    connector_resource = container_apps.split(
        "resource connectorAdapter ",
        maxsplit=1,
    )[1].split("resource api ", maxsplit=1)[0]
    assert "external: true" in connector_resource
    assert "RESEARCH_WORKSPACE_TENANT_ID" in connector_resource
    assert "RESEARCH_CONNECTOR_GATEWAY_URL" in container_apps
    assert "RESEARCH_CONNECTOR_GATEWAY_TOKEN_SCOPE" in container_apps
    assert "apiManagement!.outputs.connectorMcpUrl" in resources
    assert "apiManagement!.outputs.connectorMcpUrls" in resources
    assert "connector-adapter:" in azure_yaml
    assert "services/connector_adapter/Dockerfile" in azure_yaml
    postprovision = (ROOT / "scripts" / "postprovision.py").read_text(
        encoding="utf-8"
    )
    assert "configure_connector_adapter_identity" in postprovision
    assert "RESEARCH_APIM_PRINCIPAL_ID" in postprovision
    assert "authentication-managed-identity" in (
        ROOT / "infra" / "modules" / "api-management.bicep"
    ).read_text(encoding="utf-8")


def test_static_connector_openapi_is_bounded_for_apim_import() -> None:
    specification = json.loads(
        (
            ROOT
            / "packages"
            / "contracts"
            / "connector-adapter-openapi.json"
        ).read_text(encoding="utf-8")
    )

    operations = {
        operation["operationId"]
        for path, path_item in specification["paths"].items()
        if path.startswith("/v1/")
        for method, operation in path_item.items()
        if method in {"get", "post"}
    }
    assert operations == {
        "listResearchConnectors",
        "searchLiteratureMetadata",
        "searchGrantOpportunities",
        "searchMatchingMetadata",
    } | {
        operation.id
        for connector in connector_definitions()
        for operation in connector.operations
    }
    for schema_name in (
        "LiteratureSearchRequest",
        "GrantSearchRequest",
        "MatchingSearchRequest",
    ):
        assert specification["components"]["schemas"][schema_name][
            "additionalProperties"
        ] is False


def test_api_container_never_enables_the_local_identity_opt_ins() -> None:
    """The local developer identity is only issued when
    ``entra_auth_enforced`` is false. This proves the deployed API container
    never sets the removed demo/trust opt-ins."""
    container_apps = (
        ROOT / "infra" / "modules" / "container-apps.bicep"
    ).read_text(encoding="utf-8")

    assert "RESEARCH_ALLOW_DEMO_IDENTITY" not in container_apps
    assert "RESEARCH_TRUST_PLATFORM_IDENTITY_HEADERS" not in container_apps
    assert "name: 'RESEARCH_ENVIRONMENT'" in container_apps
    assert "value: '${name}-azure'" in container_apps


# --- Harness integration blocker #2: MI authentication composition ---------


def test_container_apps_declares_conditional_entra_auth_config_and_reports_it() -> None:
    """Covers the harness-flagged gap: ``container-apps.bicep`` had no
    ``authConfigs`` resource enforcing Entra ID bearer tokens, and the API
    process had no way to independently confirm that infra-level boundary
    was really active (see ``config.Settings.entra_auth_enforced`` /
    ``identity.resolve_identity``). The authConfigs resource must be gated
    behind ``enableEntraAuth`` (default false, since it depends on an
    out-of-band Entra App Registration), while the reporting env var must
    always be emitted so the app never has to assume enforcement on faith.
    """
    container_apps = (
        ROOT / "infra" / "modules" / "container-apps.bicep"
    ).read_text(encoding="utf-8")

    assert "param enableEntraAuth bool = false" in container_apps
    assert "param entraTenantId string = ''" in container_apps
    assert "param entraApiClientId string = ''" in container_apps
    assert "Microsoft.App/containerApps/authConfigs@" in container_apps
    assert "resource apiAuthConfig" in container_apps
    assert "if (enableEntraAuth)" in container_apps
    assert "unauthenticatedClientAction: 'Return401'" in container_apps
    assert "azureActiveDirectory" in container_apps
    # The reporting env var is unconditional (not wrapped in a `concat`
    # gated array) so the running app can always read the true value of
    # enableEntraAuth, never a value that silently disappears when the
    # feature is off.
    assert "name: 'RESEARCH_ENTRA_AUTH_ENFORCED'" in container_apps
    assert "value: string(enableEntraAuth)" in container_apps


def test_container_apps_trusts_platform_identity_header_only_when_easyauth_enforces() -> None:
    """The API must trust the platform-injected ``x-ms-client-principal``
    header exactly when Container Apps built-in authentication is enforcing.

    There is deliberately only one switch: ``RESEARCH_ENTRA_AUTH_ENFORCED`` is
    wired from the same ``enableEntraAuth`` toggle that gates the
    ``authConfigs`` resource, and ``identity.resolve_identity`` reads the
    forgeable ``x-ms-client-principal`` header only when that switch is on.
    A second, independently settable "trust the header" flag could disagree
    with the gateway that is actually deployed, so it must not exist.
    """
    container_apps = (ROOT / "infra" / "modules" / "container-apps.bicep").read_text(encoding="utf-8")
    assert "name: 'RESEARCH_ENTRA_AUTH_ENFORCED'" in container_apps
    assert "value: string(enableEntraAuth)" in container_apps
    assert "resource apiAuthConfig" in container_apps
    assert "if (enableEntraAuth)" in container_apps
    # No separate header-trust switch may be reintroduced alongside it.
    assert "RESEARCH_TRUST_PLATFORM_IDENTITY_HEADERS" not in container_apps
    assert "RESEARCH_ALLOW_DEMO_IDENTITY" not in container_apps


def test_container_apps_carry_no_attestation_secret_wiring() -> None:
    """ReleaseAttestation signs with an unkeyed SHA-256 digest, so the api
    container needs no Key Vault secret delivery to start."""
    container_apps = (
        ROOT / "infra" / "modules" / "container-apps.bicep"
    ).read_text(encoding="utf-8")

    assert "attestation" not in container_apps.lower()
    assert "secrets:" not in container_apps


def test_infrastructure_provisions_no_key_vault() -> None:
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(encoding="utf-8")
    main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")

    assert not (ROOT / "infra" / "modules" / "keyvault.bicep").exists()
    for template in (resources, main):
        assert "keyvault" not in template.lower()
        assert "attestation" not in template.lower()


def test_resources_module_threads_entra_params_through() -> None:
    resources = (
        ROOT / "infra" / "modules" / "resources.bicep"
    ).read_text(encoding="utf-8")

    assert "entraTenantId: entraTenantId" in resources
    assert "entraApiClientId: entraApiClientId" in resources
    assert "enableEntraAuth: enableEntraAuth" in resources


def test_main_bicep_defaults_entra_params_off_for_backward_compatibility() -> None:
    """The Entra App Registration is an explicit, out-of-band operator step,
    so enforcement stays off until one exists."""
    main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")

    assert "param enableEntraAuth bool = false" in main
    assert "param entraTenantId string = ''" in main
    assert "param entraApiClientId string = ''" in main
