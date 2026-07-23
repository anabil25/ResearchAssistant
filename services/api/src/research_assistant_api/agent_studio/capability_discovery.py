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

``NullCapabilityDiscoverySource`` is the explicit, honest "no external
provider layer wired" default: it returns an empty result rather than
fabricating capabilities, so ``CapabilityRegistry.from_source`` never
silently invents catalog entries. ``InMemoryCapabilityDiscoverySource`` is
test-only, mirroring the ``InMemoryModelDiscovery`` pattern used for model
discovery.

Until a real adapter is wired (translating the operational provider layer's
``ProviderRegistry``/``DiscoveryResult`` into this module's
``CapabilityDiscoveryResult``), ``capability_registry.default_registry()``
continues to serve its local hard-coded seed catalog as a documented
transitional fallback — never silently duplicated with, or silently
overridden by, discovery output.
"""

from __future__ import annotations

from typing import Protocol

from research_assistant_api.agent_studio.models import (
    CapabilityDescriptor,
    CapabilityInstance,
)


class CapabilityDiscoveryResult:
    """Immutable result of a single discovery pass.

    ``warnings`` carries honest, non-fatal discovery caveats (e.g. "one
    provider timed out") without ever hiding them or synthesizing fake
    success; callers may surface them to admins/operators.
    """

    __slots__ = ("descriptors", "instances", "warnings")

    def __init__(
        self,
        *,
        descriptors: tuple[CapabilityDescriptor, ...] = (),
        instances: tuple[CapabilityInstance, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> None:
        descriptor_ids = {descriptor.id for descriptor in descriptors}
        if len(descriptor_ids) != len(descriptors):
            raise ValueError("Capability discovery descriptor identities must be unique")
        instance_ids = {instance.id for instance in instances}
        if len(instance_ids) != len(instances):
            raise ValueError("Capability discovery instance identities must be unique")
        if any(instance.descriptor_id not in descriptor_ids for instance in instances):
            raise ValueError("Every discovered instance must reference a returned descriptor")
        self.descriptors = descriptors
        self.instances = instances
        self.warnings = warnings


class CapabilityDiscoverySource(Protocol):
    """Port implemented by a real provider/integration-owned adapter.

    An adapter over the operational provider layer's ``ProviderRegistry``
    (Foundry/model/agent/connection, File Search, AI Search, Functions,
    Blob, MCP, OpenAPI, webhooks, GitHub, Graph, etc.) should implement this
    by calling that layer's own discovery and translating its GA-only
    output into this package's ``CapabilityDescriptor``/``CapabilityInstance``
    domain types — never the reverse, and never re-implementing that
    layer's maturity/auth/schema logic here.
    """

    def discover(self) -> CapabilityDiscoveryResult: ...


class NullCapabilityDiscoverySource:
    """Explicit "no external provider layer configured" default.

    Returns an empty, honest result rather than fabricating capabilities.
    This is the production-safe default until a real adapter is injected.
    """

    def discover(self) -> CapabilityDiscoveryResult:
        return CapabilityDiscoveryResult()


class InMemoryCapabilityDiscoverySource:
    """Test-only discovery source backed by a fixed result.

    Must never be wired in a cloud/production path; it exists so unit tests
    can exercise ``CapabilityRegistry.from_source`` deterministically without
    a live provider integration.
    """

    def __init__(self, result: CapabilityDiscoveryResult | None = None) -> None:
        self._result = result if result is not None else CapabilityDiscoveryResult()

    def discover(self) -> CapabilityDiscoveryResult:
        return self._result
