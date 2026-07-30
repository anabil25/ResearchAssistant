"""The agent zip must stay self-sufficient.

Hosted agents are deployed as `agents/` alone with `agents/requirements.txt`;
the workspace-only `research_assistant_core` package is never installed in that
container. Importing it at agent startup makes every agent crash before
`/readiness`, which surfaces only as an opaque `424 session_not_ready` at
invoke time. The connector catalog is therefore vendored, and these tests fail
loudly if that vendoring regresses or drifts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
VENDORED = ROOT / "agents" / "shared" / "connector_catalog.py"
CANONICAL = (
    ROOT / "packages" / "research_core" / "src" / "research_assistant_core" / "connector_catalog.py"
)

AGENT_RUNTIME_FILES = sorted((ROOT / "agents").rglob("*.py"))
WORKSPACE_ONLY_PACKAGES = (
    "research_assistant_core",
    "research_assistant_connectors",
    "research_assistant_worker",
)


def test_vendored_connector_catalog_matches_the_canonical_module() -> None:
    assert VENDORED.exists(), "agents/shared/connector_catalog.py must be vendored"
    assert VENDORED.read_text(encoding="utf-8") == CANONICAL.read_text(encoding="utf-8"), (
        "The vendored agent connector catalog has drifted from the canonical core module. "
        "Re-copy packages/research_core/src/research_assistant_core/connector_catalog.py "
        "into agents/shared/connector_catalog.py."
    )


@pytest.mark.parametrize("package", WORKSPACE_ONLY_PACKAGES)
def test_agent_runtime_never_imports_a_workspace_only_package(package: str) -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in AGENT_RUNTIME_FILES
        if ".venv" not in path.parts
        and "tests" not in path.parts
        and f"import {package}" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        f"{package} is not installed in the hosted-agent container, so importing it "
        f"crashes the agent before /readiness. Offending files: {offenders}"
    )
