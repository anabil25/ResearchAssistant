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
    assert "path: 'research-connectors'" in module
    assert "research-connectors/v1'" not in module


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

    ``RESEARCH_TRUST_PLATFORM_IDENTITY_HEADERS`` is therefore derived from the
    same ``enableEntraAuth`` toggle that gates the ``authConfigs`` resource, so
    the app never trusts a forgeable header when the platform is not validating
    tokens -- and so the app's fail-closed startup guard
    (``config._forbid_unenforced_platform_identity_trust_outside_safe_environments``)
    is satisfied, since ``entra_auth_enforced`` is wired from the same value.
    """
    container_apps = (ROOT / "infra" / "modules" / "container-apps.bicep").read_text(encoding="utf-8")
    assert "name: 'RESEARCH_TRUST_PLATFORM_IDENTITY_HEADERS'" in container_apps
    # Both trust flags derive from the single enableEntraAuth toggle.
    assert container_apps.count("value: string(enableEntraAuth)") >= 2


def test_container_apps_sources_attestation_signing_key_from_key_vault_only_when_provisioned() -> None:
    """Harness blocker #3 (authentic signing key deployment) is code-complete
    at the application layer (``release_attestation.py`` HMAC signing +
    ``config._forbid_unversioned_or_missing_attestation_signing_key`` fail
    closed); the remaining gap was infra-level secret delivery. This proves
    the Key-Vault-backed ``secretRef`` wiring is present and gated behind an
    explicit ``attestationSigningSecretsProvisioned`` confirmation -- never
    a plaintext env var, and never assumed present by default."""
    container_apps = (
        ROOT / "infra" / "modules" / "container-apps.bicep"
    ).read_text(encoding="utf-8")

    assert "param attestationSigningSecretsProvisioned bool = false" in container_apps
    assert "param attestationKeyVaultUri string = ''" in container_apps
    assert "agent-studio-attestation-signing-key" in container_apps
    assert "agent-studio-attestation-signing-key-version" in container_apps
    assert "secretRef: 'agent-studio-attestation-signing-key'" in container_apps
    assert "secretRef: 'agent-studio-attestation-signing-key-version'" in container_apps
    # No plaintext value ever assigned for the signing key itself.
    assert "value: attestationKeyVaultUri" not in container_apps
    assert "secrets: attestationSecretRefs" in container_apps


def test_keyvault_module_uses_rbac_authorization_and_never_mints_secret_values() -> None:
    """The Key Vault module must never generate the actual signing-key
    *value* through Bicep/ARM (deployment-time randomness is not a suitable
    cryptographic key source); populating it is an explicit out-of-band
    operational step. It must also use RBAC authorization (not legacy
    access policies) and grant the API identity read-only Secrets User,
    matching this repo's existing RBAC-first convention for every other
    module (see ``resources.bicep``'s role-definition-id variables)."""
    key_vault = (
        ROOT / "infra" / "modules" / "keyvault.bicep"
    ).read_text(encoding="utf-8")

    assert "Microsoft.KeyVault/vaults@" in key_vault
    assert "enableRbacAuthorization: true" in key_vault
    assert "enablePurgeProtection: true" in key_vault
    # Key Vault Secrets User (data-plane read-only) built-in role id.
    assert "4633458b-17de-408a-b874-0445c86b69e6" in key_vault
    # Key Vault Secrets Officer (operator write access) built-in role id.
    assert "b86a8fe4-44ce-4948-aee5-eccb2c155cd7" in key_vault
    assert "output vaultUri string" in key_vault
    assert "output vaultName string" in key_vault
    # No secret *value* resource exists anywhere in this module -- secret
    # population is an explicit out-of-band operational step, never minted
    # by Bicep/ARM deployment-time randomness.
    assert "Microsoft.KeyVault/vaults/secrets" not in key_vault


def test_resources_module_wires_key_vault_and_threads_entra_params_through() -> None:
    resources = (
        ROOT / "infra" / "modules" / "resources.bicep"
    ).read_text(encoding="utf-8")

    assert "param includeAttestationKeyVault bool = false" in resources
    assert "module keyVault 'keyvault.bicep' = if (includeAttestationKeyVault)" in resources
    assert "entraTenantId: entraTenantId" in resources
    assert "entraApiClientId: entraApiClientId" in resources
    assert "enableEntraAuth: enableEntraAuth" in resources
    assert "attestationKeyVaultUri: includeAttestationKeyVault ? keyVault!.outputs.vaultUri : ''" in resources


def test_main_bicep_defaults_entra_and_keyvault_params_off_for_backward_compatibility() -> None:
    """New top-level params must default to false/empty so every existing
    deployment definition (with no knowledge of these new params) continues
    to provision exactly as before -- the Entra App Registration and Key
    Vault secret population are explicit, out-of-band operator steps."""
    main = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")

    assert "param enableEntraAuth bool = false" in main
    assert "param entraTenantId string = ''" in main
    assert "param entraApiClientId string = ''" in main
    assert "param includeAttestationKeyVault bool = false" in main
    assert "param attestationSigningSecretsProvisioned bool = false" in main
