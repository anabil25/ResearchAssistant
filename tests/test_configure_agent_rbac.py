from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from scripts import configure_agent_rbac


def test_agent_environment_values_are_version_specific() -> None:
    values = configure_agent_rbac.agent_environment_values(
        "matching-agent",
        "7",
        "https://example.services.ai.azure.com/api/projects/research",
    )

    assert values["AGENT_MATCHING_AGENT_VERSION"] == "7"
    assert values["AGENT_MATCHING_AGENT_ENDPOINT"].endswith("/agents/matching-agent/versions/7")
    assert values["AGENT_MATCHING_AGENT_RESPONSES_ENDPOINT"].endswith(
        "/agents/matching-agent/endpoint/protocols/openai/responses?api-version=v1"
    )


def test_wait_for_role_assignment_handles_propagation(monkeypatch: pytest.MonkeyPatch) -> None:
    responses: Iterator[list[str]] = iter(
        [[], [f"/providers/Microsoft.Authorization/roleDefinitions/{configure_agent_rbac.FOUNDRY_USER_ROLE_ID}"]]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(configure_agent_rbac, "run_json", lambda _command: next(responses))
    monkeypatch.setattr("scripts.configure_agent_rbac.time.sleep", sleeps.append)

    configure_agent_rbac.wait_for_role_assignment(
        "principal",
        "/project",
        attempts=2,
        delay_seconds=0.25,
    )

    assert sleeps == [0.25]


def test_wait_for_role_assignment_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(configure_agent_rbac, "run_json", lambda _command: [])
    monkeypatch.setattr("scripts.configure_agent_rbac.time.sleep", lambda _delay: None)

    with pytest.raises(RuntimeError, match="was not visible"):
        configure_agent_rbac.wait_for_role_assignment(
            "principal",
            "/project",
            attempts=2,
            delay_seconds=0,
        )


def test_sync_agent_environment_outputs_rejects_missing_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    latest = SimpleNamespace(version="3", status="active")
    only_one = SimpleNamespace(name="matching-agent", versions=SimpleNamespace(latest=latest))
    client = SimpleNamespace(agents=SimpleNamespace(list=lambda: [only_one]))
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test/projects/research")
    monkeypatch.setattr(configure_agent_rbac, "AIProjectClient", lambda **_kwargs: client)
    monkeypatch.setattr(
        "scripts.configure_agent_rbac.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=""),
    )

    with pytest.raises(RuntimeError, match="deployments are missing"):
        configure_agent_rbac.sync_agent_environment_outputs()


@pytest.mark.parametrize("status", ["creating", "failed"])
def test_sync_agent_environment_outputs_rejects_non_active_latest_version(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    latest = SimpleNamespace(version="4", status=status)
    agent = SimpleNamespace(name="matching-agent", versions=SimpleNamespace(latest=latest))
    client = SimpleNamespace(agents=SimpleNamespace(list=lambda: [agent]))
    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test/projects/research")
    monkeypatch.setattr(configure_agent_rbac, "AIProjectClient", lambda **_kwargs: client)

    with pytest.raises(RuntimeError, match=f"latest version is {status}"):
        configure_agent_rbac.sync_agent_environment_outputs()


def test_sync_agent_environment_outputs_rewrites_every_active_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents = [
        SimpleNamespace(
            name=name,
            versions=SimpleNamespace(latest=SimpleNamespace(version="9", status="active")),
        )
        for name in configure_agent_rbac.AGENT_NAMES
    ]
    client = SimpleNamespace(agents=SimpleNamespace(list=lambda: agents))
    commands: list[list[str]] = []

    def completed(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(stdout="")

    monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://example.test/projects/research")
    monkeypatch.setattr(configure_agent_rbac, "AIProjectClient", lambda **_kwargs: client)
    monkeypatch.setattr("scripts.configure_agent_rbac.subprocess.run", completed)

    configure_agent_rbac.sync_agent_environment_outputs()

    version_updates = [
        command
        for command in commands
        if command[:3] == ["azd", "env", "set"] and command[3].endswith("_VERSION")
    ]
    assert len(version_updates) == len(configure_agent_rbac.AGENT_NAMES)
    assert all(command[4] == "9" for command in version_updates)


def test_agent_instance_principal_id_rejects_incomplete_payload() -> None:
    with pytest.raises(RuntimeError, match="no instance identity"):
        configure_agent_rbac.agent_instance_principal_id({"name": "agent"})