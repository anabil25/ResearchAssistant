"""Test-session environment policy.

The suite exercises endpoints without supplying a gateway principal, which
resolves to the local developer identity because ``entra_auth_enforced``
defaults to ``False`` and the default ``environment`` is local. No
environment opt-in is required.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from research_assistant_api.config import Settings

ROOT = Path(__file__).parents[1]


@pytest.fixture(scope="session")
def azure_manifest() -> dict[str, Any]:
    manifest = cast(
        dict[str, Any],
        yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8")),
    )
    manifest["services"] = {
        name: {
            **(
                yaml.safe_load((ROOT / service["$ref"]).read_text(encoding="utf-8"))
                if "$ref" in service
                else {}
            ),
            **service,
        }
        for name, service in manifest["services"].items()
    }
    return manifest


def pytest_configure(config: pytest.Config) -> None:
    """Stop a developer's root ``.env`` from reaching ``Settings()``.

    ``Settings`` reads ``.env`` so a local run can point at a real deployment.
    Tests construct ``Settings()`` directly to assert unconfigured behaviour,
    so inheriting that file makes results depend on whether the machine has
    ever been pointed at Azure.
    """
    Settings.model_config["env_file"] = None


@pytest.fixture(autouse=True)
def deny_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guarded_connect(
        instance: socket.socket,
        address: tuple[str, int] | tuple[str, int, int, int] | str,
    ) -> None:
        if isinstance(address, tuple):
            host = address[0]
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise AssertionError(
                    f"External network access is forbidden in tests: {host}"
                )
        connect(instance, address)

    def guarded_connect_ex(
        instance: socket.socket,
        address: tuple[str, int] | tuple[str, int, int, int] | str,
    ) -> int:
        if isinstance(address, tuple):
            host = address[0]
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise AssertionError(
                    f"External network access is forbidden in tests: {host}"
                )
        return connect_ex(instance, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture(scope="session", autouse=True)
def shutdown_local_telemetry() -> Iterator[None]:
    yield
    from research_assistant_api.telemetry import shutdown_telemetry as shutdown_api
    from research_assistant_worker.telemetry import (
        shutdown_telemetry as shutdown_worker,
    )

    shutdown_worker()
    shutdown_api()
