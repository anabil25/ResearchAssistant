from __future__ import annotations

import logging
import runpy
from types import SimpleNamespace
from typing import Any

import pytest
import research_assistant_worker as worker_package
import research_assistant_worker.runtime as runtime
from research_assistant_worker.config import SchedulerSettings


def test_package_exports_runtime_main() -> None:
    assert worker_package.main is runtime.main
    assert worker_package.__all__ == ["main"]


def test_runtime_credential_prefers_managed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class FakeManagedIdentityCredential:
        def __init__(self, *, client_id: str) -> None:
            created.append(client_id)
            self.client_id = client_id

    monkeypatch.setattr(runtime, "ManagedIdentityCredential", FakeManagedIdentityCredential)
    monkeypatch.setattr(runtime, "DefaultAzureCredential", lambda: None)

    credential = runtime._credential("client-123")

    assert credential.client_id == "client-123"  # type: ignore[attr-defined]
    assert created == ["client-123"]


def test_runtime_credential_uses_default_when_client_id_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []

    class FakeDefaultAzureCredential:
        def __init__(self) -> None:
            created.append("default")

    monkeypatch.setattr(
        runtime,
        "ManagedIdentityCredential",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(runtime, "DefaultAzureCredential", FakeDefaultAzureCredential)

    credential = runtime._credential(None)

    assert isinstance(credential, FakeDefaultAzureCredential)
    assert created == ["default"]


def test_build_worker_registers_orchestrator_and_activities(monkeypatch: pytest.MonkeyPatch) -> None:
    constructed: list[dict[str, Any]] = []

    class FakeWorker:
        def __init__(self, **kwargs: Any) -> None:
            constructed.append(kwargs)
            self.orchestrators: list[Any] = []
            self.activities: list[Any] = []

        def add_orchestrator(self, orchestrator: Any) -> None:
            self.orchestrators.append(orchestrator)

        def add_activity(self, activity: Any) -> None:
            self.activities.append(activity)

    credentials: list[str | None] = []
    monkeypatch.setattr(
        runtime,
        "DurableTaskSchedulerWorker",
        FakeWorker,
    )

    def fake_credential(client_id: str | None) -> str:
        credentials.append(client_id)
        return f"credential:{client_id}"

    monkeypatch.setattr(runtime, "_credential", fake_credential)

    secure_settings = SchedulerSettings(
        host_address="scheduler.example.test",
        task_hub="research",
        secure_channel=True,
        managed_identity_client_id="client-123",
    )
    local_settings = SchedulerSettings(
        host_address="localhost:8080",
        task_hub="default",
        secure_channel=False,
        managed_identity_client_id=None,
    )
    monkeypatch.setattr(runtime, "parse_scheduler_settings", lambda: secure_settings)

    secure_worker = runtime.build_worker()

    monkeypatch.setattr(runtime, "parse_scheduler_settings", lambda: local_settings)
    local_worker = runtime.build_worker()

    assert secure_worker.orchestrators == [runtime.research_pipeline]  # type: ignore[attr-defined]
    assert secure_worker.activities == [  # type: ignore[attr-defined]
        runtime.ingest_source,  # type: ignore[attr-defined]
        runtime.retrieve_evidence,  # type: ignore[attr-defined]
        runtime.synthesize_artifact,  # type: ignore[attr-defined]
        runtime.verify_artifact,  # type: ignore[attr-defined]
        runtime.complete_run,  # type: ignore[attr-defined]
        runtime.execute_workflow_step,  # type: ignore[attr-defined]
    ]
    assert constructed[0] == {
        "host_address": "scheduler.example.test",
        "taskhub": "research",
        "token_credential": "credential:client-123",
        "secure_channel": True,
    }
    assert constructed[1] == {
        "host_address": "localhost:8080",
        "taskhub": "default",
        "token_credential": None,
        "secure_channel": False,
    }
    assert local_worker.activities == secure_worker.activities  # type: ignore[attr-defined]
    assert credentials == ["client-123"]


def test_main_starts_and_stops_worker_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[str] = []
    worker = SimpleNamespace(
        start=lambda: calls.append("start"),
        stop=lambda: calls.append("stop"),
    )

    class InterruptingEvent:
        def wait(self) -> None:
            raise KeyboardInterrupt()

    monkeypatch.setattr(runtime, "configure_telemetry", lambda name: calls.append(f"telemetry:{name}"))
    monkeypatch.setattr(runtime, "build_worker", lambda: worker)
    monkeypatch.setattr(runtime, "Event", InterruptingEvent)
    monkeypatch.setattr(logging, "basicConfig", lambda **kwargs: calls.append(f"basicConfig:{kwargs['level']}"))

    with caplog.at_level(logging.INFO):
        runtime.main()

    assert calls == [
        "telemetry:research-assistant-worker",
        f"basicConfig:{logging.INFO}",
        "start",
        "stop",
    ]
    assert "Worker shutdown requested" in caplog.text


def test_module_entrypoint_calls_package_main(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(worker_package, "main", lambda: calls.append("called"))

    runpy.run_module("research_assistant_worker.__main__", run_name="__main__")

    assert calls == ["called"]
