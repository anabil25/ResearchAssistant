from __future__ import annotations

from typing import Any

from agent_framework import Agent
from agent_framework_foundry_hosting import ResponsesHostServer  # type: ignore[import-untyped]

from .contracts import AgentManifest
from .factory import get_factory
from .profiles import get_manifest
from .settings import HarnessSettings


def build_agent(
    profile_id: str,
    *,
    client: Any | None = None,
    settings: HarnessSettings | None = None,
) -> Agent:
    return get_factory(profile_id).build(client=client, settings=settings)


def run_profile(profile_id: str) -> None:
    ResponsesHostServer(build_agent(profile_id)).run()


def describe_profile(profile_id: str) -> AgentManifest:
    return get_manifest(profile_id)
