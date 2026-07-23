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
from .contracts import DiscoveryResult, InvocationContext, PlatformProvider
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
    def create(config: ProviderConfig) -> PlatformProvider:
        if isinstance(config, FoundryConfig):
            return cast(PlatformProvider, FoundryProvider(config))
        if isinstance(config, SearchConfig):
            return cast(PlatformProvider, AzureAISearchProvider(config))
        if isinstance(config, FunctionsConfig):
            return cast(PlatformProvider, AzureFunctionsProvider(config))
        if isinstance(config, BlobConfig):
            return cast(PlatformProvider, AzureBlobProvider(config))
        if isinstance(config, MCPConfig):
            return cast(PlatformProvider, MCPStreamableHTTPProvider(config))
        if isinstance(config, OpenAPIConfig):
            return cast(PlatformProvider, OpenAPIProvider(config))
        if isinstance(config, WebhookConfig):
            return cast(PlatformProvider, WebhookProvider(config))
        if isinstance(config, GitHubConfig):
            return cast(PlatformProvider, GitHubProvider(config))
        if isinstance(config, GraphConfig):
            return cast(PlatformProvider, MicrosoftGraphProvider(config))
        raise TypeError(f"Unsupported provider configuration: {type(config).__name__}")


class ProviderRegistry:
    def __init__(self, providers: tuple[PlatformProvider, ...] = ()) -> None:
        indexed = {provider.descriptor.provider_id: provider for provider in providers}
        if len(indexed) != len(providers):
            raise ValueError("Provider identifiers must be unique")
        self._providers = indexed

    @classmethod
    def from_environment(cls, environment: ProviderEnvironment) -> ProviderRegistry:
        return cls(tuple(ProviderFactory.create(config) for config in environment.providers))

    @property
    def providers(self) -> MappingProxyType[str, PlatformProvider]:
        return MappingProxyType(self._providers)

    def get(self, provider_id: str) -> PlatformProvider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"Unknown provider: {provider_id}") from exc

    def discover_all(self, context: InvocationContext) -> MappingProxyType[str, DiscoveryResult]:
        return MappingProxyType(
            {provider_id: provider.discover(context) for provider_id, provider in self._providers.items()}
        )
