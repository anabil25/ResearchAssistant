from agent_framework import WorkflowAgent
from agent_framework_foundry_hosting import ResponsesHostServer  # type: ignore[import-untyped]
from dotenv import load_dotenv
from shared.catalog import capabilities_for_manifest
from shared.factory import GovernedAgentFactory
from shared.middleware import middleware_for_manifest
from shared.profiles import get_manifest
from shared.settings import HarnessSettings
from shared.workflows import FoundrySpecialistInvoker, SpecialistInvoker, build_coordinator_workflow

MANIFEST = get_manifest("coordinator")
FACTORY = GovernedAgentFactory(MANIFEST)


def build_agent(
    *,
    settings: HarnessSettings | None = None,
    invoker: SpecialistInvoker | None = None,
) -> WorkflowAgent:
    load_dotenv(override=False)
    effective_settings = settings or HarnessSettings.from_environment()
    effective_invoker = invoker or FoundrySpecialistInvoker(effective_settings)
    resolved_manifest = FACTORY.resolved_manifest(effective_settings)
    capabilities = capabilities_for_manifest(resolved_manifest, effective_settings)
    return WorkflowAgent(
        build_coordinator_workflow(effective_invoker),
        name=resolved_manifest.name,
        description=resolved_manifest.description,
        middleware=middleware_for_manifest(
            resolved_manifest,
            effective_settings,
            capabilities,
        ),
    )


def run() -> None:
    ResponsesHostServer(build_agent()).run()


__all__ = ["FACTORY", "MANIFEST", "build_agent", "build_coordinator_workflow", "run"]
