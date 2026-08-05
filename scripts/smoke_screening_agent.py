"""Live smoke: run the screening agent against the deployed Foundry models."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))

from screening.main import build_agent, final_report  # noqa: E402

REQUEST = {
    "query": "Screen for a review of randomised trials of AI triage in emergency care.",
    "tenant_id": os.environ.get("AZURE_TENANT_ID", "demo"),
    "project_id": os.environ.get("AZURE_AI_PROJECT_NAME", "researchAssistant"),
    "principal_id": "smoke-runner",
    "session_id": "smoke-1",
    "inclusion_criteria": [
        "Randomised controlled trial",
        "Adult patients in an emergency department",
        "Reports a triage accuracy or time-to-treatment outcome",
    ],
    "exclusion_criteria": [
        "Editorial, commentary, or letter",
        "Protocol without results",
        "Paediatric-only population",
    ],
    "evidence": [
        {
            "evidence_id": "p1",
            "title": "Machine-assisted triage in adult emergency care: a randomised trial",
            "abstract": (
                "We randomised 1,204 adults presenting to two emergency departments to "
                "algorithmic triage support or usual care. Primary outcome was triage "
                "accuracy against expert adjudication; median time-to-treatment fell from "
                "48 to 39 minutes."
            ),
        },
        {
            "evidence_id": "p2",
            "title": "Why algorithmic triage will not fix crowding",
            "abstract": (
                "In this commentary we argue that triage automation addresses a symptom "
                "rather than the structural causes of emergency department crowding."
            ),
        },
        {
            "evidence_id": "p3",
            "title": "Protocol for a randomised trial of AI triage in paediatric emergency care",
            "abstract": (
                "This protocol describes a planned randomised trial in children aged 2-16. "
                "Recruitment has not yet begun and no results are reported."
            ),
        },
        {
            "evidence_id": "p4",
            "title": "Deep learning for acuity prediction: a retrospective cohort",
            "abstract": (
                "We retrospectively analysed 88,000 adult emergency visits and trained a "
                "model to predict acuity. No randomisation was performed."
            ),
        },
        {
            "evidence_id": "p5",
            "title": "Triage decision support and time to antibiotics in adult sepsis",
            "abstract": (
                "A stepped-wedge study across four hospitals. Adults with suspected sepsis. "
                "Time to antibiotics is reported. Allocation was by cluster and period."
            ),
        },
    ],
}


_FILLER = [
    ("Cluster-randomised triage support across twelve adult EDs", "Adults. Randomised by site. Reports triage accuracy."),
    ("Nurse-led triage versus algorithmic triage: randomised comparison", "Adults in an ED, randomised, time-to-treatment reported."),
    ("Letter: on the limits of triage benchmarks", "A letter to the editor responding to a recent trial."),
    ("Paediatric acuity scoring with gradient boosting", "Children under 16 only. Retrospective."),
    ("Randomised trial of triage support in rural emergency departments", "Adults randomised across six rural EDs; accuracy reported."),
    ("Systematic review of AI triage tools", "A review of 42 studies. No primary randomisation."),
    ("Time-to-antibiotic after triage automation: before-and-after study", "Adults. No randomisation; sequential cohorts."),
    ("Protocol: randomised evaluation of triage AI in adult ED", "Protocol only. No results reported."),
    ("Randomised crossover of two triage algorithms in adults", "Adults, randomised crossover, triage accuracy reported."),
    ("Emergency department crowding: a narrative overview", "Narrative overview, no primary data."),
]


def _request(large: bool) -> dict[str, object]:
    if not large:
        return REQUEST
    evidence = list(REQUEST["evidence"])  # type: ignore[arg-type]
    for index, (title, abstract) in enumerate(_FILLER, start=6):
        evidence.append({"evidence_id": f"p{index}", "title": title, "abstract": abstract})
    return {**REQUEST, "evidence": evidence}


async def main() -> None:
    large = "--large" in sys.argv
    request = _request(large)
    agent = build_agent()
    session = agent.create_session()
    response = await agent.run(json.dumps(request), session=session)
    report = final_report(response)
    if report is None:
        print("no parseable report; raw transcript follows")
        print(response.text[:1500])
        raise SystemExit(1)
    print("\n=== decisions ===")
    for item in sorted(report.decisions, key=lambda d: int(d.evidence_id.lstrip("p") or 0)):
        print(f"  {item.evidence_id}: {item.decision.value:8} [{item.criterion[:34]}] {item.rationale[:60]}")
    print(f"\nunresolved: {report.unresolved}")
    print(f"conflicts:  {len(report.conflicts)}")
    print(f"\nsummary: {report.summary[:300]}")
    supplied = {paper["evidence_id"] for paper in request["evidence"]}  # type: ignore[index]
    covered = {item.evidence_id for item in report.decisions} | set(report.unresolved)
    print(f"\ncoverage: {len(covered)}/{len(supplied)}  exact={covered == supplied}")


if __name__ == "__main__":
    asyncio.run(main())
