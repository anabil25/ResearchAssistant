"""The agent-side half of chat attachments."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from shared.errors import ConfigurationError
from shared.profiles import get_manifest
from shared.settings import HarnessSettings
from shared.tools import tools_for_profile

CHAT_PROFILES = ("literature", "grant", "matching", "dataset")


def settings() -> HarnessSettings:
    return HarnessSettings(
        foundry_project_endpoint="https://project.example",
        model_deployment_name="gpt-5.4-mini",
        source_tree_digest="0" * 64,
        managed_identity_client_id="agent-client-id",
        toolbox_endpoint="https://project.example/toolboxes/research-shared/mcp?api-version=v1",
        default_timeout_seconds=37,
    )


@pytest.mark.parametrize("profile_id", CHAT_PROFILES)
def test_chat_studios_fail_closed_without_the_toolbox_endpoint(profile_id: str) -> None:
    with pytest.raises(ConfigurationError, match="Toolbox endpoint"):
        tools_for_profile(get_manifest(profile_id))


@pytest.mark.parametrize("profile_id", CHAT_PROFILES)
def test_chat_studios_use_only_the_shared_code_interpreter(
    profile_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def build_toolbox(*args: object, **kwargs: object) -> SimpleNamespace:
        calls.append((args, kwargs))
        return SimpleNamespace(allowed_tools=None)

    credential = object()
    monkeypatch.setattr("shared.tools.get_credential", lambda _client_id: credential)
    monkeypatch.setattr("shared.tools.FoundryToolbox", build_toolbox)

    configured = settings()
    toolbox = tools_for_profile(get_manifest(profile_id), settings=configured)

    assert calls == [
        (
            (credential,),
            {
                "url": str(configured.toolbox_endpoint),
                "timeout": configured.default_timeout_seconds,
            },
        )
    ]
    assert toolbox.allowed_tools == frozenset({"code_interpreter"})


@pytest.mark.parametrize("profile_id", ("institution", "coordinator"))
def test_agents_without_a_chat_surface_get_no_file_toolbox(profile_id: str) -> None:
    assert tools_for_profile(get_manifest(profile_id)) == []


def test_every_agent_is_told_attachments_are_untrusted() -> None:
    for profile_id in CHAT_PROFILES:
        instructions = get_manifest(profile_id).instructions
        assert "session home directory" in instructions
        assert "untrusted data" in instructions
