from __future__ import annotations

import json
import logging
import os
import time
from typing import Annotated, Any

from agent_framework import tool
from agent_framework_foundry_hosting import FoundryToolbox  # type: ignore[import-untyped]
from azure.ai.projects import AIProjectClient
from openai import APIStatusError
from pydantic import Field

from shared.credentials import get_credential
from shared.profiles import AgentProfile

logger = logging.getLogger(__name__)


def _agent_names() -> dict[str, str]:
    return {
        "literature": os.getenv("RESEARCH_LITERATURE_AGENT_NAME", "literature-agent"),
        "grant": os.getenv("RESEARCH_GRANT_AGENT_NAME", "grant-agent"),
        "matching": os.getenv("RESEARCH_MATCHING_AGENT_NAME", "matching-agent"),
        "dataset": os.getenv("RESEARCH_DATASET_AGENT_NAME", "dataset-agent"),
        "institutional_qa": os.getenv("RESEARCH_INSTITUTION_AGENT_NAME", "institution-agent"),
    }


def _online_agent_names() -> dict[str, str]:
    return {
        "literature": os.getenv(
            "RESEARCH_LITERATURE_ONLINE_AGENT_NAME",
            "literature-online-agent",
        ),
        "grant": os.getenv(
            "RESEARCH_GRANT_ONLINE_AGENT_NAME",
            "grant-online-agent",
        ),
        "matching": os.getenv(
            "RESEARCH_MATCHING_ONLINE_AGENT_NAME",
            "matching-online-agent",
        ),
    }


def delegated_agent_name(capability: str, sensitivity: str) -> str | None:
    if sensitivity == "public" and capability in _online_agent_names():
        return _online_agent_names()[capability]
    return _agent_names().get(capability)


def _invoke_specialist(client: Any, request: str, agent_name: str) -> str:
    session_retry_delays = (15, 30, 60)
    empty_output_retry_delays = (2, 5)
    session_retries = 0
    empty_output_retries = 0
    while True:
        try:
            response = client.responses.create(input=request)
        except APIStatusError as exc:
            error = exc.body.get("error", {}) if isinstance(exc.body, dict) else {}
            if (
                exc.status_code != 424
                or not isinstance(error, dict)
                or error.get("code") != "session_not_ready"
                or session_retries == len(session_retry_delays)
            ):
                raise
            time.sleep(session_retry_delays[session_retries])
            session_retries += 1
            continue
        raw_output = getattr(response, "output_text", None)
        output = raw_output.strip() if isinstance(raw_output, str) else ""
        if output:
            return output
        if empty_output_retries == len(empty_output_retry_delays):
            raise RuntimeError(
                f"Hosted specialist {agent_name} returned no output after bounded retries"
            )
        delay = empty_output_retry_delays[empty_output_retries]
        logger.warning(
            "Hosted specialist %s returned empty output; retrying in %s seconds.",
            agent_name,
            delay,
        )
        time.sleep(delay)
        empty_output_retries += 1


def build_delegate_tool() -> Any:
    @tool(approval_mode="never_require")
    def delegate_to_specialist(
        capability: Annotated[
            str,
            Field(description=("One of literature, grant, matching, dataset, institutional_qa")),
        ],
        request: Annotated[
            str,
            Field(description="Complete user request and any verified context"),
        ],
        sensitivity: Annotated[
            str,
            Field(description="One of public, internal, confidential, restricted"),
        ] = "internal",
    ) -> str:
        """Delegate a bounded read-only request to the matching Hosted Agent."""
        agent_name = delegated_agent_name(capability, sensitivity)
        if agent_name is None:
            return json.dumps(
                {
                    "error": "unsupported_capability",
                    "allowed": sorted(_agent_names()),
                }
            )
        if sensitivity not in {"public", "internal", "confidential", "restricted"}:
            return json.dumps(
                {
                    "error": "invalid_sensitivity",
                    "allowed": ["public", "internal", "confidential", "restricted"],
                }
            )
        endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
        project = AIProjectClient(
            endpoint=endpoint,
            credential=get_credential(),
            allow_preview=True,
        )
        client = project.get_openai_client(agent_name=agent_name)
        return _invoke_specialist(client, request, agent_name)

    return delegate_to_specialist


def tools_for_profile(profile: AgentProfile, client: Any | None = None) -> Any:
    if profile.id == "coordinator":
        return [build_delegate_tool()]
    if os.getenv("TOOLBOX_ENDPOINT"):
        return FoundryToolbox(get_credential())
    tools: list[Any] = []
    if profile.enable_web_search and client is not None:
        tools.append(
            client.get_web_search_tool(
                search_context_size="medium",
            )
        )
    return tools
