"""Secret-free provider configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .contracts import ApprovalPolicy, AuthMode, Idempotency, Maturity, OperationClass

DEFAULT_UPLOAD_BYTES = 4 * 1024 * 1024
GRAPH_SIMPLE_UPLOAD_MAX_BYTES = 250_000_000


def validate_configured_url(value: str | None) -> None:
    if value is None:
        return
    try:
        parsed = urlsplit(value)
    except ValueError:
        return
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Configured provider URLs cannot contain userinfo")
    try:
        _ = parsed.port
    except ValueError:
        return


@dataclass(frozen=True, slots=True)
class AuthConfig:
    mode: AuthMode
    token_scope: str | None = None
    secret_name: str | None = None
    header_name: str | None = None
    connection_ref: str | None = None
    connection_version: str = "1"
    identity_mode: str | None = None
    authorized_roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.connection_version or self.identity_mode == "":
            raise ValueError("Connection version and configured identity mode must be non-empty")
        if any(not role for role in self.authorized_roles) or len(set(self.authorized_roles)) != len(
            self.authorized_roles
        ):
            raise ValueError("Authorized connection roles must be non-empty and unique")

    @property
    def connection_scopes(self) -> tuple[str, ...]:
        return (self.token_scope,) if self.token_scope else ()

    @property
    def effective_identity_mode(self) -> str:
        return self.identity_mode or self.mode.value


@dataclass(frozen=True, slots=True)
class FoundryConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(
        AuthMode.MANAGED_IDENTITY,
        "https://ai.azure.com/.default",
        connection_ref="foundry-project-managed-identity",
        identity_mode="managed_identity",
        authorized_roles=("Azure AI User",),
    )
    models_path: str | None = None
    deployments_path: str | None = None
    agents_path: str | None = None
    connections_path: str | None = None
    vector_stores_path: str | None = None
    responses_path: str | None = None
    api_version: str = "2025-05-01"

    def __post_init__(self) -> None:
        validate_configured_url(self.endpoint)


@dataclass(frozen=True, slots=True)
class SearchConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(
        AuthMode.MANAGED_IDENTITY,
        "https://search.azure.com/.default",
        connection_ref="azure-search-managed-identity",
        identity_mode="managed_identity",
        authorized_roles=("Search Index Data Reader",),
    )
    api_version: str = "2025-09-01"

    def __post_init__(self) -> None:
        validate_configured_url(self.endpoint)


@dataclass(frozen=True, slots=True)
class FunctionPolicy:
    name: str
    operation_class: OperationClass = OperationClass.PRIVILEGED
    approval_policy: ApprovalPolicy = ApprovalPolicy.REQUIRED
    idempotency: Idempotency = Idempotency.NONE
    maturity: Maturity = Maturity.UNKNOWN


@dataclass(frozen=True, slots=True)
class FunctionsConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig
    discovery_url: str | None = None
    discovery_style: str = "http"
    discovery_auth: AuthConfig | None = None
    invoke_path_template: str = "/api/{name}"
    function_policies: tuple[FunctionPolicy, ...] = ()

    def __post_init__(self) -> None:
        validate_configured_url(self.endpoint)
        validate_configured_url(self.discovery_url)


@dataclass(frozen=True, slots=True)
class BlobConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(
        AuthMode.MANAGED_IDENTITY,
        "https://storage.azure.com/.default",
        connection_ref="azure-blob-managed-identity",
        identity_mode="managed_identity",
        authorized_roles=("Storage Blob Data Contributor",),
    )
    api_version: str = "2023-11-03"
    max_upload_bytes: int = DEFAULT_UPLOAD_BYTES

    def __post_init__(self) -> None:
        validate_configured_url(self.endpoint)
        if self.max_upload_bytes <= 0:
            raise ValueError("Blob max_upload_bytes must be positive")


@dataclass(frozen=True, slots=True)
class MCPToolPolicy:
    name: str
    operation_class: OperationClass = OperationClass.PRIVILEGED
    approval_policy: ApprovalPolicy = ApprovalPolicy.REQUIRED
    idempotency: Idempotency = Idempotency.NONE
    maturity: Maturity = Maturity.UNKNOWN


@dataclass(frozen=True, slots=True)
class MCPConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(AuthMode.NONE)
    protocol_version: str = "2025-06-18"
    tool_policies: tuple[MCPToolPolicy, ...] = ()

    def __post_init__(self) -> None:
        validate_configured_url(self.endpoint)


@dataclass(frozen=True, slots=True)
class OpenAPIConfig:
    base_url: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(AuthMode.NONE)
    document_url: str | None = None
    document_auth: AuthConfig = AuthConfig(AuthMode.NONE)
    document: dict[str, Any] | None = field(default=None, repr=False)
    operation_policies: tuple[OpenAPIOperationPolicy, ...] = ()

    def __post_init__(self) -> None:
        validate_configured_url(self.base_url)
        validate_configured_url(self.document_url)


@dataclass(frozen=True, slots=True)
class OpenAPIOperationPolicy:
    operation_id: str
    operation_class: OperationClass
    approval_policy: ApprovalPolicy
    idempotency: Idempotency | None = None
    maturity: Maturity = Maturity.UNKNOWN


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    destination_url: str | None
    tenant_id: str | None
    operation_id: str
    auth: AuthConfig = AuthConfig(AuthMode.NONE)
    method: str = "POST"
    signing_algorithm: str | None = None
    signature_header: str = "X-Signature"
    health_method: str | None = "HEAD"

    def __post_init__(self) -> None:
        validate_configured_url(self.destination_url)


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig
    owner: str | None = None
    api_version: str = "2022-11-28"

    def __post_init__(self) -> None:
        validate_configured_url(self.endpoint)


@dataclass(frozen=True, slots=True)
class GraphConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(
        AuthMode.MANAGED_IDENTITY,
        "https://graph.microsoft.com/.default",
        connection_ref="microsoft-graph-managed-identity",
        identity_mode="managed_identity",
        authorized_roles=("Sites.Selected",),
    )
    sites_path: str = "/sites?search=*"
    discover_items: bool = True
    max_upload_bytes: int = DEFAULT_UPLOAD_BYTES

    def __post_init__(self) -> None:
        validate_configured_url(self.endpoint)
        if not 0 < self.max_upload_bytes <= GRAPH_SIMPLE_UPLOAD_MAX_BYTES:
            raise ValueError("Graph max_upload_bytes must be between 1 and the 250 MB simple-upload limit")


ProviderConfig = (
    FoundryConfig
    | SearchConfig
    | FunctionsConfig
    | BlobConfig
    | MCPConfig
    | OpenAPIConfig
    | WebhookConfig
    | GitHubConfig
    | GraphConfig
)


@dataclass(frozen=True, slots=True)
class ProviderEnvironment:
    name: str
    tenant_id: str
    providers: tuple[ProviderConfig, ...]

    def __post_init__(self) -> None:
        if not self.name or not self.tenant_id:
            raise ValueError("Provider environment name and tenant boundary are required")
        if any(config.tenant_id != self.tenant_id for config in self.providers):
            raise ValueError("Every provider must use the environment tenant boundary")
