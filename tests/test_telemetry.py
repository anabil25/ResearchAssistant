from __future__ import annotations

import pytest
from research_assistant_api.telemetry import configure_telemetry as configure_api
from research_assistant_worker.telemetry import (
    configure_telemetry as configure_worker,
)


def test_telemetry_is_explicitly_disabled_without_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)

    assert configure_api("api-test") is False
    assert configure_worker("worker-test") is False
