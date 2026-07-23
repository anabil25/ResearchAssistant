from shared.factory import GovernedAgentFactory
from shared.profiles import get_manifest
from shared.runtime import run_profile

MANIFEST = get_manifest("literature_online")
FACTORY = GovernedAgentFactory(MANIFEST)
build_agent = FACTORY.build


def run() -> None:
    run_profile("literature_online")
