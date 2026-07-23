from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_workbench_has_no_numeric_font_size_below_twelve_pixels() -> None:
    css = (ROOT / "apps" / "web" / "src" / "app" / "globals.css").read_text(encoding="utf-8")
    undersized = [
        match.group(0)
        for match in re.finditer(r"font-size:\s*(\d+(?:\.\d+)?)px", css)
        if float(match.group(1)) < 12
    ]

    assert undersized == []
