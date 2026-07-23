from __future__ import annotations

from typing import Any

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.ai.projects import AIProjectClient
from dotenv import load_dotenv

from .capabilities import (
    CapabilityDescriptor,
    resolved_instance_fingerprint,
    template_instance_fingerprint,
)
from .catalog import capabilities_for_manifest
from .contracts import AgentManifest, bind_contracts
from .credentials import get_credential
from .errors import ConfigurationError, HarnessError, StaleCapabilityBindingError
from .middleware import middleware_for_manifest
from .profiles import get_manifest
from .release import ReleaseMetadata, build_release_metadata
from .settings import HarnessSettings
from .tools import tools_for_profile


class GovernedAgentFactory:
    def __init__(self, manifest: AgentManifest) -> None:
        self.manifest = manifest

    def build(
        self,
        *,
        client: Any | None = None,
        settings: HarnessSettings | None = None,
    ) -> Agent:
        load_dotenv(override=False)
        effective_settings = settings or (
            HarnessSettings.from_environment() if client is None else None
        )
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
        capabilities = capabilities_for_manifest(self.manifest, effective_settings)
        resolved_manifest = self._resolve_manifest(
            effective_settings,
            capabilities,
        )
        contracts = bind_contracts(resolved_manifest)
        return Agent(
            client=client,
            name=resolved_manifest.name,
            instructions=resolved_manifest.instructions,
            tools=tools_for_profile(resolved_manifest, client, effective_settings),
            default_options={
                "store": False,
                "response_format": contracts.output_model,
            },
            middleware=middleware_for_manifest(
                resolved_manifest,
                effective_settings,
                capabilities,
            ),
        )

    def capabilities(
        self,
        settings: HarnessSettings | None = None,
    ) -> tuple[Any, ...]:
        return capabilities_for_manifest(self.manifest, settings)

    def release(self, settings: HarnessSettings) -> ReleaseMetadata:
        manifest = self.resolved_manifest(settings)
        return build_release_metadata(
            manifest,
            model_deployment=settings.model_deployment_name,
        )

    def readiness(self, settings: HarnessSettings) -> dict[str, str | bool]:
        readiness = settings.readiness(toolbox_required=_requires_toolbox(self.manifest))
        try:
            self.resolved_manifest(settings)
        except HarnessError:
            readiness["ready"] = False
        return readiness

    def resolved_manifest(self, settings: HarnessSettings) -> AgentManifest:
        self._validate_model_policy(settings)
        capabilities = capabilities_for_manifest(self.manifest, settings)
        return self._resolve_manifest(settings, capabilities)

    def _validate_model_policy(self, settings: HarnessSettings) -> None:
        if settings.model_deployment_name != self.manifest.model_policy.deployment_name:
            raise ConfigurationError(
                "Configured model deployment does not match manifest model policy",
                context={"agent": self.manifest.id},
            )
        actual_version = settings.model_deployment_version or _resolve_model_deployment_version(
            settings
        )
        if actual_version != self.manifest.model_policy.pinned_model_version:
            raise ConfigurationError(
                "Configured model version does not match manifest model policy",
                context={"agent": self.manifest.id},
            )

    def _resolve_manifest(
        self,
        settings: HarnessSettings,
        capabilities: tuple[CapabilityDescriptor, ...],
    ) -> AgentManifest:
        descriptors = {item.id: item for item in capabilities}
        bindings = []
        for binding in self.manifest.capability_bindings:
            descriptor = descriptors[binding.capability_id]
            current_fingerprint = resolved_instance_fingerprint(
                binding,
                descriptor,
                project_endpoint=str(settings.foundry_project_endpoint),
                destination_endpoint=(
                    str(settings.toolbox_endpoint)
                    if binding.operation_id.startswith("foundry.toolbox.")
                    and settings.toolbox_endpoint is not None
                    else None
                ),
            )
            template_fingerprint = template_instance_fingerprint(binding)
            if binding.instance_fingerprint not in {
                template_fingerprint,
                current_fingerprint,
            }:
                raise StaleCapabilityBindingError(
                    "Capability binding targets changed instance configuration",
                    context={
                        "capability": binding.capability_id,
                        "instance_ref": binding.instance_ref,
                        "expected_fingerprint": binding.instance_fingerprint,
                        "current_fingerprint": current_fingerprint,
                    },
                )
            bindings.append(
                binding.model_copy(
                    update={"instance_fingerprint": current_fingerprint}
                )
            )
        return self.manifest.model_copy(
            update={"capability_bindings": tuple(bindings)}
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
    return any(
        binding.operation_id.startswith("foundry.toolbox.")
        for binding in manifest.capability_bindings
    )
