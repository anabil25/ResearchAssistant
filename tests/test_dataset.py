from __future__ import annotations

import pytest
from research_assistant_core.dataset import DatasetProfileError, _format_number, profile_csv
from research_assistant_core.fixtures import SAMPLE_CSV


def test_dataset_profile_matches_direct_fixture_oracle() -> None:
    profile = profile_csv(SAMPLE_CSV)

    assert profile["rows"] == 6
    assert profile["columns"] == 3
    score = next(item for item in profile["column_profiles"] if item["name"] == "score")
    assert score["null_count"] == 0
    assert score["minimum"] == "70"
    assert score["maximum"] == "88"
    assert score["mean"] == "79.17"


@pytest.mark.parametrize("content", ["", "header\n", 'not,a,csv\n"unterminated'])
def test_invalid_or_empty_dataset_is_rejected(content: str) -> None:
    with pytest.raises(DatasetProfileError):
        profile_csv(content)


def test_inline_dataset_size_is_bounded() -> None:
    with pytest.raises(DatasetProfileError, match="at most 5 MB"):
        profile_csv("column\n" + ("x" * 5_000_001))


def test_number_formatting_omits_missing_and_non_finite_values() -> None:
    assert _format_number(None) is None
    assert _format_number(float("nan")) is None
