"""Test-session environment policy.

``Settings.allow_demo_identity`` defaults to ``False`` in application code
(the unauthenticated "demo sandbox" identity bypass must be an explicit,
impossible-to-misconfigure-into-production local/dev/test adapter, never a
silent default -- see the review finding this fixes). Most of this test
suite exercises endpoints without supplying an authenticated principal and
therefore *does* need the demo identity enabled; that opt-in must happen
here, as an explicit environment-variable declaration for the test session,
rather than by relying on the library default. This module is collected
before any test module in this directory imports
``research_assistant_api.app`` (whose module-level ``app.state.settings``
is resolved once via the process-wide ``get_settings()`` cache), so setting
the environment variable here reliably takes effect for that first import.

``os.environ.setdefault`` is used (not ``setenv``) so a test that needs to
exercise the "demo identity disabled" behavior can still monkeypatch this
variable to ``"false"`` for its own scope without being overridden back.
"""

from __future__ import annotations

import os
import ipaddress
import socket
from collections.abc import Iterator
import pytest

os.environ.setdefault("RESEARCH_ALLOW_DEMO_IDENTITY", "true")


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
