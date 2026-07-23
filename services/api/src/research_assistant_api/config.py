from __future__ import annotations

from functools import lru_cache
from typing import Literal
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
        default=True,
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

@lru_cache
def get_settings() -> Settings:
    return Settings()
