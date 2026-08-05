from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    foundry_project_endpoint: str = Field(
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
    cosmos_endpoint: str = Field(
        validation_alias="AZURE_COSMOS_ENDPOINT",
    )
    cosmos_database: str = Field(
        default="research",
        validation_alias="AZURE_COSMOS_DATABASE",
    )
    storage_blob_endpoint: str = Field(
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
    search_endpoint: str = Field(
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
    entra_auth_enforced: bool = Field(
        default=False,
        validation_alias="RESEARCH_ENTRA_AUTH_ENFORCED",
        description=(
            "The single authentication switch. True means an authenticating gateway "
            "(Azure Container Apps built-in authentication / APIM) validates the Entra "
            "bearer token and injects x-ms-client-principal ahead of this process, so "
            "identity.resolve_identity trusts that header and rejects any request "
            "without it. False means nothing is in front of the API: the header is "
            "ignored outright (a forged one grants nothing) and the local developer "
            "identity is issued instead. Set from the infra-controlled "
            "RESEARCH_ENTRA_AUTH_ENFORCED env var (see infra/modules/container-apps.bicep's "
            "enableEntraAuth parameter), never asserted by this process itself."
        ),
    )
    workspace_tenant_id: str = Field(
        validation_alias="RESEARCH_WORKSPACE_TENANT_ID",
    )
    workspace_project_id: str = Field(
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
    def validate_foundry_endpoint(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Foundry project endpoint must use HTTPS")
        return value.rstrip("/")

    @field_validator("cosmos_endpoint")
    @classmethod
    def validate_cosmos_endpoint(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Cosmos DB endpoint must use HTTPS")
        return value.rstrip("/")

    @field_validator("storage_blob_endpoint")
    @classmethod
    def validate_storage_endpoint(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Storage Blob endpoint must use HTTPS")
        return value.rstrip("/")

    @field_validator("search_endpoint")
    @classmethod
    def validate_search_endpoint(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("Azure AI Search endpoint must use HTTPS")
        return value.rstrip("/")

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


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
