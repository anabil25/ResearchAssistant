from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.ai.projects import AIProjectClient
from dotenv import load_dotenv

from .capabilities import (
    CapabilityDescriptor,
    CapabilityHandlerResolver,
    OperationClass,
    ProviderContractAdapter,
    ToolRegistration,
    runtime_attested_registration,
)
from .catalog import capabilities_for_manifest
from .contracts import AgentManifest, bind_contracts
from .credentials import get_credential
from .errors import ConfigurationError, HarnessError
from .idempotency import IdempotencyStore
from .middleware import middleware_for_manifest
from .profiles import get_manifest
from .release import ReleaseMetadata, build_release_metadata
from .settings import HarnessSettings
from .tools import tools_for_profile


@dataclass(frozen=True, slots=True)
class PreparedAgent:
    manifest: AgentManifest
    capabilities: tuple[CapabilityDescriptor, ...]
    registrations: tuple[ToolRegistration, ...]


class GovernedAgentFactory:
    def __init__(self, manifest: AgentManifest) -> None:
        self.manifest = manifest

    def build(
        self,
        *,
        client: Any | None = None,
        settings: HarnessSettings | None = None,
        provider_adapter: ProviderContractAdapter | None = None,
        idempotency_store: IdempotencyStore | None = None,
        allow_test_idempotency_store: bool = False,
    ) -> Agent:
        load_dotenv(override=False)
        effective_settings = settings or (HarnessSettings.from_environment() if client is None else None)
        if effective_settings is None:
            raise ConfigurationError(
                "Injected clients require explicit governed runtime settings",
                context={"agent": self.manifest.id},
            )
        self._validate_model_policy(effective_settings)
        if client is None:
            client = _build_foundry_client(effective_settings)
        if _requires_toolbox(self.manifest) and (
            effective_settings is None or effective_settings.toolbox_endpoint is None
        ):
            raise ConfigurationError(
                "Manifest requires a configured Foundry Toolbox endpoint",
                context={"agent": self.manifest.id},
            )
        prepared = self.prepare(
            effective_settings,
            provider_adapter=provider_adapter,
        )
        if _requires_durable_idempotency(prepared.capabilities) and (
            idempotency_store is None
            or (
                not getattr(idempotency_store, "is_durable", False)
                and not allow_test_idempotency_store
            )
        ):
            raise ConfigurationError(
                "Write-capable Hosted Agents require an app-owned durable idempotency store",
                context={"agent": self.manifest.id},
            )
        release = build_release_metadata(
            prepared.manifest,
            model_deployment=effective_settings.model_deployment_name,
            registrations=prepared.registrations,
        )
        contracts = bind_contracts(prepared.manifest)
        return Agent(
            client=client,
            name=prepared.manifest.name,
            instructions=prepared.manifest.instructions,
            tools=tools_for_profile(prepared.manifest, client, effective_settings),
            default_options={
                "store": False,
                "response_format": contracts.output_model,
            },
            middleware=middleware_for_manifest(
                prepared.manifest,
                effective_settings,
                prepared.capabilities,
                prepared.registrations,
                idempotency_store=idempotency_store,
                release_id=release.release_id,
                allow_test_idempotency_store=allow_test_idempotency_store,
            ),
        )

    def capabilities(
        self,
        settings: HarnessSettings | None = None,
    ) -> tuple[Any, ...]:
        return capabilities_for_manifest(self.manifest, settings)

    def release(
        self,
        settings: HarnessSettings,
        *,
        provider_adapter: ProviderContractAdapter | None = None,
    ) -> ReleaseMetadata:
        prepared = self.prepare(
            settings,
            provider_adapter=provider_adapter,
        )
        return build_release_metadata(
            prepared.manifest,
            model_deployment=settings.model_deployment_name,
            registrations=prepared.registrations,
        )

    def readiness(
        self,
        settings: HarnessSettings,
        *,
        provider_adapter: ProviderContractAdapter | None = None,
        idempotency_store: IdempotencyStore | None = None,
    ) -> dict[str, str | bool]:
        readiness = settings.readiness(toolbox_required=_requires_toolbox(self.manifest))
        try:
            prepared = self.prepare(
                settings,
                provider_adapter=provider_adapter,
            )
        except HarnessError:
            readiness["ready"] = False
            return readiness
        requires_durable_idempotency = _requires_durable_idempotency(prepared.capabilities)
        if requires_durable_idempotency:
            durable = idempotency_store is not None and getattr(
                idempotency_store,
                "is_durable",
                False,
            )
            readiness["durable_idempotency"] = durable
            if not durable:
                readiness["ready"] = False
        return readiness

    def resolved_manifest(
        self,
        settings: HarnessSettings,
        *,
        provider_adapter: ProviderContractAdapter | None = None,
    ) -> AgentManifest:
        return self.prepare(
            settings,
            provider_adapter=provider_adapter,
        ).manifest

    def prepare(
        self,
        settings: HarnessSettings,
        *,
        provider_adapter: ProviderContractAdapter | None = None,
        handler_resolver: CapabilityHandlerResolver | None = None,
    ) -> PreparedAgent:
        self._validate_model_policy(settings)
        capabilities = capabilities_for_manifest(self.manifest, settings)
        if self.manifest.capability_bindings and provider_adapter is None:
            raise ConfigurationError(
                "Capability bindings require an attested provider adapter",
                context={"agent": self.manifest.id},
            )
        registrations = (
            tuple(
                runtime_attested_registration(
                    binding,
                    provider_adapter,
                    tenant_id=binding.tenant_scope,
                    project_id=binding.project_scope,
                    handler_resolver=handler_resolver,
                )
                for binding in self.manifest.capability_bindings
            )
            if provider_adapter is not None
            else ()
        )
        return PreparedAgent(
            manifest=self.manifest,
            capabilities=capabilities,
            registrations=registrations,
        )

    def _validate_model_policy(self, settings: HarnessSettings) -> None:
        if settings.model_deployment_name != self.manifest.model_policy.deployment_name:
            raise ConfigurationError(
                "Configured model deployment does not match manifest model policy",
                context={"agent": self.manifest.id},
            )
        actual_version = settings.model_deployment_version or _resolve_model_deployment_version(settings)
        if actual_version != self.manifest.model_policy.pinned_model_version:
            raise ConfigurationError(
                "Configured model version does not match manifest model policy",
                context={"agent": self.manifest.id},
            )


def get_factory(profile_id: str) -> GovernedAgentFactory:
    return GovernedAgentFactory(get_manifest(profile_id))


def _build_foundry_client(settings: HarnessSettings) -> FoundryChatClient:
    return FoundryChatClient(
        project_endpoint=str(settings.foundry_project_endpoint),
        model=settings.model_deployment_name,
        credential=get_credential(settings.managed_identity_client_id),
    )


def _resolve_model_deployment_version(settings: HarnessSettings) -> str:
    with AIProjectClient(
        endpoint=str(settings.foundry_project_endpoint),
        credential=get_credential(settings.managed_identity_client_id),
    ) as project:
        deployment = project.deployments.get(settings.model_deployment_name)
    version = getattr(deployment, "model_version", None)
    if not isinstance(version, str) or not version:
        raise ConfigurationError(
            "Configured deployment is not a versioned Foundry model deployment",
            context={"deployment": settings.model_deployment_name},
        )
    return version


def _requires_toolbox(manifest: AgentManifest) -> bool:
    return any(binding.operation_ref.id.startswith("foundry.toolbox.") for binding in manifest.capability_bindings)


def _requires_durable_idempotency(
    capabilities: tuple[CapabilityDescriptor, ...],
) -> bool:
    return any(
        capability.operation
        in {
            OperationClass.WRITE_REVERSIBLE,
            OperationClass.WRITE_IRREVERSIBLE,
            OperationClass.PRIVILEGED,
        }
        for capability in capabilities
    )
