from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Iterator

import pytest

os.environ["RESEARCH_TELEMETRY_MODE"] = "local"
os.environ["ENABLE_CONSOLE_EXPORTERS"] = "false"
for _name in tuple(os.environ):
    if _name.startswith("OTEL_EXPORTER_"):
        del os.environ[_name]


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
