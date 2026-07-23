from agent_framework import WorkflowAgent
from agent_framework_foundry_hosting import ResponsesHostServer  # type: ignore[import-untyped]
from dotenv import load_dotenv
from shared.approvals import ApprovalConsumptionAdapter
from shared.capabilities import ProviderContractAdapter
from shared.factory import GovernedAgentFactory
from shared.idempotency import IdempotencyStore
from shared.middleware import middleware_for_manifest
from shared.profiles import get_manifest
from shared.release import build_release_metadata
from shared.settings import HarnessSettings
from shared.workflows import (
    FoundrySpecialistInvoker,
    SpecialistInvoker,
    build_coordinator_workflow,
    specialist_handler_resolver,
)

MANIFEST = get_manifest("coordinator")
FACTORY = GovernedAgentFactory(MANIFEST)


def build_agent(
    *,
    settings: HarnessSettings | None = None,
    invoker: SpecialistInvoker | None = None,
    provider_adapter: ProviderContractAdapter | None = None,
    idempotency_store: IdempotencyStore | None = None,
    approval_adapter: ApprovalConsumptionAdapter | None = None,
    allow_test_idempotency_store: bool = False,
    allow_test_approval_adapter: bool = False,
) -> WorkflowAgent:
    load_dotenv(override=False)
    effective_settings = settings or HarnessSettings.from_environment()
    effective_invoker = invoker or FoundrySpecialistInvoker(effective_settings)
    prepared = FACTORY.prepare(
        effective_settings,
        provider_adapter=provider_adapter,
        handler_resolver=specialist_handler_resolver(effective_invoker),
    )
    release = build_release_metadata(
        prepared.manifest,
        model_deployment=effective_settings.model_deployment_name,
        registrations=prepared.registrations,
    )
    return WorkflowAgent(
        build_coordinator_workflow(
            prepared.registrations[0],
            idempotency_store=idempotency_store,
            approval_adapter=approval_adapter,
            release_id=release.release_id,
            allow_test_idempotency_store=allow_test_idempotency_store,
            allow_test_approval_adapter=allow_test_approval_adapter,
        ),
        name=prepared.manifest.name,
        description=prepared.manifest.description,
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
        ),
    )


def run() -> None:
    ResponsesHostServer(build_agent()).run()


__all__ = ["FACTORY", "MANIFEST", "build_agent", "build_coordinator_workflow", "run"]
