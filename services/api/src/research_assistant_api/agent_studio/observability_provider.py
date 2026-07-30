"""Deployment Observability/Monitor read surface.

A single ``DeploymentRecord.trace_ref`` field is not a Monitor tab: agent
owners need health history, invocation-count/latency/error-rate metrics,
opaque trace correlation links, and (best-effort) tool/cost statistics for
one deployment over a time window. This module is the provider port for
that surface, following the same honest-unavailable pattern as
``model_discovery.py``: a typed ``Protocol``, a typed error, an in-memory
test double, and a real Application-Insights-backed implementation that is
only ever wired in when ``Settings.agent_studio_app_insights_resource_id``
is actually configured.

Unlike ``evaluation_runner.py``/``playground_invoker.py`` (which always
return the explicit-unavailable adapter because *execution* requires the
harness-owned runtime, out of scope for this platform session), querying
Application Insights for already-emitted telemetry is squarely within this
platform's own ownership -- so ``build_observability_provider`` mirrors
``build_model_discovery``: real when configured, explicit ``503``-raising
``UnavailableObservabilityProvider`` otherwise. Nothing here is cached or
persisted by this platform; every call re-queries the source freshly.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from azure.core.credentials import TokenCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from research_assistant_core.azure_auth import azure_credential

from research_assistant_api.agent_studio.models import (
    DeploymentObservabilitySummary,
    DeploymentRecord,
    ToolInvocationStat,
    utc_now,
)
from research_assistant_api.config import Settings

#: Source label surfaced on ``DeploymentObservabilitySummary.source`` for the
#: real Application Insights-backed provider.
APP_INSIGHTS_SOURCE = "application-insights"

#: Source label surfaced by ``InMemoryObservabilityProvider`` -- must never
#: appear in a production response.
IN_MEMORY_SOURCE = "in-memory-test-double"


class ObservabilityProviderError(RuntimeError):
    pass


class ObservabilityProvider(Protocol):
    def get_deployment_summary(
        self, deployment: DeploymentRecord, *, window: timedelta
    ) -> DeploymentObservabilitySummary: ...


class UnavailableObservabilityProvider:
    """Explicit cloud-unavailable path: no Application Insights resource is configured."""

    def get_deployment_summary(
        self, deployment: DeploymentRecord, *, window: timedelta
    ) -> DeploymentObservabilitySummary:
        del deployment, window
        raise ObservabilityProviderError(
            "No Application Insights resource is configured; deployment observability is unavailable."
        )


class InMemoryObservabilityProvider:
    """Explicit, test/offline-only provider backed by a fixed summary or factory.

    This must never be wired in a cloud/production path; it exists so unit
    tests can exercise the router route deterministically without a live
    Application Insights resource.
    """

    def __init__(self, summary: DeploymentObservabilitySummary | None = None) -> None:
        self._summary = summary

    def get_deployment_summary(
        self, deployment: DeploymentRecord, *, window: timedelta
    ) -> DeploymentObservabilitySummary:
        if self._summary is not None:
            return self._summary
        now = utc_now()
        return DeploymentObservabilitySummary(
            deployment_id=deployment.id,
            logical_agent_id=deployment.logical_agent_id,
            window_start=now - window,
            window_end=now,
            health=deployment.health,
            invocation_count=0,
            error_count=0,
            error_rate=0.0,
            source=IN_MEMORY_SOURCE,
        )


def _build_summary_query(deployment_id: str, start: datetime, end: datetime) -> str:
    """Builds the multi-statement KQL query for one deployment/window.

    Three independent tabular statements (each an unconsumed pipe
    expression) produce three result tables in ``LogsQueryResult.tables``,
    in declaration order: (0) aggregate invocation/error/latency counters
    over ``requests``+``dependencies``, (1) per-tool invocation/error
    counters over ``customEvents`` named ``ToolInvocation``, (2) distinct
    ``operation_Id`` correlation IDs (opaque trace links, capped at 20).
    All three are scoped to the same ``customDimensions.deployment_id``
    value and the same ``[start, end)`` window; this deliberately makes no
    assumption about which specific runtime emits these signals beyond the
    ``deployment_id`` custom dimension, since the harness-owned invocation
    instrumentation schema is not finalized as of this module's authorship.
    """
    escaped_id = deployment_id.replace('"', '\\"')
    start_iso = start.isoformat()
    end_iso = end.isoformat()
    return (
        f"let _start = datetime({start_iso});\n"
        f"let _end = datetime({end_iso});\n"
        f"let _scoped = union isfuzzy=true requests, dependencies\n"
        f"| where timestamp between (_start .. _end)\n"
        f'| where tostring(customDimensions["deployment_id"]) == "{escaped_id}";\n'
        f"_scoped\n"
        f"| summarize invocation_count = count(), error_count = countif(success == false), "
        f"p50 = percentile(duration, 50), p95 = percentile(duration, 95);\n"
        f"customEvents\n"
        f"| where timestamp between (_start .. _end)\n"
        f'| where name == "ToolInvocation"\n'
        f'| where tostring(customDimensions["deployment_id"]) == "{escaped_id}"\n'
        f"| summarize invocation_count = count(), "
        f'error_count = countif(tostring(customDimensions["success"]) == "false") '
        f'by tool_name = tostring(customDimensions["tool_name"])\n'
        f"| order by tool_name asc;\n"
        f"_scoped\n"
        f"| summarize by operation_Id\n"
        f"| take 20"
    )


def _row_to_dict(columns: list[str], row: Any) -> dict[str, Any]:
    return dict(zip(columns, row, strict=False))


class AppInsightsObservabilityProvider:
    """Live deployment observability via ``azure-monitor-query``'s ``LogsQueryClient``.

    Wraps the query defensively: any SDK failure, partial-result status, or
    unexpected table shape surfaces as a typed ``ObservabilityProviderError``
    rather than silently returning zeros or fabricated data.
    """

    def __init__(self, resource_id: str, credential: TokenCredential) -> None:
        self._resource_id = resource_id
        self._credential = credential

    def get_deployment_summary(
        self, deployment: DeploymentRecord, *, window: timedelta
    ) -> DeploymentObservabilitySummary:
        end = utc_now()
        start = end - window
        query = _build_summary_query(deployment.id, start, end)
        client = LogsQueryClient(self._credential)
        try:
            response = client.query_resource(self._resource_id, query, timespan=(start, end))
        except Exception as exc:  # surfaced as a typed observability error
            raise ObservabilityProviderError(
                f"Querying Application Insights for deployment {deployment.id!r} failed."
            ) from exc
        if response.status == LogsQueryStatus.PARTIAL:
            tables = list(getattr(response, "partial_data", None) or [])
        elif response.status == LogsQueryStatus.SUCCESS:
            tables = list(getattr(response, "tables", None) or [])
        else:
            raise ObservabilityProviderError(
                f"Application Insights query for deployment {deployment.id!r} did not succeed "
                f"(status={response.status!r})."
            )
        if len(tables) < 3:
            raise ObservabilityProviderError(
                f"Application Insights query for deployment {deployment.id!r} returned an "
                f"unexpected shape ({len(tables)} table(s), expected 3)."
            )
        aggregate_rows = [_row_to_dict(list(tables[0].columns), row) for row in tables[0].rows]
        aggregate = aggregate_rows[0] if aggregate_rows else {}
        invocation_count = int(aggregate.get("invocation_count") or 0)
        error_count = int(aggregate.get("error_count") or 0)
        p50 = aggregate.get("p50")
        p95 = aggregate.get("p95")
        tool_stats = tuple(
            ToolInvocationStat(
                tool_name=str(row.get("tool_name") or "unknown"),
                invocation_count=int(row.get("invocation_count") or 0),
                error_count=min(int(row.get("error_count") or 0), int(row.get("invocation_count") or 0)),
            )
            for row in (_row_to_dict(list(tables[1].columns), row) for row in tables[1].rows)
        )
        trace_links = tuple(
            str(row.get("operation_Id"))
            for row in (_row_to_dict(list(tables[2].columns), row) for row in tables[2].rows)
            if row.get("operation_Id")
        )
        error_count = min(error_count, invocation_count)
        error_rate = (error_count / invocation_count) if invocation_count > 0 else 0.0
        return DeploymentObservabilitySummary(
            deployment_id=deployment.id,
            logical_agent_id=deployment.logical_agent_id,
            window_start=start,
            window_end=end,
            health=deployment.health,
            invocation_count=invocation_count,
            error_count=error_count,
            error_rate=error_rate,
            latency_p50_ms=float(p50) if isinstance(p50, int | float) else None,
            latency_p95_ms=float(p95) if isinstance(p95, int | float) else None,
            tool_stats=tool_stats,
            trace_links=trace_links,
            estimated_cost_usd=None,  # no real cost model backs this provider; never fabricated
            source=APP_INSIGHTS_SOURCE,
        )


def build_observability_provider(settings: Settings) -> ObservabilityProvider:
    if not settings.agent_studio_app_insights_resource_id:
        return UnavailableObservabilityProvider()
    return AppInsightsObservabilityProvider(
        settings.agent_studio_app_insights_resource_id,
        azure_credential(settings.managed_identity_client_id),
    )
