# mypy: disable-error-code=import-untyped
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
import research_assistant_api.agent_studio.observability_provider as observability_provider
from azure.monitor.query import LogsQueryStatus
from research_assistant_api.agent_studio.models import (
    DeploymentEnvironment,
    DeploymentHealth,
    DeploymentObservabilitySummary,
    DeploymentRecord,
    HealthStatus,
    RuntimeTarget,
)
from research_assistant_api.agent_studio.observability_provider import (
    APP_INSIGHTS_SOURCE,
    IN_MEMORY_SOURCE,
    AppInsightsObservabilityProvider,
    InMemoryObservabilityProvider,
    ObservabilityProviderError,
    UnavailableObservabilityProvider,
    _build_summary_query,
    build_observability_provider,
)
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


def _deployment(**overrides: Any) -> DeploymentRecord:
    defaults: dict[str, Any] = dict(
        id="deployment-1",
        logical_agent_id="agent-1",
        tenant_id="tenant-1",
        project_id="project-1",
        version_id="version-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        runtime_target=RuntimeTarget.CUSTOM_HOSTED,
        deployed_by="user-1",
        health=DeploymentHealth(status=HealthStatus.HEALTHY),
    )
    defaults.update(overrides)
    return DeploymentRecord(**defaults)


def test_unavailable_observability_provider_raises() -> None:
    with pytest.raises(ObservabilityProviderError, match="unavailable"):
        UnavailableObservabilityProvider().get_deployment_summary(_deployment(), window=timedelta(hours=1))


def test_in_memory_observability_provider_default_summary() -> None:
    deployment = _deployment()
    provider = InMemoryObservabilityProvider()
    summary = provider.get_deployment_summary(deployment, window=timedelta(hours=6))
    assert summary.deployment_id == deployment.id
    assert summary.logical_agent_id == deployment.logical_agent_id
    assert summary.invocation_count == 0
    assert summary.error_count == 0
    assert summary.error_rate == 0.0
    assert summary.source == IN_MEMORY_SOURCE
    assert summary.window_end - summary.window_start == timedelta(hours=6)
    assert summary.health.status is HealthStatus.HEALTHY


def test_in_memory_observability_provider_returns_fixed_summary() -> None:
    now = datetime.now(UTC)
    fixed = DeploymentObservabilitySummary(
        deployment_id="fixed-1",
        logical_agent_id="fixed-agent",
        window_start=now - timedelta(hours=1),
        window_end=now,
        health=DeploymentHealth(),
        invocation_count=5,
        error_count=1,
        error_rate=0.2,
        source=IN_MEMORY_SOURCE,
    )
    provider = InMemoryObservabilityProvider(fixed)
    result = provider.get_deployment_summary(_deployment(), window=timedelta(hours=1))
    assert result is fixed


def test_build_summary_query_escapes_quotes_and_contains_three_statements() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)
    query = _build_summary_query('agent"1', start, end)
    assert 'agent\\"1' in query
    assert query.count("customDimensions") >= 3
    assert "ToolInvocation" in query
    assert "operation_Id" in query
    assert query.count("_scoped") >= 3  # reused across all three statements


class _FakeTable:
    def __init__(self, columns: list[str], rows: list[list[Any]]) -> None:
        self.columns = columns
        self.rows = rows


class _FakeResponse:
    def __init__(
        self,
        status: Any,
        tables: list[_FakeTable] | None = None,
        partial_data: list[_FakeTable] | None = None,
    ) -> None:
        self.status = status
        self.tables = tables
        self.partial_data = partial_data
        self.partial_error = "partial failure" if partial_data is not None else None


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, response: _FakeResponse | Exception) -> None:
    class _FakeClient:
        def __init__(self, credential: Any) -> None:
            self.credential = credential

        def query_resource(self, resource_id: str, query: str, *, timespan: Any) -> _FakeResponse:
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(observability_provider, "LogsQueryClient", _FakeClient)


def _provider() -> AppInsightsObservabilityProvider:
    return AppInsightsObservabilityProvider(
        "/subscriptions/sub/resourceGroups/rg", cast("TokenCredential", object())
    )


def test_app_insights_provider_success_path(monkeypatch: pytest.MonkeyPatch) -> None:
    aggregate_table = _FakeTable(
        ["invocation_count", "error_count", "p50", "p95"],
        [[10, 2, 120.5, 480.0]],
    )
    tool_table = _FakeTable(
        ["tool_name", "invocation_count", "error_count"],
        [["search", 4, 1], ["fetch", 2, 0]],
    )
    trace_table = _FakeTable(["operation_Id"], [["op-1"], ["op-2"], [None]])
    response = _FakeResponse(LogsQueryStatus.SUCCESS, tables=[aggregate_table, tool_table, trace_table])
    _install_fake_client(monkeypatch, response)

    provider = _provider()
    summary = provider.get_deployment_summary(_deployment(), window=timedelta(hours=24))

    assert summary.invocation_count == 10
    assert summary.error_count == 2
    assert summary.error_rate == pytest.approx(0.2)
    assert summary.latency_p50_ms == pytest.approx(120.5)
    assert summary.latency_p95_ms == pytest.approx(480.0)
    assert len(summary.tool_stats) == 2
    assert summary.tool_stats[0].tool_name == "search"
    assert summary.tool_stats[0].error_count == 1
    assert summary.trace_links == ("op-1", "op-2")  # None operation_Id filtered out
    assert summary.source == APP_INSIGHTS_SOURCE
    assert summary.estimated_cost_usd is None


def test_app_insights_provider_partial_status_uses_partial_data(monkeypatch: pytest.MonkeyPatch) -> None:
    aggregate_table = _FakeTable(["invocation_count", "error_count", "p50", "p95"], [[1, 0, 50.0, 90.0]])
    tool_table = _FakeTable(["tool_name", "invocation_count", "error_count"], [])
    trace_table = _FakeTable(["operation_Id"], [])
    response = _FakeResponse(
        LogsQueryStatus.PARTIAL, partial_data=[aggregate_table, tool_table, trace_table]
    )
    _install_fake_client(monkeypatch, response)

    provider = _provider()
    summary = provider.get_deployment_summary(_deployment(), window=timedelta(hours=1))
    assert summary.invocation_count == 1
    assert summary.tool_stats == ()
    assert summary.trace_links == ()


def test_app_insights_provider_empty_aggregate_defaults_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    aggregate_table = _FakeTable(["invocation_count", "error_count", "p50", "p95"], [])
    tool_table = _FakeTable(["tool_name", "invocation_count", "error_count"], [])
    trace_table = _FakeTable(["operation_Id"], [])
    response = _FakeResponse(LogsQueryStatus.SUCCESS, tables=[aggregate_table, tool_table, trace_table])
    _install_fake_client(monkeypatch, response)

    provider = _provider()
    summary = provider.get_deployment_summary(_deployment(), window=timedelta(hours=1))
    assert summary.invocation_count == 0
    assert summary.error_count == 0
    assert summary.error_rate == 0.0
    assert summary.latency_p50_ms is None
    assert summary.latency_p95_ms is None


def test_app_insights_provider_clamps_error_count_over_invocation_count(monkeypatch: pytest.MonkeyPatch) -> None:
    aggregate_table = _FakeTable(["invocation_count", "error_count", "p50", "p95"], [[1, 9, None, "n/a"]])
    tool_table = _FakeTable(["tool_name", "invocation_count", "error_count"], [["search", 1, 9]])
    trace_table = _FakeTable(["operation_Id"], [])
    response = _FakeResponse(LogsQueryStatus.SUCCESS, tables=[aggregate_table, tool_table, trace_table])
    _install_fake_client(monkeypatch, response)

    provider = _provider()
    summary = provider.get_deployment_summary(_deployment(), window=timedelta(hours=1))
    assert summary.error_count == 1  # clamped to invocation_count
    assert summary.error_rate == pytest.approx(1.0)
    assert summary.latency_p50_ms is None  # non-numeric p50
    assert summary.latency_p95_ms is None  # non-numeric p95
    assert summary.tool_stats[0].error_count == 1  # clamped


def test_app_insights_provider_raises_on_unexpected_status(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _FakeResponse("failure")
    _install_fake_client(monkeypatch, response)
    provider = _provider()
    with pytest.raises(ObservabilityProviderError, match="did not succeed"):
        provider.get_deployment_summary(_deployment(), window=timedelta(hours=1))


def test_app_insights_provider_raises_on_unexpected_table_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    aggregate_table = _FakeTable(["invocation_count", "error_count", "p50", "p95"], [[1, 0, 10.0, 20.0]])
    response = _FakeResponse(LogsQueryStatus.SUCCESS, tables=[aggregate_table])
    _install_fake_client(monkeypatch, response)
    provider = _provider()
    with pytest.raises(ObservabilityProviderError, match="unexpected shape"):
        provider.get_deployment_summary(_deployment(), window=timedelta(hours=1))


def test_app_insights_provider_wraps_query_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, RuntimeError("boom"))
    provider = _provider()
    with pytest.raises(ObservabilityProviderError, match="failed"):
        provider.get_deployment_summary(_deployment(), window=timedelta(hours=1))


def test_build_observability_provider_returns_unavailable_when_not_configured() -> None:
    settings = Settings(agent_studio_app_insights_resource_id=None)
    provider = build_observability_provider(settings)
    assert isinstance(provider, UnavailableObservabilityProvider)


def test_build_observability_provider_returns_app_insights_provider_when_configured() -> None:
    settings = Settings(
        agent_studio_app_insights_resource_id="/subscriptions/sub/resourceGroups/rg/providers/microsoft.insights/components/app"
    )
    provider = build_observability_provider(settings)
    assert isinstance(provider, AppInsightsObservabilityProvider)


def test_build_observability_provider_uses_managed_identity_when_client_id_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        observability_provider, "ManagedIdentityCredential", lambda client_id: f"managed:{client_id}"
    )
    settings = Settings(
        agent_studio_app_insights_resource_id="/subscriptions/sub/resourceGroups/rg/providers/microsoft.insights/components/app",
        managed_identity_client_id="client-123",
    )
    provider = build_observability_provider(settings)
    assert isinstance(provider, AppInsightsObservabilityProvider)
    assert cast(Any, provider._credential) == "managed:client-123"
