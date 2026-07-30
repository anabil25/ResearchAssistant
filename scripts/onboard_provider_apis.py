"""Manually onboard provider APIs into API Management as MCP servers.

The same logic runs automatically from the azd ``postprovision`` hook; this entry
point exists for targeted re-runs against an already provisioned environment.
"""

from __future__ import annotations

import argparse
import sys

from azure.identity import DefaultAzureCredential

from scripts.provider_onboarding import onboard_provider_apis, provider_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--only", action="append", default=[], help="Restrict to specific connector IDs.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    entries = provider_manifest()
    if args.only:
        selected = set(args.only)
        entries = [entry for entry in entries if entry["connectorId"] in selected]
    if not entries:
        raise SystemExit("No providers selected")

    if args.dry_run:
        for entry in entries:
            tools = len(entry["operationIds"])
            print(f"{entry['connectorId']:18} {entry['provenance']:8} mcp={entry['mcpApiId']} tools={tools}")
        return 0

    onboard_provider_apis(
        DefaultAzureCredential(),
        subscription_id=args.subscription,
        resource_group=args.resource_group,
        service_name=args.service,
        entries=entries,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
