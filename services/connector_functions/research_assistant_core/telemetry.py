from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import Protocol

from opentelemetry import metrics, trace
from opentelemetry._logs import get_logger_provider, set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)


class TelemetryMode(StrEnum):
    DISABLED = "disabled"
    LOCAL = "local"
    AZURE_MONITOR = "azure-monitor"


class CloseableCredential(Protocol):
    def close(self) -> None: ...


@dataclass(slots=True)
class TelemetryRuntime:
    mode: TelemetryMode
    service_name: str
    providers: tuple[object, ...] = ()
    cleanup: Callable[[], None] | None = None
    span_exporter: InMemorySpanExporter | None = None
    metric_reader: InMemoryMetricReader | None = None
    log_exporter: InMemoryLogRecordExporter | None = None
    closed: bool = field(default=False, init=False)

    def shutdown(self) -> None:
        if self.closed:
            return
        self.closed = True
        stack = ExitStack()
        if self.cleanup is not None:
            stack.callback(self.cleanup)
        for provider in reversed(self.providers):
            shutdown = getattr(provider, "shutdown", None)
            if callable(shutdown):
                stack.callback(shutdown)
        stack.close()


ProviderSnapshot = Callable[[], tuple[object, ...]]
AzureMonitorConfigurer = Callable[..., None]
CredentialFactory = Callable[[str | None], CloseableCredential]

_LOCAL_ENVIRONMENTS = frozenset({"dev", "development", "local", "test", "testing"})
_LOCAL_RUNTIME: TelemetryRuntime | None = None
_LOCAL_RUNTIME_LOCK = Lock()


def resolve_telemetry_mode(
    *,
    environment: str | None,
    environ: Mapping[str, str],
) -> TelemetryMode:
    explicit = environ.get("RESEARCH_TELEMETRY_MODE")
    if explicit is not None:
        try:
            return TelemetryMode(explicit.strip().lower())
        except ValueError as exc:
            raise ValueError(
                "RESEARCH_TELEMETRY_MODE must be disabled, local, or azure-monitor"
            ) from exc
    if (environment or "").strip().lower() in _LOCAL_ENVIRONMENTS:
        return TelemetryMode.LOCAL
    if environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        return TelemetryMode.AZURE_MONITOR
    return TelemetryMode.DISABLED


def global_provider_snapshot() -> tuple[object, ...]:
    return (
        trace.get_tracer_provider(),
        metrics.get_meter_provider(),
        get_logger_provider(),
    )


def validate_provider_ownership(
    actual: tuple[object, ...],
    expected: tuple[object, ...],
) -> None:
    if actual != expected:
        raise RuntimeError("Local telemetry requires unconfigured OpenTelemetry providers")


def configure_local_telemetry(service_name: str) -> TelemetryRuntime:
    global _LOCAL_RUNTIME
    with _LOCAL_RUNTIME_LOCK:
        if _LOCAL_RUNTIME is not None:
            if _LOCAL_RUNTIME.closed:
                raise RuntimeError("Local telemetry has already been shut down")
            return _LOCAL_RUNTIME

        resource = Resource({"service.name": service_name, "deployment.environment": "local"})
        span_exporter = InMemorySpanExporter()
        tracer_provider = TracerProvider(resource=resource, shutdown_on_exit=False)
        tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
        metric_reader = InMemoryMetricReader()
        meter_provider = MeterProvider(
            metric_readers=(metric_reader,),
            resource=resource,
            shutdown_on_exit=False,
        )
        log_exporter = InMemoryLogRecordExporter()  # type: ignore[no-untyped-call]
        logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False)
        logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))

        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        set_logger_provider(logger_provider)
        validate_provider_ownership(
            global_provider_snapshot(),
            (tracer_provider, meter_provider, logger_provider),
        )

        _LOCAL_RUNTIME = TelemetryRuntime(
            mode=TelemetryMode.LOCAL,
            service_name=service_name,
            providers=(tracer_provider, meter_provider, logger_provider),
            span_exporter=span_exporter,
            metric_reader=metric_reader,
            log_exporter=log_exporter,
        )
        return _LOCAL_RUNTIME


class TelemetryController:
    def __init__(
        self,
        *,
        provider_snapshot: ProviderSnapshot = global_provider_snapshot,
    ) -> None:
        self._provider_snapshot = provider_snapshot
        self._runtime: TelemetryRuntime | None = None

    @property
    def runtime(self) -> TelemetryRuntime | None:
        return self._runtime

    def configure(
        self,
        service_name: str,
        *,
        environment: str | None,
        azure_monitor_configurer: AzureMonitorConfigurer,
        credential_factory: CredentialFactory,
        environ: Mapping[str, str] | None = None,
    ) -> TelemetryMode:
        values = os.environ if environ is None else environ
        mode = resolve_telemetry_mode(environment=environment, environ=values)
        if self._runtime is not None:
            if (
                self._runtime.mode != mode
                or self._runtime.service_name != service_name
                or self._runtime.closed
            ):
                raise RuntimeError("Telemetry is already configured with a different runtime")
            return mode

        if mode == TelemetryMode.DISABLED:
            self._runtime = TelemetryRuntime(mode=mode, service_name=service_name)
            return mode
        if mode == TelemetryMode.LOCAL:
            self._runtime = configure_local_telemetry(service_name)
            return mode

        connection_string = values.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
        if not connection_string:
            raise RuntimeError(
                "Azure Monitor telemetry requires APPLICATIONINSIGHTS_CONNECTION_STRING"
            )
        credential = credential_factory(values.get("AZURE_CLIENT_ID"))
        configured = False
        try:
            azure_monitor_configurer(
                connection_string=connection_string,
                credential=credential,
                logger_name="research_assistant",
                enable_live_metrics=False,
            )
            configured = True
        finally:
            if not configured:
                credential.close()
        self._runtime = TelemetryRuntime(
            mode=mode,
            service_name=service_name,
            providers=self._provider_snapshot(),
            cleanup=credential.close,
        )
        return mode

    def shutdown(self) -> None:
        if self._runtime is not None:
            self._runtime.shutdown()
