"""Consumption seam for provider-driven capability discovery.

Agent Studio must not duplicate Foundry/tool discovery logic that belongs to
the operational provider/integration layer (see the platform correction:
"Consume provider discovery through an interface owned by integration
session; do not duplicate Foundry/tool discovery in API."). This module owns
the *port* side of that boundary: a small ``CapabilityDiscoverySource``
Protocol, expressed entirely in this package's own domain types
(``CapabilityDescriptor``/``CapabilityInstance``), that any real provider
integration can implement without Agent Studio importing that integration's
internal contracts directly.

The port is **async and scope-aware**: every discovery call carries an
explicit ``CapabilityDiscoveryRequest`` (tenant+project ``ScopeContext``,
requesting principal, correlation id, and a timeout budget). There is no
unscoped "discover everything" call — a source can always tell which
tenant/project it is being asked about, and ``CapabilityRegistry`` rejects
any returned instance whose own tenant/project does not match the request
(see ``CapabilityRegistry.from_source``).

A result also always distinguishes two honestly different situations that a
bare empty tuple cannot: **honest empty success** (``available=True``, the
provider was reachable and simply has nothing to report) versus **explicit
unavailability** (``available=False``, e.g. no provider is configured, it
timed out, or it was cancelled). ``NullCapabilityDiscoverySource`` is the
production-safe default when no real adapter is wired: it reports
``available=False`` rather than a silently empty "success", so a caller (and
the UI) can render "provider integration unavailable" instead of mistaking
it for "no capabilities discovered". ``InMemoryCapabilityDiscoverySource`` is
test-only, mirroring the ``InMemoryModelDiscovery`` pattern used for model
discovery, and additionally supports simulating a slow/cancelled provider
for ``discover_with_timeout`` contract tests.

Until a real adapter is wired (translating the operational provider layer's
``ProviderRegistry``/``DiscoveryResult`` into this module's
``CapabilityDiscoveryResult``), production composition
(``research_assistant_api.app``) wires ``NullCapabilityDiscoverySource`` (or
an equivalent explicit-unavailable source) — never a hard-coded seed catalog
masquerading as discovery output. See ``capability_registry.seeded_test_registry``
for the test-only fixture that still exercises a populated catalog.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from research_assistant_api.agent_studio.models import (
    CapabilityDescriptor,
    CapabilityInstance,
)
from research_assistant_api.agent_studio.scope import ScopeContext

#: Default discovery timeout budget when a caller does not specify one.
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 10.0


class CapabilityDiscoveryRequest(BaseModel):
    """Non-optional context for a single scope-aware discovery pass.

    There is deliberately no way to construct a "discover everything, no
    scope" request: ``scope`` is required, so every ``CapabilityDiscoverySource``
    implementation and every caller of it must always know which
    tenant+project it is discovering on behalf of. ``principal`` and
    ``correlation_id`` let a real provider adapter attribute/trace the call;
    ``timeout_seconds`` bounds how long ``discover_with_timeout`` will wait
    before treating a hung provider as unavailable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: ScopeContext
    principal: str = Field(min_length=1, max_length=200)
    correlation_id: str = Field(min_length=1, max_length=200)
    timeout_seconds: float = Field(default=DEFAULT_DISCOVERY_TIMEOUT_SECONDS, gt=0)


class CapabilityDiscoveryResult:
    """Immutable result of a single discovery pass.

    ``warnings`` carries honest, non-fatal discovery caveats (e.g. "one
    provider timed out") without ever hiding them or synthesizing fake
    success; callers may surface them to admins/operators.

    ``available``/``unavailable_reason`` distinguish "the provider was
    reachable and honestly has nothing to report" (``available=True``, empty
    ``descriptors``/``instances``) from "the provider integration itself is
    not usable right now" (``available=False``, non-empty
    ``unavailable_reason``, and — since an unavailable pass cannot vouch for
    anything it might otherwise have returned — always empty
    ``descriptors``/``instances``).
    """

    __slots__ = ("available", "descriptors", "instances", "unavailable_reason", "warnings")

    def __init__(
        self,
        *,
        descriptors: tuple[CapabilityDescriptor, ...] = (),
        instances: tuple[CapabilityInstance, ...] = (),
        warnings: tuple[str, ...] = (),
        available: bool = True,
        unavailable_reason: str | None = None,
    ) -> None:
        descriptor_ids = {descriptor.id for descriptor in descriptors}
        if len(descriptor_ids) != len(descriptors):
            raise ValueError("Capability discovery descriptor identities must be unique")
        instance_ids = {instance.id for instance in instances}
        if len(instance_ids) != len(instances):
            raise ValueError("Capability discovery instance identities must be unique")
        if any(instance.descriptor_id not in descriptor_ids for instance in instances):
            raise ValueError("Every discovered instance must reference a returned descriptor")
        if available:
            if unavailable_reason is not None:
                raise ValueError("An available discovery result must not carry an unavailable_reason")
        else:
            if descriptors or instances:
                raise ValueError(
                    "An unavailable discovery result cannot vouch for descriptors/instances; it must "
                    "be empty"
                )
            if not unavailable_reason:
                raise ValueError("An unavailable discovery result must carry a non-empty unavailable_reason")
        self.descriptors = descriptors
        self.instances = instances
        self.warnings = warnings
        self.available = available
        self.unavailable_reason = unavailable_reason


class CapabilityDiscoverySource(Protocol):
    """Port implemented by a real provider/integration-owned adapter.

    An adapter over the operational provider layer's ``ProviderRegistry``
    (Foundry/model/agent/connection, File Search, AI Search, Functions,
    Blob, MCP, OpenAPI, webhooks, GitHub, Graph, etc.) should implement this
    by calling that layer's own discovery and translating its GA-only
    output into this package's ``CapabilityDescriptor``/``CapabilityInstance``
    domain types — never the reverse, and never re-implementing that
    layer's maturity/auth/schema logic here. ``discover`` is async and
    receives the full scope-aware ``CapabilityDiscoveryRequest``; it may
    raise (e.g. ``TimeoutError``/``asyncio.CancelledError``) rather than
    return, in which case callers should prefer ``discover_with_timeout``.
    """

    async def discover(self, request: CapabilityDiscoveryRequest) -> CapabilityDiscoveryResult: ...


class NullCapabilityDiscoverySource:
    """Explicit "no external provider layer configured" default.

    Reports an explicit ``available=False`` result rather than a silently
    empty "success" — production composition must never let the absence of
    a real adapter look like an honest empty catalog. This is the
    production-safe default until a real adapter is injected.
    """

    async def discover(self, request: CapabilityDiscoveryRequest) -> CapabilityDiscoveryResult:
        return CapabilityDiscoveryResult(
            available=False,
            unavailable_reason=(
                "No capability discovery provider is configured for this deployment; provider "
                "integration is unavailable."
            ),
        )


class InMemoryCapabilityDiscoverySource:
    """Test-only discovery source backed by a fixed result.

    Must never be wired in a cloud/production path; it exists so unit tests
    can exercise ``CapabilityRegistry.from_source`` deterministically without
    a live provider integration. ``delay_seconds``/``raise_cancelled`` let a
    test simulate a slow or self-cancelling provider for
    ``discover_with_timeout`` contract tests.
    """

    def __init__(
        self,
        result: CapabilityDiscoveryResult | None = None,
        *,
        delay_seconds: float | None = None,
        raise_cancelled: bool = False,
    ) -> None:
        self._result = result if result is not None else CapabilityDiscoveryResult()
        self._delay_seconds = delay_seconds
        self._raise_cancelled = raise_cancelled

    async def discover(self, request: CapabilityDiscoveryRequest) -> CapabilityDiscoveryResult:
        if self._raise_cancelled:
            raise asyncio.CancelledError("Simulated provider-side cancellation for testing.")
        if self._delay_seconds is not None:
            await asyncio.sleep(self._delay_seconds)
        return self._result


async def discover_with_timeout(
    source: CapabilityDiscoverySource, request: CapabilityDiscoveryRequest
) -> CapabilityDiscoveryResult:
    """Await ``source.discover(request)`` bounded by ``request.timeout_seconds``.

    A provider that hangs past its timeout budget, or that is cancelled
    while discovering, must never surface as an empty-but-successful
    discovery pass: both are translated into an honest ``available=False``
    result (with a descriptive ``unavailable_reason``) instead of raising or
    silently returning nothing. This is the call path production/route code
    should use instead of calling ``source.discover`` directly.

    A genuine cancellation of the *caller's own task* (e.g. request
    disconnect, shutdown) is different from a *provider* raising
    ``CancelledError`` on its own (simulated by
    ``InMemoryCapabilityDiscoverySource(raise_cancelled=True)`` in tests):
    only the latter is translated here. ``Task.cancelling()`` (3.11+)
    distinguishes them -- if the current task itself has an outstanding
    ``.cancel()`` request, cancellation is honored and re-raised rather than
    swallowed, so this helper never breaks cooperative task cancellation.
    """

    try:
        return await asyncio.wait_for(source.discover(request), timeout=request.timeout_seconds)
    except TimeoutError:
        return CapabilityDiscoveryResult(
            available=False,
            unavailable_reason=f"Capability discovery timed out after {request.timeout_seconds}s.",
        )
    except asyncio.CancelledError:
        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling() > 0:
            raise
        return CapabilityDiscoveryResult(
            available=False,
            unavailable_reason="Capability discovery was cancelled before it completed.",
        )
