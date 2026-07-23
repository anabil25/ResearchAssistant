from __future__ import annotations

import os
from typing import Any

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer  # type: ignore[import-untyped]
from dotenv import load_dotenv

from shared.credentials import get_credential
from shared.profiles import AgentProfile, get_profile
from shared.tools import tools_for_profile


def build_agent(profile_id: str, *, client: Any | None = None) -> Agent:
    load_dotenv(override=False)
    profile = get_profile(profile_id)
    chat_client = client or _build_foundry_client()
    return Agent(
        client=chat_client,
        name=profile.name,
        instructions=profile.instructions,
        tools=tools_for_profile(profile, chat_client),
        default_options={"store": False},
    )


def _build_foundry_client() -> FoundryChatClient:
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    return FoundryChatClient(
        project_endpoint=endpoint,
        model=model,
        credential=get_credential(),
    )


def run_profile(profile_id: str) -> None:
    agent = build_agent(profile_id)
    ResponsesHostServer(agent).run()


def describe_profile(profile_id: str) -> AgentProfile:
    return get_profile(profile_id)
