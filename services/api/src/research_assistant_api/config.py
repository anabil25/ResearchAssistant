from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Environment names (case-insensitive, whitespace-trimmed) in which the
#: interactive local/dev "demo sandbox" identity (see
#: ``research_assistant_api.identity.DEMO_SANDBOX_SOURCE``) may be enabled.
#: Any other environment name -- including unrecognized ones -- is treated
#: as production-like and refuses to start with demo identity enabled, so
#: the bypass can never be silently carried into a production deployment
#: through an unset or misconfigured ``RESEARCH_ALLOW_DEMO_IDENTITY``.
#: ``Settings.allow_demo_identity`` defaults to ``False``: even in a safe
#: environment, demo identity must be explicitly opted into via
#: ``RESEARCH_ALLOW_DEMO_IDENTITY=true`` -- it is never enabled by default.
DEMO_IDENTITY_SAFE_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})

#: Environment names (case-insensitive, whitespace-trimmed) in which a
#: ``ReleaseAttestation`` may fall back to an unkeyed SHA-256 content digest
#: (``signature_algorithm == "sha256-digest"``) instead of a genuine keyed
#: signature. That fallback is honestly labeled (see
#: ``research_assistant_api.agent_studio.release_attestation``) but it is
#: integrity labeling, not authentication -- a third party cannot trust it
#: without also trusting "nobody else could compute a SHA-256 hash". Outside
#: these environments, ``Settings`` refuses to start at all unless a real,
#: versioned HMAC signing key (``AGENT_STUDIO_ATTESTATION_SIGNING_KEY`` +
#: ``AGENT_STUDIO_ATTESTATION_SIGNING_KEY_VERSION``) is configured. This is a
#: deliberately independent constant from ``DEMO_IDENTITY_SAFE_ENVIRONMENTS``
#: (even though the values currently match) so the two unrelated security
#: controls can never accidentally share, or be coupled through, one name.
ATTESTATION_UNSIGNED_DIGEST_SAFE_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})

#: Environment names (case-insensitive, whitespace-trimmed) in which
#: ``Settings.trust_platform_identity_headers`` may be enabled without a
#: confirmed ``entra_auth_enforced=True``. ``resolve_identity`` trusts the
#: platform-injected ``x-ms-client-principal`` header outright -- by design,
#: because Azure Container Apps' built-in authentication (EasyAuth /
#: ``authConfigs``) is meant to have already validated the incoming
#: ``Authorization`` bearer token and rejected unauthenticated requests
#: before this process ever sees them; this process deliberately does not
#: re-parse the bearer token itself. That trust is only sound once
#: Container Apps ``authConfigs`` is actually deployed and enforcing.
#: ``entra_auth_enforced`` is populated from the infra-controlled
#: ``RESEARCH_ENTRA_AUTH_ENFORCED`` env var (set by
#: ``infra/modules/container-apps.bicep``'s ``enableEntraAuth`` parameter),
#: letting the running app self-report whether that infra boundary is
#: really active rather than assuming it on faith. This is a deliberately
#: independent constant from ``DEMO_IDENTITY_SAFE_ENVIRONMENTS`` /
#: ``ATTESTATION_UNSIGNED_DIGEST_SAFE_ENVIRONMENTS`` (even though the values
#: currently match) so the three unrelated security controls can never
#: accidentally share, or be coupled through, one name.
ENTRA_AUTH_UNENFORCED_SAFE_ENVIRONMENTS = frozenset({"development", "dev", "local", "test", "testing"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("RESEARCH_ENVIRONMENT", "AZURE_ENV_NAME"),
    )
    execution_mode: Literal["mock", "hosted"] = Field(
        default="mock",
        validation_alias=AliasChoices("RESEARCH_EXECUTION_MODE", "EXECUTION_MODE"),
    )
    foundry_project_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices("FOUNDRY_PROJECT_ENDPOINT", "AZURE_AI_PROJECT_ENDPOINT"),
    )
    coordinator_agent_name: str = Field(
        default="research-coordinator",
        validation_alias="RESEARCH_COORDINATOR_AGENT_NAME",
    )
    managed_identity_client_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AZURE_CLIENT_ID", "AZURE_MANAGED_IDENTITY_CLIENT_ID"),
    )
    cosmos_endpoint: str | None = Field(
        default=None,
        validation_alias="AZURE_COSMOS_ENDPOINT",
    )
    cosmos_database: str = Field(
        default="research",
        validation_alias="AZURE_COSMOS_DATABASE",
    )
    durable_task_connection_string: str | None = Field(
        default=None,
        validation_alias="DURABLE_TASK_SCHEDULER_CONNECTION_STRING",
    )
    storage_blob_endpoint: str | None = Field(
        default=None,
        validation_alias="AZURE_STORAGE_BLOB_ENDPOINT",
    )
    storage_source_container: str = Field(
        default="sources",
        validation_alias="AZURE_STORAGE_SOURCE_CONTAINER",
    )
    agent_studio_bundle_container: str = Field(
        default="agent-studio-bundles",
        validation_alias="AZURE_STORAGE_AGENT_STUDIO_BUNDLE_CONTAINER",
    )
    agent_studio_cosmos_database: str = Field(
        default="agent-studio",
        validation_alias="AZURE_COSMOS_AGENT_STUDIO_DATABASE",
    )
    agent_studio_metadata_container: str = Field(
        default="agentStudioMetadataV1",
        validation_alias="AZURE_COSMOS_AGENT_STUDIO_METADATA_CONTAINER",
    )
    agent_studio_memory_container: str = Field(
        default="agentStudioMemoryV1",
        validation_alias="AZURE_COSMOS_AGENT_STUDIO_MEMORY_CONTAINER",
    )
    agent_studio_audit_container: str = Field(
        default="agentStudioAuditV1",
        validation_alias="AZURE_COSMOS_AGENT_STUDIO_AUDIT_CONTAINER",
    )
    agent_studio_catalog_container: str = Field(
        default="agentStudioCatalogV1",
        validation_alias="AZURE_COSMOS_AGENT_STUDIO_CATALOG_CONTAINER",
    )
    agent_studio_attestation_signing_key: str | None = Field(
        default=None,
        validation_alias="AGENT_STUDIO_ATTESTATION_SIGNING_KEY",
        description=(
            "Shared secret used to HMAC-sign ReleaseAttestation objects. When unset, "
            "ReleaseAttestation falls back to an unkeyed SHA-256 content digest and reports "
            "signature_algorithm='sha256-digest' rather than claiming a keyed signature it "
            "cannot actually provide -- this digest-only fallback is refused at startup "
            "outside ATTESTATION_UNSIGNED_DIGEST_SAFE_ENVIRONMENTS. Must be paired with "
            "agent_studio_attestation_signing_key_version whenever set."
        ),
    )
    agent_studio_attestation_signing_key_version: str | None = Field(
        default=None,
        validation_alias="AGENT_STUDIO_ATTESTATION_SIGNING_KEY_VERSION",
        description=(
            "Version label (e.g. a rotation date or a Key Vault secret version) for "
            "agent_studio_attestation_signing_key, embedded in every ReleaseAttestation it "
            "signs. Required whenever agent_studio_attestation_signing_key is configured, so "
            "a verifier that retains multiple historical secrets (key rotation) can look up "
            "the exact secret an older attestation was actually signed with."
        ),
    )
    agent_studio_app_insights_resource_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "AGENT_STUDIO_APP_INSIGHTS_RESOURCE_ID", "APPLICATIONINSIGHTS_RESOURCE_ID"
        ),
        description=(
            "ARM resource ID (e.g. '/subscriptions/<sid>/resourceGroups/<rg>/providers/"
            "microsoft.insights/components/<name>') of the Application Insights component "
            "queried by the deployment Observability/Monitor read surface via "
            "azure-monitor-query's LogsQueryClient.query_resource. When unset, that surface "
            "is explicitly unavailable (503) rather than silently degraded or fabricated."
        ),
    )
    search_endpoint: str | None = Field(
        default=None,
        validation_alias="AZURE_SEARCH_ENDPOINT",
    )
    search_index_name: str = Field(
        default="research-evidence",
        validation_alias="AZURE_SEARCH_INDEX_NAME",
    )
    connector_gateway_url: str | None = Field(
        default=None,
        validation_alias="RESEARCH_CONNECTOR_GATEWAY_URL",
    )
    connector_gateway_token_scope: str | None = Field(
        default=None,
        validation_alias="RESEARCH_CONNECTOR_GATEWAY_TOKEN_SCOPE",
    )
    allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000"],
        validation_alias="RESEARCH_ALLOWED_ORIGINS",
    )
    allow_demo_identity: bool = Field(
        default=False,
        validation_alias="RESEARCH_ALLOW_DEMO_IDENTITY",
    )
    trust_platform_identity_headers: bool = Field(
        default=False,
        validation_alias="RESEARCH_TRUST_PLATFORM_IDENTITY_HEADERS",
    )
    entra_auth_enforced: bool = Field(
        default=False,
        validation_alias="RESEARCH_ENTRA_AUTH_ENFORCED",
        description=(
            "Self-reported confirmation that Azure Container Apps built-in "
            "authentication (EasyAuth / authConfigs) is actually deployed and "
            "enforcing Entra ID bearer tokens ahead of this process. Set from the "
            "infra-controlled RESEARCH_ENTRA_AUTH_ENFORCED env var (see "
            "infra/modules/container-apps.bicep's enableEntraAuth parameter), never "
            "asserted by this process itself. resolve_identity's trust of the "
            "platform-injected x-ms-client-principal header (gated on "
            "trust_platform_identity_headers) is only sound when this is true; "
            "outside ENTRA_AUTH_UNENFORCED_SAFE_ENVIRONMENTS, enabling "
            "trust_platform_identity_headers without this confirmed is refused at "
            "startup -- see _forbid_unenforced_platform_identity_trust_outside_safe_environments."
        ),
    )
    workspace_tenant_id: str = Field(
        default="demo",
        validation_alias="RESEARCH_WORKSPACE_TENANT_ID",
    )
    workspace_project_id: str = Field(
        default="demo-project",
        validation_alias="RESEARCH_WORKSPACE_PROJECT_ID",
    )
    agent_studio_capability_provider_url: str | None = Field(
        default=None,
        validation_alias="RESEARCH_CAPABILITY_PROVIDER_URL",
        description=(
            "Base URL of the provider integration's discovery HTTP surface "
            "('GET /v1/providers', 'GET /v1/providers/{provider_id}/capabilities'), "
            "translating the flat 'research-assistant.integration-provider.v7' wire "
            "contract into this package's own CapabilityDescriptor/CapabilityInstance "
            "domain types (see agent_studio.capability_discovery.HttpCapabilityDiscoverySource). "
            "When unset, capability discovery is explicitly unavailable "
            "(NullCapabilityDiscoverySource) rather than silently empty."
        ),
    )
    agent_studio_capability_provider_token_scope: str | None = Field(
        default=None,
        validation_alias="RESEARCH_CAPABILITY_PROVIDER_TOKEN_SCOPE",
        description=(
            "OAuth scope requested via ManagedIdentityCredential when calling "
            "agent_studio_capability_provider_url (e.g. "
            "'https://management.azure.com/.default', matching the provider's own "
            "ARM-audience GatewayTokenValidator). Only used when the provider URL is "
            "also configured."
        ),
    )
    #: Adapter-owned defence-in-depth bounds for the provider-v7 HTTP discovery
    #: adapter (``agent_studio.capability_discovery.HttpCapabilityDiscoverySource``).
    #: Every one of these is settings-owned and never derived from the wire,
    #: the requesting user, or a model: an untrusted provider response can never
    #: talk the adapter into unbounded memory, cardinality, fan-out, or wall-clock
    #: cost. Exceeding a bound fails closed with a typed honest "unavailable"
    #: result (catalog-level) or a skipped item with a warning (per provider).
    agent_studio_capability_provider_max_response_bytes: int = Field(
        default=8_000_000,
        gt=0,
        le=134_217_728,
        validation_alias="RESEARCH_CAPABILITY_PROVIDER_MAX_RESPONSE_BYTES",
        description=(
            "Hard cap on the number of bytes read from any single provider discovery "
            "HTTP response before it is JSON-parsed. A response larger than this is "
            "abandoned unread rather than buffered in full."
        ),
    )
    agent_studio_capability_provider_max_providers: int = Field(
        default=250,
        gt=0,
        le=100_000,
        validation_alias="RESEARCH_CAPABILITY_PROVIDER_MAX_PROVIDERS",
        description=(
            "Maximum number of providers the catalog ('GET /v1/providers') may list. "
            "A larger catalog cannot be honestly enumerated within the adapter's "
            "bounds, so discovery reports an explicit unavailable result."
        ),
    )
    agent_studio_capability_provider_max_descriptors_per_provider: int = Field(
        default=500,
        gt=0,
        le=100_000,
        validation_alias="RESEARCH_CAPABILITY_PROVIDER_MAX_DESCRIPTORS",
        description="Maximum capability descriptors accepted from one provider's discovery response.",
    )
    agent_studio_capability_provider_max_instances_per_provider: int = Field(
        default=2_000,
        gt=0,
        le=1_000_000,
        validation_alias="RESEARCH_CAPABILITY_PROVIDER_MAX_INSTANCES",
        description="Maximum discovered instances accepted from one provider's discovery response.",
    )
    agent_studio_capability_provider_max_operations_per_descriptor: int = Field(
        default=200,
        gt=0,
        le=100_000,
        validation_alias="RESEARCH_CAPABILITY_PROVIDER_MAX_OPERATIONS",
        description="Maximum operations accepted on a single capability descriptor.",
    )
    agent_studio_capability_provider_max_concurrency: int = Field(
        default=8,
        gt=0,
        le=256,
        validation_alias="RESEARCH_CAPABILITY_PROVIDER_MAX_CONCURRENCY",
        description=(
            "Maximum concurrent per-provider capability requests the adapter fans out "
            "with; bounds outbound connection pressure independent of catalog size."
        ),
    )
    agent_studio_capability_provider_deadline_seconds: float = Field(
        default=25.0,
        gt=0,
        le=600.0,
        validation_alias="RESEARCH_CAPABILITY_PROVIDER_DEADLINE_SECONDS",
        description=(
            "Overall wall-clock ceiling for a single discovery pass (catalog plus all "
            "per-provider fan-out). Exceeding it yields an honest unavailable result "
            "rather than a partial catalog presented as complete."
        ),
    )

    require_approval_context_resolver: bool = Field(
        default=False,
        validation_alias="RESEARCH_REQUIRE_APPROVAL_CONTEXT_RESOLVER",
        description=(
            "Require a trusted approval-context resolver to be configured. Always "
            "effectively true in production (see approval_context_resolver_required); "
            "this flag lets non-production environments opt in explicitly."
        ),
    )

    @property
    def approval_context_resolver_required(self) -> bool:
        return self.require_approval_context_resolver or self.environment.casefold() in {
            "prod",
            "production",
        }

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("foundry_project_endpoint")
    @classmethod
    def validate_foundry_endpoint(cls, value: str | None) -> str | None:
        if value and not value.startswith("https://"):
            raise ValueError("Foundry project endpoint must use HTTPS")
        return value.rstrip("/") if value else None

    @field_validator("cosmos_endpoint")
    @classmethod
    def validate_cosmos_endpoint(cls, value: str | None) -> str | None:
        if value and not value.startswith("https://"):
            raise ValueError("Cosmos DB endpoint must use HTTPS")
        return value.rstrip("/") if value else None

    @field_validator("storage_blob_endpoint")
    @classmethod
    def validate_storage_endpoint(cls, value: str | None) -> str | None:
        if value and not value.startswith("https://"):
            raise ValueError("Storage Blob endpoint must use HTTPS")
        return value.rstrip("/") if value else None

    @field_validator("search_endpoint")
    @classmethod
    def validate_search_endpoint(cls, value: str | None) -> str | None:
        if value and not value.startswith("https://"):
            raise ValueError("Azure AI Search endpoint must use HTTPS")
        return value.rstrip("/") if value else None

    @field_validator("agent_studio_app_insights_resource_id")
    @classmethod
    def validate_app_insights_resource_id(cls, value: str | None) -> str | None:
        if value and not value.startswith("/subscriptions/"):
            raise ValueError(
                "Application Insights resource ID must be a full ARM resource ID starting "
                "with '/subscriptions/'."
            )
        return value.rstrip("/") if value else None

    @field_validator("connector_gateway_url")
    @classmethod
    def validate_connector_gateway_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlsplit(value)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if parsed.scheme != "https" and not local_http:
            raise ValueError("Connector gateway URL must use HTTPS except for local loopback development")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Connector gateway URL must not contain credentials, query, or fragment")
        return value.rstrip("/")

    @field_validator("agent_studio_capability_provider_url")
    @classmethod
    def validate_capability_provider_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlsplit(value)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if parsed.scheme != "https" and not local_http:
            raise ValueError(
                "Capability provider URL must use HTTPS except for local loopback development"
            )
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Capability provider URL must not contain credentials, query, or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _forbid_demo_identity_outside_safe_environments(self) -> Settings:
        """Fail closed at startup, not at request time.

        ``allow_demo_identity`` issues a fixed, unauthenticated identity
        (``research_assistant_api.identity.DEMO_SANDBOX_SOURCE``) with a
        fabricated researcher/admin group membership. That bypass must be
        an explicit, impossible-to-misconfigure-into-production test/dev
        adapter: refuse to construct ``Settings`` at all if it is enabled
        while ``environment`` is not one of the known-safe local/dev/test
        names, rather than allowing a bad or missing ``RESEARCH_ENVIRONMENT``
        to silently carry the bypass into a production deployment.
        """
        if self.allow_demo_identity and self.environment.strip().lower() not in DEMO_IDENTITY_SAFE_ENVIRONMENTS:
            raise ValueError(
                "RESEARCH_ALLOW_DEMO_IDENTITY may only be enabled when environment is one of "
                f"{sorted(DEMO_IDENTITY_SAFE_ENVIRONMENTS)}; refusing to start with "
                f"environment={self.environment!r} and demo identity enabled."
            )
        return self

    @model_validator(mode="after")
    def _forbid_unversioned_or_missing_attestation_signing_key(self) -> Settings:
        """Fail closed at startup, not at request time.

        A ``ReleaseAttestation`` signed with an unkeyed SHA-256 digest
        (``signature_algorithm == "sha256-digest"``) is honestly labeled as
        integrity-only, never as authentication -- a third party can only
        trust it by also trusting "nobody else could compute a SHA-256
        hash". That is acceptable for local development/tests, but must
        never become the silent production default: refuse to construct
        ``Settings`` at all when no signing key is configured and
        ``environment`` is not one of the known-safe local/dev/test names,
        rather than allowing a bad or missing ``RESEARCH_ENVIRONMENT`` to
        silently carry an unauthenticated attestation into production.

        A configured signing key must also always carry an explicit
        ``agent_studio_attestation_signing_key_version``: an unversioned
        managed secret cannot be rotated or audited, so this is refused
        regardless of environment.
        """
        if self.agent_studio_attestation_signing_key and not self.agent_studio_attestation_signing_key_version:
            raise ValueError(
                "AGENT_STUDIO_ATTESTATION_SIGNING_KEY_VERSION must be set whenever "
                "AGENT_STUDIO_ATTESTATION_SIGNING_KEY is configured, so signed "
                "ReleaseAttestations can be rotated and audited by version."
            )
        if (
            not self.agent_studio_attestation_signing_key
            and self.environment.strip().lower() not in ATTESTATION_UNSIGNED_DIGEST_SAFE_ENVIRONMENTS
        ):
            raise ValueError(
                "AGENT_STUDIO_ATTESTATION_SIGNING_KEY (with "
                "AGENT_STUDIO_ATTESTATION_SIGNING_KEY_VERSION) must be configured outside of "
                f"{sorted(ATTESTATION_UNSIGNED_DIGEST_SAFE_ENVIRONMENTS)} environments; refusing "
                "to start with an unauthenticated sha256-digest-only ReleaseAttestation and "
                f"environment={self.environment!r}."
            )
        return self

    @model_validator(mode="after")
    def _forbid_unenforced_platform_identity_trust_outside_safe_environments(self) -> Settings:
        """Fail closed at startup, not at request time.

        ``trust_platform_identity_headers=True`` tells
        ``identity.resolve_identity`` to trust the platform-injected
        ``x-ms-client-principal`` header outright, without independently
        re-parsing or validating the incoming ``Authorization`` bearer token
        itself -- by design, because Azure Container Apps' built-in
        authentication (EasyAuth / ``authConfigs``) is meant to have already
        validated that token and rejected unauthenticated requests before
        this process ever sees them. That design is only sound once
        Container Apps ``authConfigs`` is actually deployed and enforcing; if
        it is not (e.g. missing infra wiring, or the deployment is reachable
        by a path that bypasses it), a forged ``x-ms-client-principal``
        header would be trusted as if it were a real Entra identity.
        ``entra_auth_enforced`` lets the running app self-report whether that
        infra-level enforcement is really active, sourced from the
        infra-controlled ``RESEARCH_ENTRA_AUTH_ENFORCED`` env var rather than
        asserted by this process itself. Refuse to construct ``Settings`` at
        all when platform identity headers are trusted but Entra enforcement
        is not confirmed and ``environment`` is not one of the known-safe
        local/dev/test names -- the same fail-closed shape as the
        demo-identity and attestation-signing-key guards above.
        """
        if (
            self.trust_platform_identity_headers
            and not self.entra_auth_enforced
            and self.environment.strip().lower() not in ENTRA_AUTH_UNENFORCED_SAFE_ENVIRONMENTS
        ):
            raise ValueError(
                "RESEARCH_ENTRA_AUTH_ENFORCED must be true whenever "
                "RESEARCH_TRUST_PLATFORM_IDENTITY_HEADERS is enabled outside of "
                f"{sorted(ENTRA_AUTH_UNENFORCED_SAFE_ENVIRONMENTS)} environments; refusing to "
                "start with an unconfirmed Container Apps authConfigs boundary and "
                f"environment={self.environment!r}."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
