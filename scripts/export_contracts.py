from __future__ import annotations

import json
from pathlib import Path

from research_assistant_api.app import app
from research_assistant_connector_adapter.app import app as connector_app
from research_assistant_connector_adapter.provider_api import contract_app as provider_app

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "packages" / "contracts" / "openapi.json"
CONNECTOR_OUTPUT = (
    ROOT / "packages" / "contracts" / "connector-adapter-openapi.json"
)
PROVIDER_OUTPUT = (
    ROOT / "packages" / "contracts" / "provider-adapter-openapi.json"
)


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT.relative_to(ROOT)}")
    CONNECTOR_OUTPUT.write_text(
        json.dumps(connector_app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {CONNECTOR_OUTPUT.relative_to(ROOT)}")
    PROVIDER_OUTPUT.write_text(
        json.dumps(provider_app.openapi(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {PROVIDER_OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
