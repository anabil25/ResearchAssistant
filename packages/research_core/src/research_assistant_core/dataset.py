from __future__ import annotations

from io import StringIO
from math import isfinite
from typing import Any

import polars as pl


class DatasetProfileError(ValueError):
    pass


def _format_number(value: Any) -> str | None:
    if value is None:
        return None
    numeric = float(value)
    if not isfinite(numeric):
        return None
    if numeric.is_integer():
        return f"{int(numeric):,}"
    return f"{numeric:,.2f}"


def profile_csv(csv_text: str) -> dict[str, Any]:
    if len(csv_text.encode()) > 5_000_000:
        raise DatasetProfileError("The inline POC profiler accepts at most 5 MB")

    try:
        frame = pl.read_csv(StringIO(csv_text), infer_schema_length=1000)
    except Exception as exc:
        raise DatasetProfileError(f"CSV could not be parsed: {exc}") from exc

    if frame.height == 0 or frame.width == 0:
        raise DatasetProfileError("Dataset must contain at least one row and column")

    columns: list[dict[str, Any]] = []
    for name, dtype in frame.schema.items():
        series = frame[name]
        profile: dict[str, Any] = {
            "name": name,
            "dtype": str(dtype),
            "null_count": series.null_count(),
            "unique_count": series.n_unique(),
        }
        if dtype.is_numeric():
            profile.update(
                {
                    "mean": _format_number(series.mean()),
                    "minimum": _format_number(series.min()),
                    "maximum": _format_number(series.max()),
                }
            )
        columns.append(profile)

    return {
        "rows": frame.height,
        "columns": frame.width,
        "column_profiles": columns,
    }
