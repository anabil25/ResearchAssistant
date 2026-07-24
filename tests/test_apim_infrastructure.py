from __future__ import annotations

import json
from pathlib import Path

from research_assistant_api.config import DEMO_IDENTITY_SAFE_ENVIRONMENTS

ROOT = Path(__file__).resolve().parents[1]


def test_apim_module_uses_supported_mcp_resource_model_and_policies() -> None:
    module = (
        ROOT / "infra" / "modules" / "api-management.bicep"
    ).read_text(encoding="utf-8")

    assert "Microsoft.ApiManagement/service@2024-05-01" in module
    assert "name: 'StandardV2'" in module
    assert "virtualNetworkType: 'External'" in module
    assert "Microsoft.ApiManagement/service/apis@2025-09-01-preview" in module
    assert "type: 'mcp'" in module
    assert module.count(
        "Microsoft.ApiManagement/service/apis/tools@2025-09-01-preview"
    ) == 3
    assert "searchLiteratureMetadata" in module
    assert "searchGrantOpportunities" in module
    assert "searchMatchingMetadata" in module
    assert "validate-azure-ad-token" in module
    assert "validate-content" in module
    assert "rate-limit-by-key" in module
    assert "context.Response.Body" not in module
    assert "Microsoft.Insights/diagnosticSettings@2021-05-01-preview" in module
    assert "categoryGroup: 'allLogs'" in module


def test_apim_has_a_dedicated_delegated_network_boundary() -> None:
    network = (
        ROOT / "infra" / "modules" / "app-private-network.bicep"
    ).read_text(encoding="utf-8")

    assert "snet-api-management" in network
    assert "10.70.3.0/24" in network
    assert "Microsoft.Web/serverFarms" in network
    assert "apiManagementNsg" in network
    assert "Storage" in network
    assert "AzureKeyVault" in network


def test_connector_adapter_is_internal_and_wired_through_apim() -> None:
    container_apps = (
        ROOT / "infra" / "modules" / "container-apps.bicep"
    ).read_text(encoding="utf-8")
    resources = (
        ROOT / "infra" / "modules" / "resources.bicep"
    ).read_text(encoding="utf-8")
    azure_yaml = (ROOT / "azure.yaml").read_text(encoding="utf-8")

    assert "'azd-service-name': 'connector-adapter'" in container_apps
    assert "external: false" in container_apps
    assert "RESEARCH_CONNECTOR_GATEWAY_URL" in container_apps
    assert "RESEARCH_CONNECTOR_GATEWAY_TOKEN_SCOPE" in container_apps
    assert "apiManagement!.outputs.connectorMcpUrl" in resources
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
    }
    for schema_name in (
        "LiteratureSearchRequest",
        "GrantSearchRequest",
        "MatchingSearchRequest",
    ):
        assert specification["components"]["schemas"][schema_name][
            "additionalProperties"
        ] is False


def test_api_container_never_enables_demo_identity_and_has_unsafe_environment_value() -> None:
    """Defense-in-depth for the Agent Studio demo-sandbox membership bypass
    (see ``research_assistant_api.agent_studio.authz.DemoSandboxMembershipPolicy``):
    ``allow_demo_identity`` already defaults to ``False`` and
    ``Settings._forbid_demo_identity_outside_safe_environments`` refuses to
    start if it is ever enabled outside a small safe-environment allowlist.
    This test proves the deployed API container reinforces both invariants:
    it never sets ``RESEARCH_ALLOW_DEMO_IDENTITY`` at all (so the safe
    ``False`` default always applies in production), and its
    ``RESEARCH_ENVIRONMENT`` value is not itself one of the safe-environment
    names, so even a forced override could never pass the startup guard."""
    container_apps = (
        ROOT / "infra" / "modules" / "container-apps.bicep"
    ).read_text(encoding="utf-8")

    assert "RESEARCH_ALLOW_DEMO_IDENTITY" not in container_apps
    assert "name: 'RESEARCH_ENVIRONMENT'" in container_apps
    assert "value: '${name}-azure'" in container_apps

    # The interpolated value always ends in the literal ``-azure`` suffix; no
    # safe-environment name shares that suffix, so no resource ``name`` value
    # could make the deployed ``RESEARCH_ENVIRONMENT`` collide with a safe
    # environment even if ``allow_demo_identity`` were forcibly overridden.
    assert not any(safe_name.endswith("-azure") for safe_name in DEMO_IDENTITY_SAFE_ENVIRONMENTS)
