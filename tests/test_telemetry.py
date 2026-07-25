from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import research_assistant_api.telemetry as api_telemetry
import research_assistant_worker.telemetry as worker_telemetry
from research_assistant_api.telemetry import configure_telemetry as configure_api
from research_assistant_worker.telemetry import (
    configure_telemetry as configure_worker,
)


def test_telemetry_is_explicitly_disabled_without_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    monkeypatch.setattr(api_telemetry, "_configured", False)
    monkeypatch.setattr(worker_telemetry, "_configured", False)

    assert configure_api("api-test") is False
    assert configure_worker("worker-test") is False


@pytest.mark.parametrize(
    ("module", "configure", "service_name"),
    [
        (api_telemetry, configure_api, "api-test"),
        (worker_telemetry, configure_worker, "worker-test"),
    ],
)
def test_telemetry_configures_azure_monitor_when_connection_string_present(
    module: Any,
    configure: Any,
    service_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=fake")
    monkeypatch.delenv("AZURE_CLIENT_ID", raising=False)
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setattr(module, "_configured", False)
    configure_mock = MagicMock()
    monkeypatch.setattr(module, "configure_azure_monitor", configure_mock)
    default_credential_mock = MagicMock(name="DefaultAzureCredential-instance")
    monkeypatch.setattr(
        module,
        "DefaultAzureCredential",
        MagicMock(return_value=default_credential_mock),
    )
    managed_identity_mock = MagicMock(name="ManagedIdentityCredential-cls")
    monkeypatch.setattr(module, "ManagedIdentityCredential", managed_identity_mock)

    result = configure(service_name)

    assert result is True
    assert module._configured is True
    configure_mock.assert_called_once()
    _, kwargs = configure_mock.call_args
    assert kwargs["credential"] is default_credential_mock
    assert kwargs["logger_name"] == "research_assistant"
    assert kwargs["enable_live_metrics"] is False
    managed_identity_mock.assert_not_called()
    import os

    assert os.environ["OTEL_SERVICE_NAME"] == service_name


@pytest.mark.parametrize(
    ("module", "configure"),
    [
        (api_telemetry, configure_api),
        (worker_telemetry, configure_worker),
    ],
)
def test_telemetry_uses_managed_identity_when_client_id_is_set(
    module: Any,
    configure: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "InstrumentationKey=fake")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-123")
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    monkeypatch.setattr(module, "_configured", False)
    monkeypatch.setattr(module, "configure_azure_monitor", MagicMock())
    managed_identity_instance = MagicMock(name="ManagedIdentityCredential-instance")
    managed_identity_mock = MagicMock(return_value=managed_identity_instance)
    monkeypatch.setattr(module, "ManagedIdentityCredential", managed_identity_mock)
    monkeypatch.setattr(module, "DefaultAzureCredential", MagicMock())

    result = configure("managed-identity-test")

    assert result is True
    managed_identity_mock.assert_called_once_with(client_id="client-123")


@pytest.mark.parametrize(
    ("module", "configure"),
    [
        (api_telemetry, configure_api),
        (worker_telemetry, configure_worker),
    ],
)
def test_telemetry_short_circuits_when_already_configured(
    module: Any,
    configure: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_configured", True)
    configure_mock = MagicMock()
    monkeypatch.setattr(module, "configure_azure_monitor", configure_mock)

    result = configure("already-configured-test")

    assert result is True
    configure_mock.assert_not_called()
