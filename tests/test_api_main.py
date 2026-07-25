from __future__ import annotations

import runpy
from typing import Any

import research_assistant_api
import uvicorn


def test_package_main_runs_uvicorn(monkeypatch: Any) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(uvicorn, "run", fake_run)

    research_assistant_api.main()

    assert calls == [
        (
            ("research_assistant_api.app:app",),
            {"host": "0.0.0.0", "port": 8000, "reload": False},
        )
    ]


def test_main_module_invokes_package_main(monkeypatch: Any) -> None:
    calls: list[str] = []

    monkeypatch.setattr(research_assistant_api, "main", lambda: calls.append("called"))

    runpy.run_module("research_assistant_api.__main__", run_name="__main__")

    assert calls == ["called"]
