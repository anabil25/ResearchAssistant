from __future__ import annotations

import atexit
import logging
from typing import Any

from research_assistant_core.telemetry import (
    CloseableCredential,
    TelemetryController,
    TelemetryMode,
    TelemetryRuntime,
)

_controller = TelemetryController()


def _managed_identity_credential(client_id: str | None) -> CloseableCredential:
    from azure.identity import ManagedIdentityCredential

    return ManagedIdentityCredential(client_id=client_id)


def _configure_azure_monitor(**kwargs: Any) -> None:
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(**kwargs)


def configure_telemetry(
    service_name: str,
    *,
    environment: str | None = None,
) -> TelemetryMode:
    mode = _controller.configure(
        service_name,
        environment=environment,
        azure_monitor_configurer=_configure_azure_monitor,
        credential_factory=_managed_identity_credential,
    )
    logging.getLogger("research_assistant").setLevel(logging.INFO)
    return mode


def telemetry_runtime() -> TelemetryRuntime | None:
    return _controller.runtime


def shutdown_telemetry() -> None:
    _controller.shutdown()


atexit.register(shutdown_telemetry)
