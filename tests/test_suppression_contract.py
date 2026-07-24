from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.check_suppression_contract import (
    INITIAL_MISSING_REASON_MAXIMUM,
    INTEGRATION_REFRESH_REVIEW,
    MISSING_REASON_POLICY,
    Suppression,
    _javascript_comments,
    census,
    compare_inventory,
    missing_reason_policy,
    parse_javascript_comment,
    parse_python_comment,
    validate_inventory,
    validate_missing_reason_policy,
)


def test_python_suppressions_distinguish_scope_and_reason() -> None:
    scoped = parse_python_comment("sample.py", "# type:" " ignore[arg-type] - invalid double")
    bare = parse_python_comment("sample.py", "# type:" " ignore")
    noqa = parse_python_comment("sample.py", "# noqa: E402 - bootstrap import order")
    pragma = parse_python_comment(
        "sample.py",
        "# pragma:" " no cover - protocol guarantees an attempt",
    )

    assert scoped == [Suppression("sample.py", "type-ignore", "arg-type", "invalid double")]
    assert bare == [Suppression("sample.py", "type-ignore", "", "")]
    assert noqa == [Suppression("sample.py", "noqa", "E402", "bootstrap import order")]
    assert pragma == [
        Suppression(
            "sample.py",
            "coverage-pragma",
            "",
            "protocol guarantees an attempt",
        )
    ]


def test_javascript_scanner_ignores_strings_and_reads_template_expressions() -> None:
    marker = "eslint-" + "disable-next-line"
    source = (
        f'const text = "// {marker} no-console";\n'
        f"const value = `${{ /* {marker} no-alert -- guarded */ alert('x') }}`;\n"
    )

    comments = list(_javascript_comments(source))
    entries = [
        entry
        for comment in comments
        for entry in parse_javascript_comment("sample.ts", comment)
    ]

    assert entries == [
        Suppression("sample.ts", "eslint-disable", "no-alert", "guarded")
    ]


def test_compare_inventory_is_exact_in_both_directions() -> None:
    expected = {"sourceSuppressions": [{"path": "a.py"}]}

    assert compare_inventory(expected, expected) == []
    assert compare_inventory(expected, {"sourceSuppressions": []})
    assert compare_inventory(expected, {"sourceSuppressions": [{"path": "b.py"}]})


def test_missing_reason_grandfather_is_exact_and_initially_capped() -> None:
    records = [_suppression_record(count=INITIAL_MISSING_REASON_MAXIMUM)]
    inventory = _minimal_inventory()
    inventory["sourceSuppressions"] = records
    inventory["missingReasonPolicy"] = missing_reason_policy(
        records,
        None,
        integration_refresh=False,
    )

    assert validate_missing_reason_policy(inventory, None) == []
    inventory["sourceSuppressions"][0]["count"] += 1
    inventory["missingReasonPolicy"] = missing_reason_policy(
        inventory["sourceSuppressions"],
        None,
        integration_refresh=False,
    )

    assert (
        "initial missing-reason grandfather exceeds 74; "
        "use the one-time reviewed integration refresh"
    ) in validate_missing_reason_policy(inventory, None)


def test_missing_reason_grandfather_allows_only_exact_shrink() -> None:
    historical = _minimal_inventory()
    historical["sourceSuppressions"] = [_suppression_record(count=2)]
    historical["missingReasonPolicy"] = missing_reason_policy(
        historical["sourceSuppressions"],
        None,
        integration_refresh=False,
    )
    current = _minimal_inventory()
    current["sourceSuppressions"] = [_suppression_record(count=1)]
    current["missingReasonPolicy"] = missing_reason_policy(
        current["sourceSuppressions"],
        historical,
        integration_refresh=False,
    )

    assert validate_missing_reason_policy(current, historical) == []

    current["sourceSuppressions"] = [
        _suppression_record(count=1),
        _suppression_record(path="new.py"),
    ]
    current["missingReasonPolicy"] = missing_reason_policy(
        current["sourceSuppressions"],
        historical,
        integration_refresh=False,
    )

    errors = validate_missing_reason_policy(current, historical)
    assert any(MISSING_REASON_POLICY in error for error in errors)
    assert "missing-reason grandfather cardinality may only decrease" not in errors


def test_missing_reason_integration_refresh_is_exact_and_one_time() -> None:
    historical = _minimal_inventory()
    historical["sourceSuppressions"] = [_suppression_record()]
    historical["missingReasonPolicy"] = missing_reason_policy(
        historical["sourceSuppressions"],
        None,
        integration_refresh=False,
    )
    current = _minimal_inventory()
    current["sourceSuppressions"] = [
        _suppression_record(),
        _suppression_record(path="incoming.py"),
    ]
    current["missingReasonPolicy"] = missing_reason_policy(
        current["sourceSuppressions"],
        historical,
        integration_refresh=True,
    )

    assert validate_missing_reason_policy(current, historical) == []

    future = _minimal_inventory()
    future["sourceSuppressions"] = [
        *current["sourceSuppressions"],
        _suppression_record(path="later.py"),
    ]
    future["missingReasonPolicy"] = missing_reason_policy(
        future["sourceSuppressions"],
        current,
        integration_refresh=False,
    )

    errors = validate_missing_reason_policy(future, current)
    assert any("New reasonless suppression is not grandfathered: later.py" in error for error in errors)
    assert "missing-reason grandfather cardinality may only decrease" in errors


def test_validate_inventory_rejects_bare_and_javascript_suppressions(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inventory["sourceSuppressions"] = [
        _suppression_record(kind="type-ignore", scope=""),
        _suppression_record(path="web.ts", kind="ts-expect-error", scope=""),
    ]

    errors = validate_inventory(tmp_path, inventory, [])

    assert "bare type-ignore is forbidden: sample.py" in errors
    assert "ts-expect-error suppressions are pinned to zero: web.ts" in errors


def test_validate_inventory_requires_pragma_reason(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inventory["sourceSuppressions"] = [
        _suppression_record(kind="coverage-pragma", scope="", reason="")
    ]

    assert "coverage pragma requires a stated reason: sample.py" in validate_inventory(
        tmp_path,
        inventory,
        [],
    )


def test_validate_inventory_rejects_coverage_gate_and_domain_drift(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    inventory["coverageConfig"]["run"]["branch"] = False
    inventory["coverageConfig"]["run"]["source"] = []
    inventory["coverageConfig"]["report"]["fail_under"] = 99
    inventory["reportedCoverageFiles"] = []

    errors = validate_inventory(tmp_path, inventory, ["src/package/module.py:1:unexpected"])

    assert "coverage branch measurement must remain enabled" in errors
    assert "coverage fail_under must remain exactly 100" in errors
    assert "configured coverage source roots differ from packaging-derived roots" in errors
    assert "coverage JSON file set differs from the packaging-derived source file set" in errors
    assert "unclassified coverage exclusion: src/package/module.py:1:unexpected" in errors


def test_validate_inventory_requires_load_bearing_links(tmp_path: Path) -> None:
    inventory = _minimal_inventory()
    record = _suppression_record()
    record["role"] = "load-bearing"
    inventory["sourceSuppressions"] = [record]

    errors = validate_inventory(tmp_path, inventory, [])

    assert "load-bearing suppression lacks protectedTest: sample.py" in errors
    assert "load-bearing suppression lacks protectedControl: sample.py" in errors


def test_validate_inventory_accepts_resolved_load_bearing_links(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_store.py"
    source_file = tmp_path / "src" / "store.py"
    test_file.parent.mkdir()
    source_file.parent.mkdir()
    test_file.write_text("def test_conflict(): pass\n", encoding="utf-8")
    source_file.write_text("def recover_conflict(): pass\n", encoding="utf-8")
    inventory = _minimal_inventory()
    record = _suppression_record()
    record.update(
        {
            "role": "load-bearing",
            "protectedTest": {
                "path": "tests/test_store.py",
                "anchor": "test_conflict",
                "description": "Constructs a collaborator defect.",
            },
            "protectedControl": {
                "path": "src/store.py",
                "anchor": "recover_conflict",
                "description": "Exercises conflict recovery.",
            },
        }
    )
    inventory["sourceSuppressions"] = [record]

    assert validate_inventory(tmp_path, inventory, []) == []


def test_census_separates_posture_scope_and_structural_lines() -> None:
    inventory = _minimal_inventory()
    inventory["sourceSuppressions"] = [
        _suppression_record(posture="production", count=2),
        _suppression_record(path="test_sample.py", kind="noqa", scope="F401"),
    ]
    inventory["coverageExcludedLines"] = [
        {
            "path": "protocol.py",
            "kind": "tool-default",
            "source": "...",
            "reason": "",
            "count": 4,
        }
    ]
    inventory["coverageStructuralExclusions"] = [
        {
            "path": "protocol.py",
            "kind": "coverage-default-ellipsis-stub",
            "symbol": "Protocol.run",
            "reason": "coverage.py default",
        }
    ]

    summary = census(inventory)

    assert summary["sourceSuppressions"] == 3
    assert summary["byPosture"] == {"production": 2, "test": 1}
    assert summary["bare"] == 0
    assert summary["scoped"] == 3
    assert summary["reasonMissing"] == 3
    assert summary["coverageExcludedLines"] == 4
    assert summary["coverageStructuralRoots"] == 1


def test_committed_suppression_contract_has_expected_zero_categories() -> None:
    root = Path(__file__).resolve().parents[1]
    baseline = json.loads(
        (root / ".github" / "suppression-contract.json").read_text(encoding="utf-8")
    )
    kinds = {record["kind"] for record in baseline["sourceSuppressions"]}

    assert baseline["schemaVersion"] == "research-assistant.suppression-contract.v1"
    assert not kinds.intersection(
        {
            "coverage-pragma",
            "eslint-disable",
            "ts-ignore",
            "ts-expect-error",
            "istanbul-ignore",
            "c8-ignore",
        }
    )
    assert all(
        record["scope"]
        for record in baseline["sourceSuppressions"]
        if record["kind"] in {"type-ignore", "noqa"}
    )
    policy = baseline["missingReasonPolicy"]
    assert policy["requirement"] == MISSING_REASON_POLICY
    assert policy["initialMaximum"] == INITIAL_MISSING_REASON_MAXIMUM
    assert sum(record["count"] for record in policy["grandfathered"]) == 74
    assert policy["integrationRefresh"]["used"] is False


def _minimal_inventory() -> dict[str, Any]:
    return {
        "coverageConfig": {
            "run": {
                "branch": True,
                "relative_files": True,
                "source": ["src/package"],
                "omit": [],
            },
            "report": {
                "fail_under": 100,
                "precision": 2,
                "show_missing": True,
                "skip_empty": True,
                "exclude_lines": [],
                "exclude_also": [],
            },
            "json": {"output": "coverage.json", "pretty_print": True},
            "xml": {"output": "coverage.xml"},
        },
        "discoveredSourceRoots": ["src/package"],
        "sourceFiles": ["src/package/module.py"],
        "reportedCoverageFiles": ["src/package/module.py"],
        "moduleNames": ["package.module"],
        "productionFiles": [],
        "sourceSuppressions": [],
        "missingReasonPolicy": {
            "requirement": MISSING_REASON_POLICY,
            "initialMaximum": INITIAL_MISSING_REASON_MAXIMUM,
            "grandfathered": [],
            "integrationRefresh": {
                "used": False,
                "maximumAfter": None,
                "addedGrandfathered": [],
                "reviewRequirement": INTEGRATION_REFRESH_REVIEW,
            },
        },
        "coverageStructuralExclusions": [],
        "coverageExcludedLines": [],
    }


def _suppression_record(
    *,
    path: str = "sample.py",
    kind: str = "type-ignore",
    scope: str = "arg-type",
    reason: str = "",
    posture: str = "test",
    count: int = 1,
) -> dict[str, object]:
    return {
        "path": path,
        "kind": kind,
        "scope": scope,
        "reason": reason,
        "posture": posture,
        "role": "standard",
        "protectedTest": None,
        "protectedControl": None,
        "count": count,
    }
