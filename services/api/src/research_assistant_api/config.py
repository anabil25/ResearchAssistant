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
            "cannot actually provide."
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
    workspace_tenant_id: str = Field(
        default="demo",
        validation_alias="RESEARCH_WORKSPACE_TENANT_ID",
    )
    workspace_project_id: str = Field(
        default="demo-project",
        validation_alias="RESEARCH_WORKSPACE_PROJECT_ID",
    )

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

@lru_cache
def get_settings() -> Settings:
    return Settings()
