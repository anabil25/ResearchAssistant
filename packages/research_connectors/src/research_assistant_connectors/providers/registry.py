"""Provider factory and registry."""

from __future__ import annotations

from types import MappingProxyType
from typing import cast

from .blob import AzureBlobProvider
from .config import (
    BlobConfig,
    FoundryConfig,
    FunctionsConfig,
    GitHubConfig,
    GraphConfig,
    MCPConfig,
    OpenAPIConfig,
    ProviderConfig,
    ProviderEnvironment,
    SearchConfig,
    WebhookConfig,
)
from .contracts import DiscoveryResult, InvocationContext, Provider
from .foundry import FoundryProvider
from .functions import AzureFunctionsProvider
from .github import GitHubProvider
from .graph import MicrosoftGraphProvider
from .mcp import MCPStreamableHTTPProvider
from .openapi import OpenAPIProvider
from .search import AzureAISearchProvider
from .webhook import WebhookProvider


class ProviderFactory:
    @staticmethod
    def create(config: ProviderConfig) -> Provider:
        if isinstance(config, FoundryConfig):
            return cast(Provider, FoundryProvider(config))
        if isinstance(config, SearchConfig):
            return cast(Provider, AzureAISearchProvider(config))
        if isinstance(config, FunctionsConfig):
            return cast(Provider, AzureFunctionsProvider(config))
        if isinstance(config, BlobConfig):
            return cast(Provider, AzureBlobProvider(config))
        if isinstance(config, MCPConfig):
            return cast(Provider, MCPStreamableHTTPProvider(config))
        if isinstance(config, OpenAPIConfig):
            return cast(Provider, OpenAPIProvider(config))
        if isinstance(config, WebhookConfig):
            return cast(Provider, WebhookProvider(config))
        if isinstance(config, GitHubConfig):
            return cast(Provider, GitHubProvider(config))
        if isinstance(config, GraphConfig):
            return cast(Provider, MicrosoftGraphProvider(config))
        raise TypeError(f"Unsupported provider configuration: {type(config).__name__}")


class ProviderRegistry:
    def __init__(self, providers: tuple[Provider, ...] = ()) -> None:
        indexed = {provider.descriptor.provider_id: provider for provider in providers}
        if len(indexed) != len(providers):
            raise ValueError("Provider identifiers must be unique")
        self._providers = indexed

    @classmethod
    def from_environment(cls, environment: ProviderEnvironment) -> ProviderRegistry:
        return cls(tuple(ProviderFactory.create(config) for config in environment.providers))

    @property
    def providers(self) -> MappingProxyType[str, Provider]:
        return MappingProxyType(self._providers)

    def get(self, provider_id: str) -> Provider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"Unknown provider: {provider_id}") from exc

    def discover_all(self, context: InvocationContext) -> MappingProxyType[str, DiscoveryResult]:
        return MappingProxyType(
            {provider_id: provider.discover(context) for provider_id, provider in self._providers.items()}
        )
