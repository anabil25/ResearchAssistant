from __future__ import annotations

import socket
import sys
from types import ModuleType

import pytest
from opentelemetry import trace
from research_assistant_api import telemetry as api_telemetry
from research_assistant_core import telemetry as core_telemetry
from research_assistant_core.telemetry import (
    CloseableCredential,
    TelemetryController,
    TelemetryMode,
    TelemetryRuntime,
    global_provider_snapshot,
    resolve_telemetry_mode,
    validate_provider_ownership,
)
from research_assistant_worker import telemetry as worker_telemetry


def test_local_telemetry_never_constructs_cloud_credential_or_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        calls.append("external")
        raise AssertionError("local telemetry attempted cloud configuration")

    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )
    monkeypatch.setattr(worker_telemetry, "_configure_azure_monitor", forbidden)
    monkeypatch.setattr(worker_telemetry, "_managed_identity_credential", forbidden)

    assert (
        worker_telemetry.configure_telemetry(
            "worker-test",
            environment="development",
        )
        == TelemetryMode.LOCAL
    )
    runtime = worker_telemetry.telemetry_runtime()
    assert runtime is not None
    assert runtime.span_exporter is not None
    runtime.span_exporter.clear()
    with trace.get_tracer("telemetry-test").start_as_current_span("local-span"):
        pass
    assert [span.name for span in runtime.span_exporter.get_finished_spans()] == [
        "local-span"
    ]
    assert calls == []


def test_azure_monitor_uses_managed_identity_and_explicit_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Provider:
        def __init__(self, name: str) -> None:
            self.name = name

        def shutdown(self) -> None:
            events.append(f"shutdown:{self.name}")

    class Credential:
        def close(self) -> None:
            events.append("credential:close")

    controller = TelemetryController(
        provider_snapshot=lambda: (
            Provider("trace"),
            Provider("metrics"),
            Provider("logs"),
        )
    )

    def credential_factory(*, client_id: str | None) -> Credential:
        events.append(f"credential:{client_id}")
        return Credential()

    monkeypatch.setattr(api_telemetry, "_controller", controller)
    monkeypatch.setenv("RESEARCH_TELEMETRY_MODE", "azure-monitor")
    monkeypatch.setenv(
        "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "InstrumentationKey=00000000-0000-0000-0000-000000000000",
    )
    monkeypatch.setenv("AZURE_CLIENT_ID", "managed-identity-client")
    monkeypatch.setattr(
        api_telemetry,
        "_managed_identity_credential",
        lambda client_id: credential_factory(client_id=client_id),
    )
    monkeypatch.setattr(
        api_telemetry,
        "_configure_azure_monitor",
        lambda **kwargs: events.append(
            f"configure:{kwargs['logger_name']}:{kwargs['enable_live_metrics']}"
        ),
    )

    assert (
        api_telemetry.configure_telemetry(
            "api-production",
            environment="production",
        )
        == TelemetryMode.AZURE_MONITOR
    )
    api_telemetry.shutdown_telemetry()
    assert events == [
        "credential:managed-identity-client",
        "configure:research_assistant:False",
        "shutdown:trace",
        "shutdown:metrics",
        "shutdown:logs",
        "credential:close",
    ]


def test_azure_monitor_configuration_errors_close_credentials() -> None:
    events: list[str] = []

    class Credential:
        def close(self) -> None:
            events.append("credential:close")

    controller = TelemetryController(provider_snapshot=global_provider_snapshot)

    with pytest.raises(RuntimeError, match="configuration failed"):
        controller.configure(
            "api-production",
            environment="production",
            environ={
                "RESEARCH_TELEMETRY_MODE": "azure-monitor",
                "APPLICATIONINSIGHTS_CONNECTION_STRING": (
                    "InstrumentationKey=00000000-0000-0000-0000-000000000000"
                ),
            },
            credential_factory=lambda client_id: Credential(),
            azure_monitor_configurer=lambda **kwargs: (_ for _ in ()).throw(
                RuntimeError("configuration failed")
            ),
        )

    assert events == ["credential:close"]


def test_telemetry_modes_and_reconfiguration_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(**kwargs: object) -> None:
        del kwargs

    def credential(client_id: str | None) -> CloseableCredential:
        del client_id
        pytest.fail("credential should not be constructed")

    assert (
        resolve_telemetry_mode(
            environment="development",
            environ={},
        )
        == TelemetryMode.LOCAL
    )
    assert (
        resolve_telemetry_mode(
            environment="production",
            environ={"APPLICATIONINSIGHTS_CONNECTION_STRING": "configured"},
        )
        == TelemetryMode.AZURE_MONITOR
    )
    assert (
        resolve_telemetry_mode(environment="production", environ={})
        == TelemetryMode.DISABLED
    )
    with pytest.raises(ValueError, match="RESEARCH_TELEMETRY_MODE"):
        resolve_telemetry_mode(
            environment="test",
            environ={"RESEARCH_TELEMETRY_MODE": "unexpected"},
        )

    disabled = TelemetryController()
    assert (
        disabled.configure(
            "disabled-service",
            environment="production",
            environ={"RESEARCH_TELEMETRY_MODE": "disabled"},
            azure_monitor_configurer=unavailable,
            credential_factory=credential,
        )
        == TelemetryMode.DISABLED
    )
    assert disabled.runtime is not None
    assert (
        disabled.configure(
            "disabled-service",
            environment="production",
            environ={"RESEARCH_TELEMETRY_MODE": "disabled"},
            azure_monitor_configurer=unavailable,
            credential_factory=credential,
        )
        == TelemetryMode.DISABLED
    )
    with pytest.raises(RuntimeError, match="different runtime"):
        disabled.configure(
            "other-service",
            environment="production",
            environ={"RESEARCH_TELEMETRY_MODE": "disabled"},
            azure_monitor_configurer=unavailable,
            credential_factory=credential,
        )
    disabled.shutdown()
    disabled.shutdown()
    with pytest.raises(RuntimeError, match="different runtime"):
        disabled.configure(
            "disabled-service",
            environment="production",
            environ={"RESEARCH_TELEMETRY_MODE": "disabled"},
            azure_monitor_configurer=unavailable,
            credential_factory=credential,
        )

    missing_connection = TelemetryController()
    with pytest.raises(RuntimeError, match="APPLICATIONINSIGHTS_CONNECTION_STRING"):
        missing_connection.configure(
            "azure-service",
            environment="production",
            environ={"RESEARCH_TELEMETRY_MODE": "azure-monitor"},
            azure_monitor_configurer=unavailable,
            credential_factory=credential,
        )
    TelemetryController().shutdown()

    closed_local = TelemetryRuntime(
        mode=TelemetryMode.LOCAL,
        service_name="closed-local",
    )
    closed_local.shutdown()
    monkeypatch.setattr(core_telemetry, "_LOCAL_RUNTIME", closed_local)
    with pytest.raises(RuntimeError, match="already been shut down"):
        core_telemetry.configure_local_telemetry("closed-local")


def test_runtime_cleanup_and_provider_ownership() -> None:
    events: list[str] = []

    class Provider:
        def shutdown(self) -> None:
            events.append("provider")

    runtime = TelemetryRuntime(
        mode=TelemetryMode.AZURE_MONITOR,
        service_name="cleanup-test",
        providers=(Provider(), object()),
        cleanup=lambda: events.append("cleanup"),
    )
    runtime.shutdown()
    runtime.shutdown()
    assert events == ["provider", "cleanup"]

    providers = (object(), object(), object())
    validate_provider_ownership(providers, providers)
    with pytest.raises(RuntimeError, match="unconfigured OpenTelemetry providers"):
        validate_provider_ownership(providers, ())


def test_service_cloud_adapters_are_lazy_and_injectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Credential:
        def __init__(self, *, client_id: str | None) -> None:
            events.append(f"credential:{client_id}")

        def close(self) -> None:
            events.append("credential:close")

    identity = ModuleType("azure.identity")
    identity.ManagedIdentityCredential = Credential  # type: ignore[attr-defined]
    monitor = ModuleType("azure.monitor.opentelemetry")
    monitor.configure_azure_monitor = (  # type: ignore[attr-defined]
        lambda **kwargs: events.append(f"configure:{kwargs['service']}")
    )
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    monkeypatch.setitem(sys.modules, "azure.monitor.opentelemetry", monitor)

    api_credential = api_telemetry._managed_identity_credential("api-client")
    worker_credential = worker_telemetry._managed_identity_credential("worker-client")
    api_telemetry._configure_azure_monitor(service="api")
    worker_telemetry._configure_azure_monitor(service="worker")
    api_credential.close()
    worker_credential.close()
    assert api_telemetry.telemetry_runtime() is not None
    assert events == [
        "credential:api-client",
        "credential:worker-client",
        "configure:api",
        "configure:worker",
        "credential:close",
        "credential:close",
    ]

    with (
        socket.socket() as external_socket,
        pytest.raises(
            AssertionError,
            match="External network access is forbidden in tests",
        ),
    ):
        external_socket.connect_ex(("192.0.2.1", 443))
