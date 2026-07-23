from __future__ import annotations

import json
from pathlib import Path

from research_assistant_core.v3_contracts import v3_contract_bundle


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    output = repository / "packages" / "contracts" / "v3-contracts.json"
    output.write_text(
        json.dumps(v3_contract_bundle(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(output)


if __name__ == "__main__":
    main()
