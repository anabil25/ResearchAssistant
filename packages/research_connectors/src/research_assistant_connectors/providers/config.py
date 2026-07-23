"""Secret-free provider configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .contracts import ApprovalPolicy, AuthMode, Idempotency, Maturity, OperationClass


@dataclass(frozen=True, slots=True)
class AuthConfig:
    mode: AuthMode
    token_scope: str | None = None
    secret_name: str | None = None
    header_name: str | None = None


@dataclass(frozen=True, slots=True)
class FoundryConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(AuthMode.MANAGED_IDENTITY, "https://ai.azure.com/.default")
    models_path: str | None = None
    deployments_path: str | None = None
    agents_path: str | None = None
    connections_path: str | None = None
    vector_stores_path: str | None = None
    responses_path: str | None = None
    api_version: str = "2025-05-01"


@dataclass(frozen=True, slots=True)
class SearchConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(AuthMode.MANAGED_IDENTITY, "https://search.azure.com/.default")
    api_version: str = "2025-09-01"


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


@dataclass(frozen=True, slots=True)
class BlobConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(AuthMode.MANAGED_IDENTITY, "https://storage.azure.com/.default")
    api_version: str = "2023-11-03"


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


@dataclass(frozen=True, slots=True)
class OpenAPIConfig:
    base_url: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(AuthMode.NONE)
    document_url: str | None = None
    document_auth: AuthConfig = AuthConfig(AuthMode.NONE)
    document: dict[str, Any] | None = field(default=None, repr=False)
    operation_policies: tuple[OpenAPIOperationPolicy, ...] = ()


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


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig
    owner: str | None = None
    api_version: str = "2022-11-28"


@dataclass(frozen=True, slots=True)
class GraphConfig:
    endpoint: str | None
    tenant_id: str | None
    auth: AuthConfig = AuthConfig(AuthMode.MANAGED_IDENTITY, "https://graph.microsoft.com/.default")
    sites_path: str = "/sites?search=*"
    discover_items: bool = True


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
