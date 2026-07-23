from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_committed_v3_baseline_records_the_preimplementation_state() -> None:
    committed = json.loads(
        (ROOT / "apps" / "web" / "e2e" / "v3-baseline.json").read_text(encoding="utf-8")
    )

    assert committed["schema_version"] == "research-assistant.v3-baseline"
    assert committed["typography"]["minimum_px"] == 5
    assert committed["typography"]["count_below_12px"] == 195
    assert committed["interactions"]["total"] == 77
    assert committed["interactions"]["by_baseline"]["unwired"] == 15
    assert committed["interactions"]["by_baseline"]["missing"] == 16
    assert committed["agents"] == {
        "direct_code_hosted_agents": 9,
        "responses_v2_protocols": 9,
    }
