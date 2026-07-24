"""Provider factory and registry."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from threading import Event
from types import MappingProxyType
from typing import TypeVar, cast

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
from .contracts import (
    CapabilityBinding,
    CapabilityInstance,
    DiscoveryResult,
    HealthReport,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    PlatformProvider,
    Provider,
    ProviderDescriptor,
    ProviderTimeoutError,
    ValidationReport,
)
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


_ResultT = TypeVar("_ResultT")


class AsyncProviderAdapter:
    """Deadline-aware async boundary for a synchronous concrete provider."""

    def __init__(self, provider: PlatformProvider) -> None:
        self._provider = provider

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._provider.descriptor

    async def _run(
        self,
        context: InvocationContext,
        operation: Callable[[InvocationContext], _ResultT],
    ) -> _ResultT:
        cancelled = Event()
        caller_cancelled = context.is_cancelled
        worker_context = replace(
            context,
            is_cancelled=lambda: cancelled.is_set() or caller_cancelled(),
        )
        remaining = context.remaining_seconds(provider_id=self.descriptor.provider_id)

        def execute() -> _ResultT:
            worker_context.raise_if_cancelled_or_expired(
                provider_id=self.descriptor.provider_id
            )
            result = operation(worker_context)
            worker_context.raise_if_cancelled_or_expired(
                provider_id=self.descriptor.provider_id
            )
            return result

        task = asyncio.create_task(asyncio.to_thread(execute))
        try:
            if remaining is None:
                return await task
            return await asyncio.wait_for(task, timeout=remaining)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except TimeoutError as exc:
            cancelled.set()
            raise ProviderTimeoutError(
                "Provider operation exceeded the invocation deadline",
                provider_id=self.descriptor.provider_id,
            ) from exc

    async def discover(self, context: InvocationContext) -> DiscoveryResult:
        return await self._run(context, self._provider.discover)

    async def validate(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> ValidationReport:
        return await self._run(
            context,
            lambda worker_context: self._provider.validate(target, worker_context),
        )

    async def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport:
        return await self._run(
            context,
            lambda worker_context: self._provider.health(target, worker_context),
        )

    async def invoke(
        self,
        request: InvocationRequest,
        context: InvocationContext,
    ) -> InvocationResult:
        return await self._run(
            context,
            lambda worker_context: self._provider.invoke(request, worker_context),
        )


class AsyncProviderRegistry:
    def __init__(self, providers: tuple[Provider, ...] = ()) -> None:
        indexed = {provider.descriptor.provider_id: provider for provider in providers}
        if len(indexed) != len(providers):
            raise ValueError("Provider identifiers must be unique")
        self._providers = indexed

    @classmethod
    def from_environment(
        cls,
        environment: ProviderEnvironment,
    ) -> AsyncProviderRegistry:
        return cls(
            tuple(
                AsyncProviderAdapter(ProviderFactory.create(config))
                for config in environment.providers
            )
        )

    @property
    def providers(self) -> MappingProxyType[str, Provider]:
        return MappingProxyType(self._providers)

    def get(self, provider_id: str) -> Provider:
        try:
            return self._providers[provider_id]
        except KeyError as exc:
            raise KeyError(f"Unknown provider: {provider_id}") from exc

    async def discover_all(
        self,
        context: InvocationContext,
    ) -> MappingProxyType[str, DiscoveryResult]:
        provider_ids = tuple(self._providers)
        discoveries = await asyncio.gather(
            *(self._providers[provider_id].discover(context) for provider_id in provider_ids)
        )
        return MappingProxyType(dict(zip(provider_ids, discoveries, strict=True)))
