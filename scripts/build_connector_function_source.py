"""Stage local workspace packages into the Azure Functions project."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTION_ROOT = ROOT / "services" / "connector_functions"
PACKAGE_SOURCES = {
    "research_assistant_core": ROOT
    / "packages"
    / "research_core"
    / "src"
    / "research_assistant_core",
    "research_assistant_connectors": ROOT
    / "packages"
    / "research_connectors"
    / "src"
    / "research_assistant_connectors",
}


def _ignore_generated(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name == "__pycache__" or name.endswith((".pyc", ".pyo"))}


def build_function_source(
    destination: Path = FUNCTION_ROOT,
    package_sources: dict[str, Path] = PACKAGE_SOURCES,
) -> tuple[Path, ...]:
    staged: list[Path] = []
    for package_name, source in package_sources.items():
        if not (source / "__init__.py").is_file():
            raise FileNotFoundError(f"Function package source is missing: {source}")
        target = destination / package_name
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(source, target, ignore=_ignore_generated)
        staged.append(target)
    return tuple(staged)


def main() -> None:
    staged = build_function_source()
    print(f"Staged {len(staged)} local package(s) for connector-functions deployment.")


if __name__ == "__main__":
    main()