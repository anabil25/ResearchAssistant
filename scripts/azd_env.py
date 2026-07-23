from __future__ import annotations

import json
import os
import subprocess


def sync_canonical_azd_outputs() -> dict[str, str]:
    completed = subprocess.run(
        ["azd", "env", "get-values", "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    values: dict[str, str] = json.loads(completed.stdout)
    grouped: dict[str, list[tuple[str, str]]] = {}
    for key, value in values.items():
        grouped.setdefault(key.upper(), []).append((key, value))

    canonical: dict[str, str] = {}
    for upper_key, entries in grouped.items():
        exact = next((value for key, value in entries if key == upper_key), None)
        distinct = {value for _, value in entries}
        if exact is not None:
            canonical[upper_key] = exact
        elif len(distinct) == 1:
            canonical[upper_key] = entries[0][1]
        else:
            variants = ", ".join(key for key, _ in entries)
            raise RuntimeError(
                f"Conflicting azd values differ only by casing for {upper_key}: {variants}"
            )

    for key, value in canonical.items():
        os.environ[key] = value
        if key not in values:
            subprocess.run(
                ["azd", "env", "set", key, value],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
    return canonical
