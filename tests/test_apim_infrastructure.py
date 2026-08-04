from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

from research_assistant_core.connector_catalog import connector_definitions

from scripts.build_connector_apim_spec import (
    connector_apim_openapi,
    connector_operation_policies,
)
from scripts.export_contracts import (
    SPECIALIST_TOOLBOXES,
    connector_mcp_catalog,
    connector_mcp_tools,
    specialist_toolbox_yaml,
)

ROOT = Path(__file__).resolve().parents[1]


def test_generated_connector_mcp_catalog_matches_governed_operations() -> None:
    generated = connector_mcp_catalog()
    by_id = {entry["id"]: entry for entry in generated}

    assert set(by_id) == {connector.id for connector in connector_definitions()}
    for connector in connector_definitions():
        tools = by_id[connector.id]["tools"]
        assert tools == [
            {
                "name": operation.apim_tool_name,
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
            "name": operation.apim_tool_name,
            "displayName": operation.mcp_tool_name,
            "description": connector.description,
            "operationId": operation.id,
        }
        for connector in connector_definitions()
        for operation in connector.operations
        if operation.operation_class != "delete"
    ]

    assert generated == expected


def test_generated_connector_mcp_tool_names_are_unique_across_the_service() -> None:
    names = [tool["name"] for tool in connector_mcp_tools()]

    assert len(names) == len(set(names))
    assert max(map(len, names)) <= 32
    assert names == [
        operation.apim_tool_name
        for connector in connector_definitions()
        for operation in connector.operations
        if operation.operation_class != "delete"
    ]
    assert "orcidSearch" not in names
    # APIM 502s on tool ids that collide with its operation ids or bare actions.
    operation_ids = {
        operation.id
        for connector in connector_definitions()
        for operation in connector.operations
    }
    assert set(names).isdisjoint(operation_ids)
    assert set(names).isdisjoint({"search", "lookup"})
    assert all(re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)*", name) for name in names)


def test_generated_specialist_toolboxes_include_only_assigned_connectors() -> None:
    for agent_id, (_path, description) in SPECIALIST_TOOLBOXES.items():
        definition = specialist_toolbox_yaml(agent_id, description)

        assert "type: web_search" in definition
        for connector in connector_definitions():
            connection = f"project_connection_id: {connector.toolbox_connection_id}"
            assert (connection in definition) is (agent_id in connector.assigned_agents)


def test_apim_bicep_owns_only_durable_service_diagnostics_and_rbac() -> None:
    module = (
        ROOT / "infra" / "modules" / "api-management.bicep"
    ).read_text(encoding="utf-8")
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(
        encoding="utf-8"
    )
    role = (
        ROOT / "infra" / "modules" / "apim-named-value-writer-role.bicep"
    ).read_text(encoding="utf-8")
    postprovision = (ROOT / "scripts" / "provider_onboarding.py").read_text(
        encoding="utf-8"
    )

    assert "Microsoft.ApiManagement/service@2024-05-01" in module
    assert "name: 'StandardV2'" in module
    assert "virtualNetworkType: 'None'" in module
    assert "virtualNetworkConfiguration" not in module
    assert "Microsoft.Insights/diagnosticSettings@2021-05-01-preview" in module
    assert "categoryGroup: 'allLogs'" in module
    assert "Microsoft.Authorization/roleAssignments@2022-04-01" in module
    assert "namedValueWriterRoleDefinitionId" in module
    assert "Microsoft.ApiManagement/service/apis" not in module
    assert "Microsoft.ApiManagement/service/namedValues" not in module
    assert "connector-operation-policies.json" not in module
    assert "connector-mcp-catalog.json" not in module
    assert "namedValueWriterRoleDefinitionId: apimNamedValueWriterRoleId" in resources

    assert "Microsoft.ApiManagement/service/namedValues/write" in role
    assert "Microsoft.ApiManagement/service/namedValues/read" in role
    assert "Microsoft.ApiManagement/service/read" in role
    assert "listValue/action" not in role
    assert "Microsoft.ApiManagement/service/apis" not in role

    assert "def reconcile_connector_gateway(" in postprovision
    assert 'CONNECTOR_SUBSCRIPTION_ID = "foundry-agent-tools"' in postprovision
    assert 'f"/namedValues/{named_value}"' in postprovision
    assert 'f"/apis/{CONNECTOR_API_ID}"' in postprovision
    assert 'f"/apis/{api_id}"' in postprovision
    assert "_connector_api_policy(" in postprovision
    assert "_connector_mcp_policy(" in postprovision


def test_deployment_paths_use_the_same_basic_acr_sku() -> None:
    greenfield = (ROOT / "infra" / "modules" / "acr.bicep").read_text(
        encoding="utf-8"
    )
    brownfield = (ROOT / "infra" / "brownfield.bicep").read_text(encoding="utf-8")

    assert "name: 'Basic'" in greenfield
    assert "name: 'Basic'" in brownfield
    assert "name: 'Premium'" not in brownfield


def test_postprovision_fails_closed_before_deployment() -> None:
    postprovision = (ROOT / "scripts" / "postprovision.ps1").read_text(encoding="utf-8")
    postdeploy = (ROOT / "scripts" / "postdeploy.ps1").read_text(encoding="utf-8")
    posix_postprovision = (ROOT / "scripts" / "postprovision.sh").read_text(encoding="utf-8")
    posix_postdeploy = (ROOT / "scripts" / "postdeploy.sh").read_text(encoding="utf-8")

    assert "$PSNativeCommandUseErrorActionPreference = $true" in postprovision
    assert "Postprovision failed before required deployment inputs were ready" in postprovision
    assert "Write-Warning" not in postprovision
    assert "set -eu" in posix_postprovision
    assert "WARNING: postprovision" not in posix_postprovision
    assert "scripts.postprovision" not in postdeploy
    assert "scripts.postprovision" not in posix_postdeploy


def test_data_services_use_the_minimal_private_network() -> None:
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(
        encoding="utf-8"
    )
    container_apps = (ROOT / "infra" / "modules" / "container-apps.bicep").read_text(
        encoding="utf-8"
    )
    private_network = (ROOT / "infra" / "modules" / "app-private-network.bicep").read_text(
        encoding="utf-8"
    )

    assert "app-private-network.bicep" in resources
    assert "infrastructureSubnetId: infrastructureSubnetId" in container_apps
    assert "10.70.0.0/27" in private_network
    assert "10.70.0.32/28" in private_network
    assert "Microsoft.App/environments" in private_network
    assert "privatelink.blob." in private_network
    assert "privatelink.documents.azure.com" in private_network
    assert private_network.count("Microsoft.Network/privateEndpoints@") == 2
    assert "api-management" not in private_network


def test_every_container_app_binds_acr_pull_to_a_preexisting_identity() -> None:
    """Registry RBAC must exist before Container App creation."""
    container_apps = (ROOT / "infra" / "modules" / "container-apps.bicep").read_text(
        encoding="utf-8"
    )

    assert container_apps.count("server: acr.properties.loginServer") == 2
    app_blocks = {
        name: container_apps.split(f"resource {name} ", 1)[1].split("\nresource ", 1)[0]
        for name in ("api", "web")
    }
    for name in ("api", "web"):
        assert "identity: apiIdentityResourceId" in app_blocks[name]
    assert "principalId: apiIdentityPrincipalId" in container_apps
    assert container_apps.count("apiIdentityAcrPull") == 3


def test_foundry_project_assigns_deployer_data_plane_roles() -> None:
    resources = (ROOT / "infra" / "modules" / "resources.bicep").read_text(
        encoding="utf-8"
    )

    assert "resource developerFoundryUser" in resources
    assert "name: guid(foundryAccount::project.id, principalId, foundryUserRoleId)" in resources
    assert "roleDefinitionId: foundryUserRoleId" in resources


def test_connectors_are_apim_native_without_a_connector_container_app() -> None:
    container_apps = (
        ROOT / "infra" / "modules" / "container-apps.bicep"
    ).read_text(encoding="utf-8")
    resources = (
        ROOT / "infra" / "modules" / "resources.bicep"
    ).read_text(encoding="utf-8")
    azure_yaml = (ROOT / "azure.yaml").read_text(encoding="utf-8")

    assert "connectorAdapter" not in container_apps
    assert "ca-connectors-" not in container_apps
    assert "RESEARCH_CONNECTOR_GATEWAY_URL" in container_apps
    assert "RESEARCH_CONNECTOR_GATEWAY_TOKEN_SCOPE" in container_apps
    assert "AZURE_CONNECTOR_MCP_URLS" not in resources
    assert "AZURE_API_MANAGEMENT_MCP_SUBSCRIPTION_ID" not in resources
    assert "connector-adapter:" not in azure_yaml
    assert "services/connector_adapter/Dockerfile" not in azure_yaml
    postprovision = (ROOT / "scripts" / "postprovision.py").read_text(
        encoding="utf-8"
    )
    assert "configure_connector_adapter_identity" not in postprovision
    assert "SERVICE_CONNECTOR_ADAPTER_NAME" not in postprovision
    assert "configure_connector_gateway" in postprovision
    assert "configure_connector_mcp_tools" not in postprovision
    assert "authentication-managed-identity" in (
        ROOT / "scripts" / "provider_onboarding.py"
    ).read_text(encoding="utf-8")


def test_static_connector_openapi_is_bounded_for_apim_import() -> None:
    specification = connector_apim_openapi()

    operations = {
        operation["operationId"]
        for path_item in specification["paths"].values()
        for method, operation in path_item.items()
        if method == "get"
    }
    assert operations == {
        operation.id
        for connector in connector_definitions()
        for operation in connector.operations
    }
    assert len(specification["paths"]) == 20
    assert all(set(path_item) == {"get"} for path_item in specification["paths"].values())
    assert all(
        path_item["get"]["parameters"][0]["in"] == "query"
        for path_item in specification["paths"].values()
    )
    assert specification["components"]["schemas"]["ConnectorResult"]["additionalProperties"] is False


def test_connector_operation_policies_cover_apim_native_transformations() -> None:
    policies = {item["operationId"]: item["value"] for item in connector_operation_policies()}
    expected = {
        operation.id
        for connector in connector_definitions()
        for operation in connector.operations
    }

    assert set(policies) == expected
    for value in policies.values():
        ElementTree.fromstring(value)
        assert 'name="Authorization" exists-action="delete"' in value
        assert "set-backend-service" in value
    assert "send-request" in policies["pubmedSearch"]
    assert "xml-to-json" in policies["arxivSearch"]
    assert "xml-to-json" in policies["arxivLookup"]
    assert "<set-method>POST</set-method>" in policies["grantsGovSearch"]
    assert "<set-method>POST</set-method>" in policies["nihReporterSearch"]
    assert "context.Response.Body" in policies["crossrefSearch"]
    assert "Url.Query.GetValueOrDefault" in policies["pubmedSearch"]


def test_mcp_tools_resolve_against_the_single_connector_facade_api() -> None:
    """APIM exposes one REST API as MCP servers; a second backing API is not modelled."""
    document = connector_apim_openapi()
    facade_operations = {
        operation["operationId"]
        for path in document["paths"].values()
        for operation in path.values()
    }

    tools = connector_mcp_tools()
    assert tools
    assert {tool["operationId"] for tool in tools} <= facade_operations

    source = (ROOT / "scripts" / "provider_onboarding.py").read_text(encoding="utf-8")
    assert 'f"{self._base}/apis/{CONNECTOR_API_ID}/operations/"' in source
    assert "_repair_empty_mcp_api" not in source


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
    assert "enableApimMcpTools" not in main
