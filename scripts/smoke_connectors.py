from __future__ import annotations

import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agents"))

from research_assistant_connectors import ResearchConnectorRegistry  # noqa: E402

QUERIES = {
    "pubmed": "retrieval augmented generation",
    "europe_pmc": "retrieval augmented generation",
    "crossref": "retrieval augmented generation",
    "openalex": "retrieval augmented generation",
    "arxiv": "retrieval augmented generation",
    "clinical_trials": "artificial intelligence",
    "grants_gov": "research infrastructure",
    "nih_reporter": "artificial intelligence",
    "datacite": "research data",
    "orcid": "machine learning",
    "ror": "University of Idaho",
    "semantic_scholar": "retrieval augmented generation",
}


def main() -> None:
    registry = ResearchConnectorRegistry()
    failures: list[str] = []

    for source, query in QUERIES.items():
        try:
            result = registry.search(source, query, limit=1)
            print(f"{source}: {len(result.records)} record(s)")
        except (httpx.HTTPError, ValueError) as exc:
            failures.append(f"{source}: {type(exc).__name__}: {exc}")

    if failures:
        raise RuntimeError("Connector smoke failures:\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
