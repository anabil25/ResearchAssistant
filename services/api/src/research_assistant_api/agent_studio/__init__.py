"""Agent Studio platform foundation.

This package owns the application-level platform for managing agents as
governed artifacts: manifests, immutable versions, lineage/forks, ownership
and roles, deterministic runtime selection, capability attachment with honest
maturity filtering, workspace connections, GA-only memory scopes, advisory
evaluations, hard deterministic release gates, behavioral approvals and admin
escalation, development deployments with health/rollback metadata, and stable
logical agent ID resolution.

It intentionally does not modify ``agents/**`` or
``research_assistant_api.foundry`` (the Hosted Agent runtime harness); it only
*targets* Managed Foundry or Custom Hosted runtimes as deployment choices.
"""

from __future__ import annotations
