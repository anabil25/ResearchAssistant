"""Project-deployed model discovery.

Agent Studio only ever offers models that are *actually deployed* on the
target Foundry project. It never invents or assumes a model name. When no
Foundry project is configured, discovery is explicitly unavailable (raises
``ModelDiscoveryError``) rather than returning a fabricated or cached list.
"""

from __future__ import annotations

from typing import Protocol

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

from research_assistant_api.agent_studio.models import ModelDeploymentRef
from research_assistant_api.config import Settings


class ModelDiscoveryError(RuntimeError):
    pass


class ModelDiscovery(Protocol):
    def list_deployed_models(self) -> tuple[ModelDeploymentRef, ...]: ...


class InMemoryModelDiscovery:
    """Explicit, test/offline-only model discovery backed by a fixed list.

    This must never be wired in a cloud/production path; it exists so unit
    tests can exercise the platform deterministically without a live Foundry
    project.
    """

    def __init__(self, models: tuple[ModelDeploymentRef, ...] = ()) -> None:
        self._models = models

    def list_deployed_models(self) -> tuple[ModelDeploymentRef, ...]:
        return self._models


class UnavailableModelDiscovery:
    """Explicit cloud-unavailable path: no Foundry project is configured."""

    def list_deployed_models(self) -> tuple[ModelDeploymentRef, ...]:
        raise ModelDiscoveryError(
            "No Foundry project endpoint is configured; project-deployed model discovery is unavailable."
        )


class AIProjectModelDiscovery:
    """Live discovery of a Foundry project's deployed models via the SDK.

    The ``azure-ai-projects`` SDK surface for listing deployments is still
    evolving (beta); this wraps the call defensively so a shape change
    surfaces as a clear ``ModelDiscoveryError`` rather than silently
    fabricating results.
    """

    def __init__(self, endpoint: str, credential: TokenCredential) -> None:
        self._endpoint = endpoint
        self._credential = credential

    def list_deployed_models(self) -> tuple[ModelDeploymentRef, ...]:
        client = AIProjectClient(endpoint=self._endpoint, credential=self._credential, allow_preview=True)
        try:
            deployments = list(client.deployments.list())
        except Exception as exc:  # surfaced as a typed discovery error
            raise ModelDiscoveryError(
                f"Listing deployed models for project {self._endpoint} failed."
            ) from exc
        results: list[ModelDeploymentRef] = []
        for deployment in deployments:
            name = getattr(deployment, "name", None)
            model = getattr(deployment, "model_name", None) or getattr(deployment, "name", None)
            model_format = getattr(deployment, "model_format", None) or getattr(deployment, "type", None) or "unknown"
            capacity = getattr(deployment, "capacity", None)
            if not name or not model:
                continue
            results.append(
                ModelDeploymentRef(
                    deployment_name=str(name),
                    model_name=str(model),
                    model_format=str(model_format),
                    capacity=int(capacity) if isinstance(capacity, int) else None,
                )
            )
        return tuple(results)


def _credential(client_id: str | None) -> TokenCredential:
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


def build_model_discovery(settings: Settings) -> ModelDiscovery:
    if not settings.foundry_project_endpoint:
        return UnavailableModelDiscovery()
    return AIProjectModelDiscovery(
        settings.foundry_project_endpoint,
        _credential(settings.managed_identity_client_id),
    )
