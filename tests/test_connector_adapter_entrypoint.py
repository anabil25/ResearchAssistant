from __future__ import annotations

import uvicorn
from _pytest.monkeypatch import MonkeyPatch
from research_assistant_connector_adapter import main


def test_connector_adapter_entrypoint_uses_bounded_network_defaults(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int]] = []

    def fake_run(app: str, *, host: str, port: int) -> None:
        calls.append((app, host, port))

    monkeypatch.setattr(uvicorn, "run", fake_run)
    main()

    assert calls == [
        ("research_assistant_connector_adapter.app:app", "0.0.0.0", 8200)
    ]
