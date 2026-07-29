from agent_framework import WorkflowAgent
from agent_framework_foundry_hosting import ResponsesHostServer  # type: ignore[import-untyped]
from dotenv import load_dotenv
from shared.factory import GovernedAgentFactory
from shared.middleware import middleware_for_manifest
from shared.profiles import get_manifest
from shared.settings import HarnessSettings
from shared.state import ConversationStore
from shared.workflows import (
    FoundrySpecialistInvoker,
    SpecialistInvoker,
    build_coordinator_workflow,
)

MANIFEST = get_manifest("coordinator")
FACTORY = GovernedAgentFactory(MANIFEST)


def build_agent(
    *,
    settings: HarnessSettings | None = None,
    invoker: SpecialistInvoker | None = None,
    trusted_tenant_id: str | None = None,
    trusted_project_id: str | None = None,
    conversation_store: ConversationStore | None = None,
) -> WorkflowAgent:
    load_dotenv(override=False)
    effective_settings = settings or HarnessSettings.from_environment()
    effective_invoker = invoker or FoundrySpecialistInvoker(effective_settings)
    prepared = FACTORY.prepare_hosted(
        effective_settings,
        trusted_tenant_id=trusted_tenant_id,
        trusted_project_id=trusted_project_id,
    )
    deployment_scope = prepared.manifest.deployment_scope
    return WorkflowAgent(
        build_coordinator_workflow(
            invoker=effective_invoker,
        ),
        name=prepared.manifest.name,
        description=prepared.manifest.description,
        middleware=middleware_for_manifest(
            prepared.manifest,
            effective_settings,
            prepared.capabilities,
            prepared.registrations,
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


def run() -> None:
    ResponsesHostServer(build_agent(), configure_observability=None).run()


__all__ = ["FACTORY", "MANIFEST", "build_agent", "build_coordinator_workflow", "run"]
