from __future__ import annotations

import logging
import os

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.monitor.opentelemetry import configure_azure_monitor

_configured = False


def configure_telemetry(service_name: str) -> bool:
    global _configured
    if _configured:
        return True
    if not os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return False

    os.environ.setdefault("OTEL_SERVICE_NAME", service_name)
    client_id = os.getenv("AZURE_CLIENT_ID")
    credential = ManagedIdentityCredential(client_id=client_id) if client_id else DefaultAzureCredential()
    configure_azure_monitor(
        credential=credential,
        logger_name="research_assistant",
        enable_live_metrics=False,
    )
    logging.getLogger("research_assistant").setLevel(logging.INFO)
    _configured = True
    return True
