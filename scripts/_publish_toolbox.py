"""Publish a shared Toolbox version from the current payload definition."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts.azd_env import sync_canonical_azd_outputs  # noqa: E402

sync_canonical_azd_outputs()

from scripts.postprovision import configure_shared_toolbox  # noqa: E402

if __name__ == "__main__":
    print(configure_shared_toolbox())
