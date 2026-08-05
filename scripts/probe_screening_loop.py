"""Instrumented run: is the agent actually iterating, or one-shotting?

Counts the two loops separately -- the inner tool-calling loop inside a single
agent run, and the outer sufficiency loop that re-runs the agent -- plus every
call to the small screening deployment.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import screening.main as agent_module  # noqa: E402
from screening.main import build_agent, final_report  # noqa: E402
from smoke_screening_agent import REQUEST, _request  # noqa: E402


class Probe:
    def __init__(self) -> None:
        self.gate_calls: list[tuple[bool, int]] = []
        self.screener_calls = 0
        self.tool_batches: list[int] = []

    def wrap_gate(self) -> None:
        original = agent_module.coverage_gate

        def traced(**kwargs: Any) -> tuple[bool, str | None]:
            keep_going, feedback = original(**kwargs)
            outstanding = agent_module.outstanding_work(
                kwargs["last_result"], agent_module._CORPUS.get()
            )
            self.gate_calls.append((keep_going, len(outstanding)))
            print(
                f"[gate] ledgerid={id(agent_module._LEDGER.get())} corpus={len(agent_module._CORPUS.get())} "
                f"ledger={len(agent_module._LEDGER.get())} "
                f"outstanding={len(outstanding)} continue={keep_going}",
                flush=True,
            )
            return keep_going, feedback

        agent_module.coverage_gate = traced  # type: ignore[assignment]

    def wrap_screener(self, drop_first: bool = False) -> None:
        original = agent_module.screen_batch
        state = {"first": True}

        async def traced(client: Any, papers: list[Any]) -> Any:
            # Fault injection: silently lose half of the first batch, so the turn
            # ends with real gaps and the outer loop has something to recover.
            if drop_first and not self.gate_calls:
                state["first"] = False
                papers = papers[: len(papers) // 2]
                print(f"[fault] dropped half the first batch -> {len(papers)} kept", flush=True)
            self.screener_calls += len(papers)
            self.tool_batches.append(len(papers))
            return await original(client, papers)

        agent_module.screen_batch = traced  # type: ignore[assignment]


async def main() -> None:
    large = "--large" in sys.argv
    huge = "--huge" in sys.argv
    request = _request(large or huge)
    if huge:
        evidence = list(request["evidence"])  # type: ignore[arg-type]
        base = list(request["evidence"])  # type: ignore[arg-type]
        for copy in range(2, 5):
            for paper in base:
                evidence.append(
                    {
                        **paper,
                        "evidence_id": f"{paper['evidence_id']}x{copy}",
                        "title": f"{paper['title']} (cohort {copy})",
                    }
                )
        request = {**request, "evidence": evidence}

    probe = Probe()
    probe.wrap_gate()
    probe.wrap_screener(drop_first="--flaky" in sys.argv)

    agent = build_agent()
    session = agent.create_session()
    response = await agent.run(json.dumps(request), session=session)

    supplied = {paper["evidence_id"] for paper in request["evidence"]}  # type: ignore[index]
    report = final_report(response)
    roles = [getattr(message, "role", "?") for message in response.messages]

    print(f"\npapers supplied ........ {len(supplied)}")
    print(f"outer loop iterations .. {len(probe.gate_calls)}  (gate verdicts: {probe.gate_calls})")
    print(f"screen_papers batches .. {probe.tool_batches}")
    print(f"small-model calls ...... {probe.screener_calls}")
    print(f"messages in final run .. {len(roles)} {roles}")
    if report is None:
        print("NO PARSEABLE REPORT")
        return
    covered = {item.evidence_id for item in report.decisions} | set(report.unresolved)
    tally: dict[str, int] = {}
    for item in report.decisions:
        tally[item.decision.value] = tally.get(item.decision.value, 0) + 1
    print(f"decisions .............. {tally}")
    print(f"unresolved ............. {len(report.unresolved)}")
    print(f"coverage ............... {len(covered)}/{len(supplied)}  exact={covered == supplied}")


if __name__ == "__main__":
    asyncio.run(main())
