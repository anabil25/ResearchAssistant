from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"


def _font_audit(css_path: Path) -> dict[str, Any]:
    declarations: list[dict[str, int]] = []
    for line_number, line in enumerate(css_path.read_text(encoding="utf-8").splitlines(), start=1):
        match = re.search(r"font-size:\s*([0-9]+)px", line)
        if match:
            size = int(match.group(1))
            if size < 12:
                declarations.append({"line": line_number, "size_px": size})
    return {
        "minimum_px": min((item["size_px"] for item in declarations), default=None),
        "declarations_below_12px": declarations,
        "count_below_12px": len(declarations),
    }


def _interaction_audit(manifest_path: Path) -> dict[str, Any]:
    source = manifest_path.read_text(encoding="utf-8")
    entries = re.findall(
        r'id:\s*"(?P<id>[^"]+)".*?surface:\s*"(?P<surface>[^"]+)".*?'
        r'baseline:\s*"(?P<baseline>[^"]+)".*?milestone:\s*"(?P<milestone>[^"]+)"',
        source,
        flags=re.DOTALL,
    )
    baseline_counts = Counter(entry[2] for entry in entries)
    return {
        "total": len(entries),
        "by_baseline": dict(sorted(baseline_counts.items())),
        "gaps": [
            {"id": entry[0], "surface": entry[1], "baseline": entry[2], "milestone": entry[3]}
            for entry in entries
            if entry[2] in {"unwired", "missing"}
        ],
    }


def _test_audit() -> dict[str, int]:
    playwright_source = (WEB / "e2e" / "workbench.spec.ts").read_text(encoding="utf-8")
    jest_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((WEB / "src").rglob("*.test.ts*"))
    )
    return {
        "playwright_tests": len(re.findall(r"\btest\(\s*[\"']", playwright_source)),
        "jest_tests": len(re.findall(r"\b(?:it|test)\(\s*[\"']", jest_sources)),
    }


def _api_audit() -> list[dict[str, str]]:
    app_module = importlib.import_module("research_assistant_api.app")
    specification: dict[str, Any] = app_module.app.openapi()
    routes: list[dict[str, str]] = []
    for path, item in sorted(specification["paths"].items()):
        for method in sorted(item):
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                routes.append({"method": method.upper(), "path": path})
    return routes


def _azure_agent_audit(azure_yaml: Path) -> dict[str, int]:
    source = azure_yaml.read_text(encoding="utf-8")
    return {
        "direct_code_hosted_agents": source.count("host: azure.ai.agent"),
        "responses_v2_protocols": source.count("version: 2.0.0"),
    }


def build_baseline() -> dict[str, Any]:
    tracked_sources = [
        WEB / "src" / "app" / "globals.css",
        WEB / "src" / "components" / "research-workbench.tsx",
        WEB / "src" / "components" / "studio-components.tsx",
        WEB / "src" / "components" / "workspace-views.tsx",
        WEB / "src" / "testing" / "interaction-manifest.ts",
        ROOT / "services" / "api" / "src" / "research_assistant_api" / "app.py",
        ROOT / "azure.yaml",
    ]
    digest = hashlib.sha256()
    for path in tracked_sources:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    screenshots = re.findall(
        r'capture\("([^"]+)"',
        (WEB / "e2e" / "workbench.spec.ts").read_text(encoding="utf-8"),
    )
    return {
        "schema_version": "research-assistant.v3-baseline",
        "source_sha256": digest.hexdigest(),
        "typography": _font_audit(WEB / "src" / "app" / "globals.css"),
        "interactions": _interaction_audit(WEB / "src" / "testing" / "interaction-manifest.ts"),
        "tests": _test_audit(),
        "api_routes": _api_audit(),
        "agents": _azure_agent_audit(ROOT / "azure.yaml"),
        "screenshots": screenshots,
    }


def main() -> None:
    output = WEB / "e2e" / "v3-baseline.json"
    output.write_text(
        json.dumps(build_baseline(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
