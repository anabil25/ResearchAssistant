from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.ai.projects import AIProjectClient
from dotenv import load_dotenv

from .approvals import ApprovalConsumptionAdapter
from .capabilities import (
    ApprovalMode,
    CapabilityDescriptor,
    CapabilityHandlerResolver,
    OperationClass,
    ProviderContractAdapter,
    ToolRegistration,
    runtime_attested_registration,
)
from .catalog import capabilities_for_manifest
from .contracts import (
    AgentManifest,
    DeploymentScope,
    MemoryScope,
    bind_contracts,
    bind_deployment_scope,
    lenient_output_model,
)
from .credentials import get_credential
from .errors import ConfigurationError, HarnessError
from .idempotency import IdempotencyStore
from .middleware import middleware_for_manifest
from .profiles import get_manifest
from .release import (
    ReleaseAttestor,
    ReleaseMetadata,
    build_release_metadata,
    validate_release_attestation,
)
from .settings import HarnessSettings
from .state import ConversationStore, LongTermMemoryStore
from .telemetry import GovernanceAuditSink
from .tools import tools_for_profile


@dataclass(frozen=True, slots=True)
class PreparedAgent:
    manifest: AgentManifest
    capabilities: tuple[CapabilityDescriptor, ...]
    registrations: tuple[ToolRegistration, ...]


@dataclass(frozen=True, slots=True)
class RuntimeBootstrap:
    prepared: PreparedAgent
    release: ReleaseMetadata


class GovernedAgentFactory:
    def __init__(self, manifest: AgentManifest) -> None:
        self.manifest = manifest

    def build_hosted(
        self,
        *,
        client: Any | None = None,
        settings: HarnessSettings | None = None,
        trusted_tenant_id: str | None = None,
        trusted_project_id: str | None = None,
        conversation_store: ConversationStore | None = None,
    ) -> Agent:
        """Build a Foundry Hosted Agent with platform-managed tools and lifecycle."""
        load_dotenv(override=False)
        effective_settings = settings or HarnessSettings.from_environment()
        prepared = self.prepare_hosted(
            effective_settings,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
        )
        deployment_scope = prepared.manifest.deployment_scope
        if client is None:
            client = _build_foundry_client(effective_settings)
        contracts = bind_contracts(prepared.manifest)
        return Agent(
            client=client,
            name=prepared.manifest.name,
            instructions=prepared.manifest.instructions,
            tools=tools_for_profile(prepared.manifest, client, effective_settings),
            default_options={
                "store": False,
                "response_format": lenient_output_model(contracts.output_model),
            },
            middleware=middleware_for_manifest(
                prepared.manifest,
                effective_settings,
                prepared.capabilities,
                (),
                platform_managed_tools=True,
                conversation_store=conversation_store,
                trusted_tenant_id=(
                    deployment_scope.tenant_id
                    if deployment_scope is not None
                    else None
                ),
                trusted_project_id=(
                    deployment_scope.project_id
                    if deployment_scope is not None
                    else None
                ),
            ),
        )

    def build(
        self,
        *,
        client: Any | None = None,
        settings: HarnessSettings | None = None,
        provider_adapter: ProviderContractAdapter | None = None,
        trusted_tenant_id: str | None = None,
        trusted_project_id: str | None = None,
        idempotency_store: IdempotencyStore | None = None,
        approval_adapter: ApprovalConsumptionAdapter | None = None,
        release_attestor: ReleaseAttestor | None = None,
        conversation_store: ConversationStore | None = None,
        long_term_memory_store: LongTermMemoryStore | None = None,
        audit_sink: GovernanceAuditSink | None = None,
        allow_test_idempotency_store: bool = False,
        allow_test_approval_adapter: bool = False,
        allow_test_release_attestor: bool = False,
    ) -> Agent:
        load_dotenv(override=False)
        effective_settings = settings or (HarnessSettings.from_environment() if client is None else None)
        if effective_settings is None:
            raise ConfigurationError(
                "Injected clients require explicit governed runtime settings",
                context={"agent": self.manifest.id},
            )
        self._validate_model_policy(effective_settings)
        bootstrap = self.bootstrap_runtime(
            effective_settings,
            provider_adapter=provider_adapter,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
            idempotency_store=idempotency_store,
            approval_adapter=approval_adapter,
            release_attestor=release_attestor,
            conversation_store=conversation_store,
            long_term_memory_store=long_term_memory_store,
            allow_test_idempotency_store=allow_test_idempotency_store,
            allow_test_approval_adapter=allow_test_approval_adapter,
            allow_test_release_attestor=allow_test_release_attestor,
        )
        prepared = bootstrap.prepared
        release = bootstrap.release
        deployment_scope = prepared.manifest.deployment_scope
        if client is None:
            client = _build_foundry_client(effective_settings)
        contracts = bind_contracts(prepared.manifest)
        return Agent(
            client=client,
            name=prepared.manifest.name,
            instructions=prepared.manifest.instructions,
            tools=tools_for_profile(prepared.manifest, client, effective_settings),
            default_options={
                "store": False,
                "response_format": lenient_output_model(contracts.output_model),
            },
            middleware=middleware_for_manifest(
                prepared.manifest,
                effective_settings,
                prepared.capabilities,
                prepared.registrations,
                idempotency_store=idempotency_store,
                approval_adapter=approval_adapter,
                release_id=release.release_id,
                allow_test_idempotency_store=allow_test_idempotency_store,
                allow_test_approval_adapter=allow_test_approval_adapter,
                conversation_store=conversation_store,
                audit_sink=audit_sink,
                trusted_tenant_id=(
                    deployment_scope.tenant_id
                    if deployment_scope is not None
                    else None
                ),
                trusted_project_id=(
                    deployment_scope.project_id
                    if deployment_scope is not None
                    else None
                ),
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
        trusted_tenant_id: str | None = None,
        trusted_project_id: str | None = None,
    ) -> ReleaseMetadata:
        prepared = self.prepare(
            settings,
            provider_adapter=provider_adapter,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
        )
        return build_release_metadata(
            prepared.manifest,
            model_deployment=settings.model_deployment_name,
            source_tree_digest=settings.source_tree_digest,
            registrations=prepared.registrations,
        )

    def readiness(
        self,
        settings: HarnessSettings,
        *,
        provider_adapter: ProviderContractAdapter | None = None,
        trusted_tenant_id: str | None = None,
        trusted_project_id: str | None = None,
        idempotency_store: IdempotencyStore | None = None,
        approval_adapter: ApprovalConsumptionAdapter | None = None,
        release_attestor: ReleaseAttestor | None = None,
        conversation_store: ConversationStore | None = None,
        long_term_memory_store: LongTermMemoryStore | None = None,
    ) -> dict[str, str | bool]:
        readiness = settings.readiness(
            toolbox_required=_requires_toolbox(self.manifest),
            deployment_scope_required=bool(self.manifest.capability_bindings),
        )
        try:
            prepared = self.prepare(
                settings,
                provider_adapter=provider_adapter,
                trusted_tenant_id=trusted_tenant_id,
                trusted_project_id=trusted_project_id,
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
        requires_durable_approval = _requires_durable_approval(prepared.capabilities)
        if requires_durable_approval:
            durable_approval = approval_adapter is not None and getattr(
                approval_adapter,
                "is_durable",
                False,
            )
            readiness["durable_approval"] = durable_approval
            if not durable_approval:
                readiness["ready"] = False
        try:
            release = build_release_metadata(
                prepared.manifest,
                model_deployment=settings.model_deployment_name,
                source_tree_digest=settings.source_tree_digest,
                registrations=prepared.registrations,
            )
            validate_release_attestation(
                release,
                prepared.manifest,
                release_attestor,
            )
        except HarnessError:
            readiness["release_attested"] = False
            readiness["ready"] = False
        else:
            readiness["release_attested"] = True
        try:
            validate_persistent_memory_providers(
                prepared.manifest,
                conversation_store,
                long_term_memory_store,
            )
        except ConfigurationError:
            readiness["persistent_memory"] = False
            readiness["ready"] = False
        else:
            readiness["persistent_memory"] = True
        return readiness

    def resolved_manifest(
        self,
        settings: HarnessSettings,
        *,
        provider_adapter: ProviderContractAdapter | None = None,
        trusted_tenant_id: str | None = None,
        trusted_project_id: str | None = None,
    ) -> AgentManifest:
        return self.prepare(
            settings,
            provider_adapter=provider_adapter,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
        ).manifest

    def prepare(
        self,
        settings: HarnessSettings,
        *,
        provider_adapter: ProviderContractAdapter | None = None,
        handler_resolver: CapabilityHandlerResolver | None = None,
        trusted_tenant_id: str | None = None,
        trusted_project_id: str | None = None,
    ) -> PreparedAgent:
        self._validate_model_policy(settings)
        deployment_scope = self._resolve_deployment_scope(
            settings,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
        )
        prepared_manifest = (
            bind_deployment_scope(self.manifest, deployment_scope)
            if deployment_scope is not None
            else self.manifest
        )
        capabilities = capabilities_for_manifest(prepared_manifest, settings)
        if prepared_manifest.capability_bindings and provider_adapter is None:
            raise ConfigurationError(
                "Capability bindings require an attested provider adapter",
                context={"agent": prepared_manifest.id},
            )
        registrations = (
            tuple(
                runtime_attested_registration(
                    binding,
                    provider_adapter,
                    tenant_id=cast(DeploymentScope, deployment_scope).tenant_id,
                    project_id=cast(DeploymentScope, deployment_scope).project_id,
                    handler_resolver=handler_resolver,
                )
                for binding in prepared_manifest.capability_bindings
            )
            if provider_adapter is not None
            else ()
        )
        return PreparedAgent(
            manifest=prepared_manifest,
            capabilities=capabilities,
            registrations=registrations,
        )

    def prepare_hosted(
        self,
        settings: HarnessSettings,
        *,
        trusted_tenant_id: str | None = None,
        trusted_project_id: str | None = None,
    ) -> PreparedAgent:
        """Prepare the platform-managed Hosted Agent runtime without custom providers."""
        self._validate_model_policy(settings)
        deployment_scope = self._resolve_deployment_scope(
            settings,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
        )
        prepared_manifest = (
            bind_deployment_scope(self.manifest, deployment_scope)
            if deployment_scope is not None
            else self.manifest
        )
        if _requires_toolbox(prepared_manifest) and settings.toolbox_endpoint is None:
            raise ConfigurationError(
                "Manifest requires a configured Foundry Toolbox endpoint",
                context={"agent": self.manifest.id},
            )
        return PreparedAgent(
            manifest=prepared_manifest,
            capabilities=capabilities_for_manifest(prepared_manifest, settings),
            registrations=(),
        )

    def bootstrap_runtime(
        self,
        settings: HarnessSettings,
        *,
        provider_adapter: ProviderContractAdapter | None = None,
        handler_resolver: CapabilityHandlerResolver | None = None,
        trusted_tenant_id: str | None = None,
        trusted_project_id: str | None = None,
        idempotency_store: IdempotencyStore | None = None,
        approval_adapter: ApprovalConsumptionAdapter | None = None,
        release_attestor: ReleaseAttestor | None = None,
        conversation_store: ConversationStore | None = None,
        long_term_memory_store: LongTermMemoryStore | None = None,
        allow_test_idempotency_store: bool = False,
        allow_test_approval_adapter: bool = False,
        allow_test_release_attestor: bool = False,
    ) -> RuntimeBootstrap:
        if _requires_toolbox(self.manifest) and settings.toolbox_endpoint is None:
            raise ConfigurationError(
                "Manifest requires a configured Foundry Toolbox endpoint",
                context={"agent": self.manifest.id},
            )
        prepared = self.prepare(
            settings,
            provider_adapter=provider_adapter,
            handler_resolver=handler_resolver,
            trusted_tenant_id=trusted_tenant_id,
            trusted_project_id=trusted_project_id,
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
        if _requires_durable_approval(prepared.capabilities) and (
            approval_adapter is None
            or (
                not getattr(approval_adapter, "is_durable", False)
                and not allow_test_approval_adapter
            )
        ):
            raise ConfigurationError(
                "Approval-gated Hosted Agents require an app-owned durable approval adapter",
                context={"agent": self.manifest.id},
            )
        validate_persistent_memory_providers(
            prepared.manifest,
            conversation_store,
            long_term_memory_store,
        )
        release = build_release_metadata(
            prepared.manifest,
            model_deployment=settings.model_deployment_name,
            source_tree_digest=settings.source_tree_digest,
            registrations=prepared.registrations,
        )
        validate_release_attestation(
            release,
            prepared.manifest,
            release_attestor,
            allow_test_attestor=allow_test_release_attestor,
        )
        return RuntimeBootstrap(prepared=prepared, release=release)

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

    def _resolve_deployment_scope(
        self,
        settings: HarnessSettings,
        *,
        trusted_tenant_id: str | None,
        trusted_project_id: str | None,
    ) -> DeploymentScope | None:
        if (trusted_tenant_id is None) != (trusted_project_id is None):
            raise ConfigurationError(
                "Trusted deployment tenant and project assertions must be supplied together",
                context={"agent": self.manifest.id},
            )
        deployment_scope = settings.deployment_scope
        if trusted_tenant_id is not None and (
            deployment_scope is None
            or trusted_tenant_id != deployment_scope.tenant_id
            or trusted_project_id != deployment_scope.project_id
        ):
            raise ConfigurationError(
                "Trusted deployment scope assertion does not match HarnessSettings",
                context={"agent": self.manifest.id},
            )
        if self.manifest.capability_bindings and deployment_scope is None:
            raise ConfigurationError(
                "Capability-bearing Hosted Agents require a trusted deployment tenant and project scope",
                context={"agent": self.manifest.id},
            )
        return deployment_scope


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


def _requires_durable_approval(
    capabilities: tuple[CapabilityDescriptor, ...],
) -> bool:
    return any(capability.approval != ApprovalMode.NEVER for capability in capabilities)


def validate_persistent_memory_providers(
    manifest: AgentManifest,
    conversation_store: ConversationStore | None,
    long_term_memory_store: LongTermMemoryStore | None,
) -> None:
    conversation = manifest.memory.for_scope(MemoryScope.CONVERSATION)
    if conversation.persistent and (
        conversation_store is None
        or not getattr(conversation_store, "is_durable", False)
    ):
        raise ConfigurationError(
            "Persistent conversation memory requires an app-owned durable provider",
            context={"agent": manifest.id},
        )
    persistent_long_term = any(
        manifest.memory.for_scope(scope).persistent
        for scope in (
            MemoryScope.USER,
            MemoryScope.PROJECT,
            MemoryScope.PRIVATE_AGENT,
        )
    )
    if persistent_long_term and (
        long_term_memory_store is None
        or not getattr(long_term_memory_store, "is_durable", False)
    ):
        raise ConfigurationError(
            "Persistent long-term memory requires an app-owned durable provider",
            context={"agent": manifest.id},
        )
