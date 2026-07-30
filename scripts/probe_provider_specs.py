"""Probe allowlisted providers for official machine-readable API specifications.

Research-only helper: reports which candidate documentation URLs return a usable
OpenAPI/Swagger document. Never writes vendored specs; discovery output is
untrusted input to a human review step.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import httpx
import yaml

CANDIDATES: dict[str, tuple[str, ...]] = {
    "clinical_trials": ("https://clinicaltrials.gov/api/oas/v2",),
    "europe_pmc": (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/swagger.json",
        "https://www.ebi.ac.uk/europepmc/webservices/rest/openapi.json",
    ),
    "crossref": (
        "https://api.crossref.org/swagger-docs",
        "https://api.crossref.org/openapi.json",
    ),
    "openalex": (
        "https://api.openalex.org/openapi.json",
        "https://api.openalex.org/swagger.json",
        "https://docs.openalex.org/openapi.json",
    ),
    "datacite": (
        "https://api.datacite.org/openapi.json",
        "https://api.datacite.org/swagger.json",
    ),
    "orcid": (
        "https://pub.orcid.org/v3.0/swagger.json",
        "https://api.orcid.org/v3.0/swagger.json",
    ),
    "ror": (
        "https://api.ror.org/openapi.json",
        "https://api.ror.org/v2/openapi.json",
    ),
    "nih_reporter": (
        "https://api.reporter.nih.gov/swagger/v2/swagger.json",
        "https://api.reporter.nih.gov/swagger.json",
    ),
    "grants_gov": (
        "https://api.grants.gov/v1/api-docs",
        "https://api.grants.gov/openapi.json",
    ),
    "semantic_scholar": (
        "https://api.semanticscholar.org/graph/v1/openapi.json",
        "https://api.semanticscholar.org/api-docs/graph.json",
    ),
    "pubmed": (),
    "arxiv": (),
}


def _parse(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        try:
            parsed = yaml.safe_load(body)
        except yaml.YAMLError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _describe(document: dict[str, Any]) -> str:
    version = document.get("openapi") or document.get("swagger") or "unknown"
    paths = document.get("paths")
    path_count = len(paths) if isinstance(paths, dict) else 0
    operations = 0
    if isinstance(paths, dict):
        for item in paths.values():
            if isinstance(item, dict):
                operations += sum(
                    1
                    for method in item
                    if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}
                )
    return f"spec={version} paths={path_count} operations={operations}"


def main() -> int:
    with httpx.Client(
        timeout=httpx.Timeout(25.0, connect=10.0),
        follow_redirects=True,
        headers={"Accept": "application/json, application/yaml, text/yaml, */*"},
    ) as client:
        for connector, urls in CANDIDATES.items():
            if not urls:
                print(f"{connector:18} NO PUBLISHED SPEC (manual authoring required)")
                continue
            for url in urls:
                try:
                    response = client.get(url)
                except httpx.HTTPError as exc:
                    print(f"{connector:18} ERROR  {url} :: {type(exc).__name__}")
                    continue
                if response.status_code != 200:
                    print(f"{connector:18} {response.status_code}    {url}")
                    continue
                document = _parse(response.content)
                if document is None or "paths" not in document:
                    print(f"{connector:18} NOT-A-SPEC {url}")
                    continue
                print(f"{connector:18} OK     {url} :: {_describe(document)}")
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
