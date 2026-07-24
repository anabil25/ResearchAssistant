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
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Protocol, TypeGuard

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import ManagedIdentityCredential
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from research_assistant_api.agent_studio.models import (
    CapabilityDescriptor,
    CapabilityInstance,
    CapabilityOperation,
    HealthStatus,
    InstanceReadiness,
    OperationClass,
    OperationLifecycle,
    OperationMaturity,
)
from research_assistant_api.agent_studio.schema_ref_resolver import compute_schema_digest
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.config import Settings

#: Default discovery timeout budget when a caller does not specify one.
DEFAULT_DISCOVERY_TIMEOUT_SECONDS = 10.0

#: Adapter-owned defence-in-depth defaults, mirrored by the settings-owned
#: ``agent_studio_capability_provider_*`` bounds in ``research_assistant_api.config``.
#: They exist so a ``HttpCapabilityDiscoverySource`` constructed directly (e.g. in a
#: test) is still bounded even when no ``Settings`` object supplies overrides. None of
#: these is ever derived from the wire, the requesting principal, or a model.
DEFAULT_MAX_RESPONSE_BYTES = 8_000_000
DEFAULT_MAX_PROVIDERS = 250
DEFAULT_MAX_DESCRIPTORS_PER_PROVIDER = 500
DEFAULT_MAX_INSTANCES_PER_PROVIDER = 2_000
DEFAULT_MAX_OPERATIONS_PER_DESCRIPTOR = 200
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_DISCOVERY_DEADLINE_SECONDS = 25.0


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


class RawOperationPins(BaseModel):
    """Provider-owned operation wire values preserved **verbatim**.

    These are the operation-level pins the integration provider (contract
    ``b2745459...``) computes and owns. They are recorded exactly as received
    -- never prefixed, mutated, or coerced to ``None`` -- so a later consumer
    (audit, drift detection, operator recovery) can compare against the
    provider's own canonicalization (RFC 8785) without this adapter having to
    re-derive or vouch for them. They are deliberately kept **separate** from
    the backend's own operation digests on ``CapabilityOperation`` (which use
    this package's ``sha256:``-prefixed sorted-JSON scheme and are computed
    from the wire schema objects), so the two algorithms' outputs are never
    conflated. ``idempotency`` preserves the exact provider enum
    (``none``/``caller_key``/``provider_native``); note that ``caller_key`` is
    *conditional* and must never be collapsed to an unconditional idempotent
    boolean.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str = Field(min_length=1)
    operation_version: str = Field(min_length=1)
    idempotency: str = Field(min_length=1)
    approval_policy: str = Field(min_length=1)
    input_schema_digest: str | None = None
    output_schema_digest: str | None = None


class ProviderDescriptorPins(BaseModel):
    """Provider-owned descriptor wire pins preserved verbatim, tied to a backend id.

    ``descriptor_backend_id`` is the namespaced ``CapabilityDescriptor.id`` this
    pin-set corresponds to, so a consumer can correlate the raw provider pins
    to the mapped domain descriptor without re-parsing the wire. ``descriptor_id``
    keeps the provider's own un-namespaced id, and ``descriptor_digest`` is the
    provider's RFC-8785 content digest recorded exactly as received.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=1)
    descriptor_backend_id: str = Field(min_length=1)
    descriptor_id: str = Field(min_length=1)
    descriptor_version: str = Field(min_length=1)
    descriptor_digest: str = Field(min_length=1)
    operations: tuple[RawOperationPins, ...] = ()


class ProviderInstancePins(BaseModel):
    """Provider-owned instance wire pins preserved verbatim, tied to a backend id.

    ``instance_backend_id`` is the namespaced ``CapabilityInstance.id`` this
    pin-set corresponds to. Every listed field is a provider-computed value
    (``config_hash``, ``instance_fingerprint``, connection/destination
    authorization digests, ``provider_resource_id``, the descriptor content
    digest the instance was discovered against) recorded exactly as received.
    They are kept separate from the backend's own recomputed
    ``CapabilityInstance.descriptor_digest``/``instance_fingerprint`` (which
    ``CapabilityRegistry.register_instance`` always derives itself), never
    substituted for them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_id: str = Field(min_length=1)
    instance_backend_id: str = Field(min_length=1)
    instance_id: str = Field(min_length=1)
    provider_resource_id: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    instance_fingerprint: str = Field(min_length=1)
    descriptor_digest: str = Field(min_length=1)
    connection_authorization_digest: str = Field(min_length=1)
    allowed_destinations_digest: str = Field(min_length=1)


class DiscoveryRefreshMetadata(BaseModel):
    """Metadata a later scoped lazy/periodic/operator recovery loop would need.

    This is intentionally only the *interface* shape: it records which flat-v7
    contract/canonicalization generation a successful pass was validated
    against and which provider ids it enumerated, so a future refresh
    scheduler can decide what to re-discover and under which contract. This
    module does **not** wire any such scheduler into application startup --
    populating this metadata never triggers a refresh by itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider_contract_version: str = Field(min_length=1)
    canonicalization_version: str = Field(min_length=1)
    provider_ids: tuple[str, ...] = ()


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

    __slots__ = (
        "available",
        "descriptor_pins",
        "descriptors",
        "instance_pins",
        "instances",
        "refresh_metadata",
        "unavailable_reason",
        "warnings",
    )

    def __init__(
        self,
        *,
        descriptors: tuple[CapabilityDescriptor, ...] = (),
        instances: tuple[CapabilityInstance, ...] = (),
        warnings: tuple[str, ...] = (),
        available: bool = True,
        unavailable_reason: str | None = None,
        descriptor_pins: tuple[ProviderDescriptorPins, ...] = (),
        instance_pins: tuple[ProviderInstancePins, ...] = (),
        refresh_metadata: DiscoveryRefreshMetadata | None = None,
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
            if descriptor_pins or instance_pins or refresh_metadata is not None:
                raise ValueError(
                    "An unavailable discovery result cannot carry provider pins or refresh metadata"
                )
            if not unavailable_reason:
                raise ValueError("An unavailable discovery result must carry a non-empty unavailable_reason")
        self.descriptors = descriptors
        self.instances = instances
        self.warnings = warnings
        self.available = available
        self.unavailable_reason = unavailable_reason
        self.descriptor_pins = descriptor_pins
        self.instance_pins = instance_pins
        self.refresh_metadata = refresh_metadata


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


# --- HTTP adapter over the provider integration's flat v7 wire contract ----
#
# Translates the provider integration's own discovery HTTP surface (owned by
# the "Capability integrations" session, committed at
# b2745459bfdeae1625f35a9503e5b5fcc3478c9d) into this package's nested
# CapabilityDescriptor/CapabilityInstance domain types. This adapter reads
# only the provider's *public wire contract* (its committed OpenAPI document
# and JSON canonicalization vectors); it never imports that session's Python
# package, and never changes this package's own nested domain models.

#: The exact provider integration-contract generation this adapter has been
#: verified against. A response declaring any other ``provider_contract_version``
#: is treated as a protocol mismatch and skipped (with a warning) rather than
#: blindly translated, since this adapter's field mapping is only verified
#: correct for this exact generation.
EXPECTED_PROVIDER_CONTRACT_VERSION = "research-assistant.integration-provider.v7"

#: The canonicalization scheme the provider's own wire-level digests
#: (``descriptor_digest``/``instance_fingerprint``/``config_hash``/etc.) are
#: computed with: RFC 8785 JSON Canonicalization Scheme + SHA-256, unprefixed
#: lowercase hex. This is a **different algorithm** from this backend's own
#: ``sha256:``-prefixed sorted-``json.dumps`` scheme (``schema_ref_resolver.
#: compute_schema_digest`` / ``capability_registry._canonical_digest``) -- the
#: two are not byte-comparable. Provider-reported digests are preserved
#: **verbatim** on the ``Raw*Pins`` models (no prefix, no mutation, no drop)
#: for traceability, and are never substituted for this backend's own
#: authoritative digests, which ``CapabilityRegistry.register_instance``
#: always recomputes itself over the mapped domain objects and which this
#: adapter computes separately for operation I/O schemas.
EXPECTED_CANONICALIZATION_VERSION = "research-assistant.canonical-json.v1"

_HEX_DIGITS = frozenset("0123456789abcdef")

#: Provider ``ApprovalPolicy``/``Idempotency`` wire values. ``approval_policy``
#: collapses to ``requires_approval`` on ``CapabilityOperation`` (with the exact
#: policy preserved on ``RawOperationPins.approval_policy``); ``idempotency`` is
#: preserved exactly on ``RawOperationPins.idempotency`` and only
#: ``provider_native`` maps to an unconditional idempotent boolean (see
#: ``_map_operation``).
_APPROVAL_POLICIES = frozenset({"never", "policy_evaluated", "required"})
_IDEMPOTENCY_MODES = frozenset({"none", "caller_key", "provider_native"})

#: The single ``idempotency`` value that is *unconditionally* safe to retry.
#: ``caller_key`` is idempotent only when the caller supplies a key, so it is
#: deliberately **not** collapsed into ``CapabilityOperation.idempotent`` -- the
#: exact enum is preserved on ``RawOperationPins.idempotency`` instead.
_UNCONDITIONALLY_IDEMPOTENT = "provider_native"

_HIGH_RISK_OPERATION_CLASSES = frozenset({OperationClass.WRITE_IRREVERSIBLE, OperationClass.PRIVILEGED})
_MEDIUM_RISK_OPERATION_CLASSES = frozenset({OperationClass.WRITE_REVERSIBLE})

_HEALTH_MAP: Mapping[str, HealthStatus] = {
    "ready": HealthStatus.HEALTHY,
    "degraded": HealthStatus.DEGRADED,
    "unavailable": HealthStatus.UNHEALTHY,
    "unauthorized": HealthStatus.UNHEALTHY,
    "needs_consent": HealthStatus.UNHEALTHY,
    "misconfigured": HealthStatus.UNHEALTHY,
}

class CapabilityProviderProtocolError(RuntimeError):
    """A single provider/descriptor/instance payload failed shape or version validation.

    Raised internally by the mapping helpers below and always caught within
    ``HttpCapabilityDiscoverySource`` — never propagated to a caller — so one
    malformed provider or descriptor degrades to a ``warnings`` entry
    (skipping only that item) instead of failing the entire discovery pass.
    """


class CapabilityResponseTooLargeError(RuntimeError):
    """A provider HTTP response exceeded the adapter's settings-owned byte cap.

    Raised by ``HttpCapabilityDiscoverySource`` before an over-large body is
    ever fully buffered or JSON-parsed. On the catalog request it degrades to
    an explicit ``available=False`` result; on a per-provider request it
    degrades to a warning that skips only that provider.
    """


def _namespaced_id(provider_id: str, raw_id: str) -> str:
    """Namespaces a provider-local id so cross-provider collisions can't happen.

    ``CapabilityDiscoveryResult`` requires globally unique descriptor/instance
    identities across an *entire* result, but the provider wire contract only
    guarantees uniqueness *within* one provider's own catalog. Prefixing with
    ``provider_id`` keeps identities unique and traceable to their origin
    across a multi-provider catalog.
    """

    return f"{provider_id}:{raw_id}"


#: A safe, opaque provider id: a leading alphanumeric followed by up to 127 more
#: ``[A-Za-z0-9._-]`` characters. Deliberately excludes ``/``, ``?``, ``#``,
#: ``%``, whitespace, and anything else that could re-target or restructure the
#: authenticated URL when interpolated into the discovery path; ``..`` is also
#: rejected so a wire value can never walk the path.
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _is_safe_provider_id(provider_id: str) -> bool:
    return bool(_PROVIDER_ID_RE.fullmatch(provider_id)) and ".." not in provider_id


def _map_maturity(value: Any) -> OperationMaturity:
    try:
        return OperationMaturity(value)
    except ValueError as exc:
        raise CapabilityProviderProtocolError(f"unrecognized maturity value {value!r}") from exc


def _map_lifecycle(value: Any) -> OperationLifecycle:
    try:
        return OperationLifecycle(value)
    except ValueError as exc:
        raise CapabilityProviderProtocolError(f"unrecognized lifecycle value {value!r}") from exc


def _map_operation_class(value: Any) -> OperationClass:
    try:
        return OperationClass(value)
    except ValueError as exc:
        raise CapabilityProviderProtocolError(f"unrecognized operation_class value {value!r}") from exc


def _map_readiness(value: Any) -> InstanceReadiness:
    try:
        return InstanceReadiness(value)
    except ValueError as exc:
        raise CapabilityProviderProtocolError(f"unrecognized readiness value {value!r}") from exc


def _map_health(value: Any) -> HealthStatus:
    mapped = _HEALTH_MAP.get(value) if isinstance(value, str) else None
    if mapped is None:
        raise CapabilityProviderProtocolError(f"unrecognized health value {value!r}")
    return mapped


def _is_hex_digest(value: Any) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(char in _HEX_DIGITS for char in value)
    )


def _verbatim_required_digest(value: Any, *, field: str) -> str:
    """Preserve a required provider digest **exactly** as received, or fail closed.

    The value is validated as a well-formed lowercase 64-character SHA-256 hex
    string and returned unchanged -- never prefixed, mutated, or coerced. A
    missing or malformed value is a protocol violation (fail closed), not a
    value to silently drop to ``None``.
    """

    if _is_hex_digest(value):
        return value
    raise CapabilityProviderProtocolError(f"{field} must be a lowercase SHA-256 hex digest, got {value!r}")


def _verbatim_optional_digest(value: Any, *, field: str) -> str | None:
    """Preserve an optional provider digest verbatim; ``None`` only when absent.

    Absence (``None``) is legitimate and preserved as ``None``; a *present*
    value must be a well-formed digest and is returned unchanged (never
    prefixed or mutated). A present-but-malformed value fails closed.
    """

    if value is None:
        return None
    return _verbatim_required_digest(value, field=field)


def _verbatim_required_text(value: Any, *, field: str) -> str:
    """Preserve a required provider identifier string verbatim, or fail closed."""

    if not isinstance(value, str) or not value:
        raise CapabilityProviderProtocolError(f"{field} must be a non-empty string, got {value!r}")
    return value


def _wire_warnings(value: Any, *, source_label: str) -> tuple[str, ...]:
    """Extract provider-declared warnings from untrusted wire data, never raising.

    ``warnings`` is advisory (non-authoritative) data, so a malformed shape must
    not decide availability -- but it must not be silently dropped either, and it
    must never raise. Any shape other than an array of objects is reported *as* a
    warning describing the malformation, which is the same treatment malformed
    provider catalog entries get.

    This exists because the naive generator (``warning.get(...) for warning in
    value``) raises ``AttributeError`` on ``"boom"``, ``[1,2,3]``, ``[None]``,
    ``{"a":1}`` and ``[[1]]`` -- an exception type no caller in this module
    catches, which would let an untrusted provider decide whether the module
    honours its own fail-closed contract.
    """

    if value is None:
        return ()
    if not isinstance(value, list):
        return (f"{source_label} 'warnings' was not a JSON array ({type(value).__name__}); ignored.",)
    collected: list[str] = []
    for position, warning in enumerate(value):
        if not isinstance(warning, Mapping):
            collected.append(
                f"{source_label} warning #{position} was not a JSON object ({type(warning).__name__}); ignored."
            )
            continue
        collected.append(
            str(warning.get("message") or warning.get("reason_code") or "unknown warning")
        )
    return tuple(collected)


def _str_sequence(value: Any, *, field: str) -> tuple[str, ...]:
    """A tuple of strings from a wire array, failing closed on a non-array.

    An absent value (``None``) is an empty tuple, but a *present* non-array
    value -- or an array with a non-string member -- is a protocol violation
    rather than something to coerce: iterating a bare string would silently
    explode it into per-character entries, and ``str()``-coercing members would
    turn ``[123, null]`` into ``("123", "None")``. This matters most for
    security-relevant fields such as ``side_effect_destinations`` and
    ``least_privilege_scopes``/``least_privilege_roles``.
    """

    if value is None:
        return ()
    if not isinstance(value, list):
        raise CapabilityProviderProtocolError(f"{field} must be a JSON array, got {type(value).__name__}")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise CapabilityProviderProtocolError(
                f"{field} entries must be strings, got {type(item).__name__}"
            )
        items.append(item)
    return tuple(items)


def _backend_schema_digest(schema: Any) -> str | None:
    """This backend's *own* canonical digest of a wire I/O schema object.

    Uses this package's ``sha256:``-prefixed sorted-JSON scheme
    (``schema_ref_resolver.compute_schema_digest``) over the schema object the
    provider actually shipped on the wire -- a **separately-named backend
    canonical digest** that lives alongside (never replaces) the provider's own
    RFC-8785 ``input_schema_digest``/``output_schema_digest`` preserved verbatim
    on ``RawOperationPins``. Returns ``None`` when the schema is absent
    (``None``), but fails closed on a *present* value that is not a JSON object
    -- an absent schema and a malformed present schema are honestly different.
    """

    if schema is None:
        return None
    if not isinstance(schema, Mapping):
        raise CapabilityProviderProtocolError(
            f"wire schema must be a JSON object or absent, got {type(schema).__name__}"
        )
    return compute_schema_digest(dict(schema))


def _operation_risk(operation_class: OperationClass) -> str:
    if operation_class in _HIGH_RISK_OPERATION_CLASSES:
        return "high"
    if operation_class in _MEDIUM_RISK_OPERATION_CLASSES:
        return "medium"
    return "low"


def _map_operation(payload: Mapping[str, Any]) -> tuple[CapabilityOperation, RawOperationPins]:
    approval_policy = payload["approval_policy"]
    if approval_policy not in _APPROVAL_POLICIES:
        raise CapabilityProviderProtocolError(f"unrecognized approval_policy value {approval_policy!r}")
    idempotency = payload["idempotency"]
    if idempotency not in _IDEMPOTENCY_MODES:
        raise CapabilityProviderProtocolError(f"unrecognized idempotency value {idempotency!r}")
    operation_class = _map_operation_class(payload["operation_class"])
    docs = _str_sequence(payload.get("docs"), field="docs")
    timeout_raw = payload["timeout_seconds"]
    timeout_seconds = (
        int(timeout_raw) if isinstance(timeout_raw, int | float) and 1 <= timeout_raw <= 3600 else None
    )
    max_retries_raw = payload.get("max_retries", 0)
    max_retries = int(max_retries_raw) if isinstance(max_retries_raw, int | float) and 0 <= max_retries_raw <= 10 else 0
    # Provider RFC-8785 operation schema digests are preserved verbatim on the
    # pins; the backend's own operation schema digests are computed separately
    # from the wire schema objects (never the provider's prefixed value).
    raw_input_digest = _verbatim_optional_digest(payload.get("input_schema_digest"), field="input_schema_digest")
    raw_output_digest = _verbatim_optional_digest(payload.get("output_schema_digest"), field="output_schema_digest")
    # Operation identity is governance-relevant, not presentation text:
    # ``CapabilityDescriptor.operation(name)`` resolves approval/policy lookups by
    # it, and ``RawOperationPins.operation_id`` is the provider-owned pin used for
    # drift detection and audit correlation. So it goes through the same
    # required-string gate as every other identity, never ``str()`` coercion --
    # which would mint synthetic identities like "None"/"7"/"True" that
    # ``Field(min_length=1)`` cannot catch.
    operation_id = _verbatim_required_text(payload.get("operation_id"), field="operation_id")
    operation_version = _verbatim_required_text(payload.get("operation_version"), field="operation_version")
    operation = CapabilityOperation(
        name=operation_id,
        version=operation_version,
        maturity=_map_maturity(payload["maturity"]),
        lifecycle=_map_lifecycle(payload["lifecycle"]),
        operation_class=operation_class,
        risk=_operation_risk(operation_class),
        side_effect_destinations=_str_sequence(
            payload.get("side_effect_destinations"), field="side_effect_destinations"
        ),
        requires_approval=approval_policy != "never",
        approval_policy_ref=approval_policy if approval_policy != "never" else None,
        reason=None,
        source_url=docs[0] if docs else None,
        source_version=None,
        last_verified_at=None,
        input_schema_digest=_backend_schema_digest(payload.get("input_schema")),
        output_schema_digest=_backend_schema_digest(payload.get("output_schema")),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        # Only ``provider_native`` is unconditionally idempotent; ``caller_key``
        # is conditional (idempotent only when the caller supplies a key) and is
        # therefore never collapsed into an unconditional idempotent boolean.
        idempotent=idempotency == _UNCONDITIONALLY_IDEMPOTENT,
        least_privilege_scopes=_str_sequence(payload.get("least_privilege_scopes"), field="least_privilege_scopes"),
        least_privilege_roles=_str_sequence(payload.get("least_privilege_roles"), field="least_privilege_roles"),
    )
    pins = RawOperationPins(
        operation_id=operation_id,
        operation_version=operation_version,
        idempotency=idempotency,
        approval_policy=approval_policy,
        input_schema_digest=raw_input_digest,
        output_schema_digest=raw_output_digest,
    )
    return operation, pins


def _descriptor_risk_tier(operations: tuple[CapabilityOperation, ...]) -> str:
    """Deterministic worst-case risk tier across a descriptor's real operations.

    The provider's own ``CapabilityDescriptor`` wire shape carries no
    ``risk_tier`` field at all -- ``risk_tier``/``data_boundary``/
    ``config_schema``/``description`` are application-owned governance
    metadata, never provider-declared (confirmed against the provider's
    ``contracts.py`` dataclass, which has no such fields). Rather than a
    fabricated constant, this derives the descriptor-level tier from the
    worst (highest) risk among its own discovered operations, so a
    descriptor with even one PRIVILEGED/WRITE_IRREVERSIBLE operation is never
    mislabeled "low" risk.
    """

    if any(op.risk == "high" for op in operations):
        return "high"
    if any(op.risk == "medium" for op in operations):
        return "medium"
    return "low"


def _descriptor_description(name: str, family: str, resource_kind: str) -> str:
    """Deterministic, non-fabricated presentation label.

    The provider wire contract supplies free-text description only at the
    whole-provider level (``ProviderDescriptorResponse.description``), not
    per capability descriptor. Rather than inventing marketing copy, this
    derives a short label purely from real discovered fields; it is
    presentation-only text, never governance content.
    """

    return f"{name} ({family}/{resource_kind})"


def _map_descriptor(
    payload: Mapping[str, Any], *, provider_id: str, max_operations: int
) -> tuple[CapabilityDescriptor, ProviderDescriptorPins]:
    # The descriptor id is an identity string (it becomes half of the namespaced
    # CapabilityDescriptor.id); require it rather than str()-coercing a null/int
    # into a bogus identity like ``"None"``.
    raw_descriptor_id = _verbatim_required_text(payload.get("descriptor_id"), field="descriptor_id")
    descriptor_version = _verbatim_required_text(payload.get("descriptor_version"), field="descriptor_version")
    name = str(payload["name"])
    family = str(payload["family"])
    resource_kind = str(payload["resource_kind"])
    operations_payload = payload.get("operations") or ()
    if not operations_payload:
        raise CapabilityProviderProtocolError("descriptor has no operations")
    if len(operations_payload) > max_operations:
        raise CapabilityProviderProtocolError(
            f"descriptor declares {len(operations_payload)} operations, exceeding the adapter cap "
            f"of {max_operations}"
        )
    mapped = tuple(_map_operation(op) for op in operations_payload)
    operations = tuple(operation for operation, _ in mapped)
    operation_names = [operation.name for operation in operations]
    if len(set(operation_names)) != len(operation_names):
        # Duplicate operation ids let one operation's approval/security semantics
        # shadow another's (``CapabilityDescriptor.operation`` returns the first
        # match); reject the whole descriptor rather than retain the ambiguity.
        raise CapabilityProviderProtocolError("descriptor declares duplicate operation ids")
    backend_id = _namespaced_id(provider_id, raw_descriptor_id)
    descriptor = CapabilityDescriptor(
        id=backend_id,
        version=descriptor_version,
        provider=provider_id,
        title=name,
        description=_descriptor_description(name, family, resource_kind),
        operations=operations,
        auth_requirements=_str_sequence(payload.get("auth_modes"), field="auth_modes"),
        risk_tier=_descriptor_risk_tier(operations),
        data_boundary="project",
        config_schema={},
        managed_foundry_native=(family == "microsoft_foundry"),
    )
    pins = ProviderDescriptorPins(
        provider_id=provider_id,
        descriptor_backend_id=backend_id,
        descriptor_id=raw_descriptor_id,
        descriptor_version=descriptor_version,
        descriptor_digest=_verbatim_required_digest(payload.get("descriptor_digest"), field="descriptor_digest"),
        operations=tuple(operation_pins for _, operation_pins in mapped),
    )
    return descriptor, pins


def _map_instance(
    payload: Mapping[str, Any], *, provider_id: str, registered_by: str
) -> tuple[CapabilityInstance, ProviderInstancePins]:
    # An instance re-declares the contract generation and its owning provider;
    # both must agree with the enclosing provider discovery, or the instance is
    # an inconsistent payload we refuse to trust (fail closed, skip this item).
    # The provider echo is compared as a *string*, never via ``str()`` coercion:
    # ``str(None) == "None"``, so coercing would let a null echo satisfy the
    # cross-check for a provider legitimately named "None".
    if payload.get("provider_contract_version") != EXPECTED_PROVIDER_CONTRACT_VERSION:
        raise CapabilityProviderProtocolError(
            f"instance provider_contract_version {payload.get('provider_contract_version')!r} does not "
            f"match {EXPECTED_PROVIDER_CONTRACT_VERSION!r}"
        )
    echoed_provider_id = payload.get("provider_id")
    if not isinstance(echoed_provider_id, str) or echoed_provider_id != provider_id:
        raise CapabilityProviderProtocolError(
            f"instance provider_id {echoed_provider_id!r} does not match its enclosing provider "
            f"{provider_id!r}"
        )
    readiness = _map_readiness(payload["readiness"])
    health_status = _map_health(payload["health"])
    # ``configuration`` is a required object in flat-v7; require it here so this
    # backend's own configuration digest is always computed and drift cannot go
    # undetected (the provider's verbatim ``config_hash`` pin is preserved
    # separately below regardless).
    configuration = payload.get("configuration")
    if not isinstance(configuration, Mapping):
        raise CapabilityProviderProtocolError("instance configuration must be a JSON object")
    unavailable_reason = payload.get("unavailable_reason")
    if readiness != InstanceReadiness.READY and not unavailable_reason:
        evidence = _str_sequence(payload.get("status_evidence"), field="status_evidence")
        unavailable_reason = "; ".join(evidence) or f"Provider reported readiness={readiness.value}."
    try:
        discovered_at = datetime.fromisoformat(str(payload["last_checked_at"]))
    except ValueError as exc:
        raise CapabilityProviderProtocolError("last_checked_at is not a valid ISO-8601 timestamp") from exc
    # Identity strings (they become the namespaced instance/descriptor ids, and
    # the scope this instance claims); require them rather than str()-coercing a
    # null/int into a bogus identity such as the literal string "None".
    raw_instance_id = _verbatim_required_text(payload.get("instance_id"), field="instance_id")
    raw_descriptor_id = _verbatim_required_text(payload.get("descriptor_id"), field="descriptor_id")
    descriptor_version = _verbatim_required_text(payload.get("descriptor_version"), field="descriptor_version")
    tenant_id = _verbatim_required_text(payload.get("tenant_id"), field="tenant_id")
    project_id = _verbatim_required_text(payload.get("project_id"), field="project_id")
    backend_id = _namespaced_id(provider_id, raw_instance_id)
    instance = CapabilityInstance(
        id=backend_id,
        tenant_id=tenant_id,
        project_id=project_id,
        descriptor_id=_namespaced_id(provider_id, raw_descriptor_id),
        descriptor_version=descriptor_version,
        # The descriptor digest is always recomputed by
        # CapabilityRegistry.register_instance over the mapped domain objects;
        # the provider's own (RFC 8785) value is never trusted as
        # backend-equivalent here -- it is preserved verbatim on the pins
        # instead, so this domain object is deliberately left unset.
        descriptor_digest=None,
        discovered_provider_version=(str(payload["discovered_provider_version"]) or None)
        if payload.get("discovered_provider_version")
        else None,
        readiness=readiness,
        health_status=health_status,
        # This backend's own canonical digest of the wire configuration, so
        # CapabilityRegistry.compute_instance_fingerprint is sensitive to real
        # configuration drift (distinct from the provider's verbatim
        # ``config_hash`` pin below). instance_fingerprint itself is recomputed
        # by the registry, so the adapter leaves it unset.
        config_fingerprint=compute_schema_digest(dict(configuration)),
        instance_fingerprint=None,
        unavailable_reason=unavailable_reason if readiness != InstanceReadiness.READY else None,
        discovered_at=discovered_at,
        registered_by=registered_by,
    )
    pins = ProviderInstancePins(
        provider_id=provider_id,
        instance_backend_id=backend_id,
        instance_id=raw_instance_id,
        provider_resource_id=_verbatim_required_text(payload.get("provider_resource_id"), field="provider_resource_id"),
        config_hash=_verbatim_required_digest(payload.get("config_hash"), field="config_hash"),
        instance_fingerprint=_verbatim_required_digest(
            payload.get("instance_fingerprint"), field="instance_fingerprint"
        ),
        descriptor_digest=_verbatim_required_digest(payload.get("descriptor_digest"), field="descriptor_digest"),
        connection_authorization_digest=_verbatim_required_digest(
            payload.get("connection_authorization_digest"), field="connection_authorization_digest"
        ),
        allowed_destinations_digest=_verbatim_required_digest(
            payload.get("allowed_destinations_digest"), field="allowed_destinations_digest"
        ),
    )
    return instance, pins


#: The 5-tuple one ``_discover_one_provider`` call returns: mapped descriptors,
#: their verbatim provider pins, mapped instances, their verbatim provider pins,
#: and per-provider warnings -- pins travel 1:1 alongside their domain objects.
_ProviderDiscoveryOutcome = tuple[
    tuple[CapabilityDescriptor, ...],
    tuple[ProviderDescriptorPins, ...],
    tuple[CapabilityInstance, ...],
    tuple[ProviderInstancePins, ...],
    tuple[str, ...],
]

def _duplicates[HasId: (CapabilityDescriptor, CapabilityInstance)](
    items: Iterable[HasId],
) -> list[HasId]:
    """Every item whose ``id`` occurs more than once, order-independently.

    Used to fail closed on ambiguous identities: because the result depends only
    on *which* ids repeat and never on the order they arrived in, any permutation
    of the wire payload yields the same rejection set.
    """

    counts: Counter[str] = Counter()
    materialized = list(items)
    counts.update(item.id for item in materialized)
    return [item for item in materialized if counts[item.id] > 1]


class HttpCapabilityDiscoverySource:
    """Authenticated HTTP adapter over the provider integration's flat v7 wire contract.

    Calls ``GET /v1/providers`` (provider catalog) then, concurrently,
    ``GET /v1/providers/{provider_id}/capabilities`` (per-provider discovery),
    translating the responses into this package's own nested
    ``CapabilityDescriptor``/``CapabilityInstance`` domain types. Mirrors
    ``HttpConnectorGateway``'s managed-identity bearer-token pattern: the
    provider's own auth is a standard Azure AD/Entra token validated
    out-of-band (ARM resource-manager audience), never a client-supplied
    credential.

    Every field this adapter cannot verify or does not trust from the wire
    (governance metadata such as ``risk_tier``/``data_boundary``/
    ``description``, and every digest/fingerprint field) is either derived
    deterministically from data actually on the wire, or deliberately left
    unset so ``CapabilityRegistry`` computes it itself — this adapter never
    fabricates or blindly forwards a value it cannot stand behind. The
    provider's own wire pins (``config_hash``/``instance_fingerprint``/
    connection & destination digests/``provider_resource_id``/schema digests)
    are preserved **verbatim** on the ``descriptor_pins``/``instance_pins`` of
    the result, alongside (never in place of) those backend-owned digests.

    Every untrusted provider response is bounded before it can cost unbounded
    memory, cardinality, fan-out, or wall-clock time: response bytes are capped
    before JSON parsing, provider/descriptor/instance/operation counts are
    capped, per-provider fan-out runs under a concurrency semaphore, and the
    whole pass runs under an overall deadline. Every bound fails closed with a
    typed honest ``available=False`` result (catalog-level) or a skipped item
    with a warning (per provider). Redirects are disabled and the endpoint,
    token audience/scope, and managed-identity client are all settings-owned;
    the requesting principal is used only for attribution, never to influence
    auth, destinations, or which endpoint is called.

    A single malformed provider/descriptor/instance degrades to a
    ``warnings`` entry (skipping only that item); the whole pass only
    reports ``available=False`` when the provider catalog itself could not
    be reached, parsed, version-validated, or size/cardinality-bounded at all.
    """

    def __init__(
        self,
        base_url: str,
        *,
        credential: AsyncTokenCredential | None = None,
        token_scope: str | None = None,
        client: httpx.AsyncClient | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_providers: int = DEFAULT_MAX_PROVIDERS,
        max_descriptors_per_provider: int = DEFAULT_MAX_DESCRIPTORS_PER_PROVIDER,
        max_instances_per_provider: int = DEFAULT_MAX_INSTANCES_PER_PROVIDER,
        max_operations_per_descriptor: int = DEFAULT_MAX_OPERATIONS_PER_DESCRIPTOR,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        deadline_seconds: float = DEFAULT_DISCOVERY_DEADLINE_SECONDS,
    ) -> None:
        self._credential = credential
        self._token_scope = token_scope
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=False,
        )
        self._max_response_bytes = max_response_bytes
        self._max_providers = max_providers
        self._max_descriptors_per_provider = max_descriptors_per_provider
        self._max_instances_per_provider = max_instances_per_provider
        self._max_operations_per_descriptor = max_operations_per_descriptor
        self._max_concurrency = max_concurrency
        self._deadline_seconds = deadline_seconds

    async def _headers(self) -> dict[str, str]:
        if self._credential and self._token_scope:
            token = await self._credential.get_token(self._token_scope)
            return {"Authorization": f"Bearer {token.token}"}
        return {}

    async def _get_json(self, path: str, headers: dict[str, str]) -> Any:
        """GET ``path`` and JSON-parse it, bounding the body before it is parsed.

        The response is streamed and abandoned the moment it exceeds
        ``max_response_bytes``, so an over-large (or unbounded) provider
        response can never be fully buffered in memory or handed to the JSON
        parser. Redirects are refused per-request (belt-and-braces with the
        owned client's ``follow_redirects=False``) so a wire ``Location`` can
        never re-target the authenticated request, and only an exact ``200`` is
        accepted -- any other status (including a 3xx with a JSON body) is
        raised as ``httpx.HTTPStatusError`` after its bounded body is consumed.
        """

        chunks: list[bytes] = []
        total = 0
        async with self._client.stream("GET", path, headers=headers, follow_redirects=False) as response:
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_response_bytes:
                    raise CapabilityResponseTooLargeError(
                        f"Provider response for {path!r} exceeded the {self._max_response_bytes}-byte cap."
                    )
                chunks.append(chunk)
            if response.status_code != 200:
                raise httpx.HTTPStatusError(
                    f"{response.status_code} non-200 response for {path!r}.",
                    request=response.request,
                    response=response,
                )
        return json.loads(b"".join(chunks))

    async def discover(self, request: CapabilityDiscoveryRequest) -> CapabilityDiscoveryResult:
        """Run one discovery pass bounded by the adapter's overall deadline.

        The deadline is an adapter-owned wall-clock ceiling over the whole pass
        (catalog plus every per-provider fan-out). Exceeding it yields an honest
        ``available=False`` result rather than a partial catalog presented as
        complete. A genuine cancellation of the caller's own task is *not*
        converted here -- only this adapter's own deadline is.
        """

        try:
            async with asyncio.timeout(self._deadline_seconds):
                return await self._discover(request)
        except TimeoutError:
            return CapabilityDiscoveryResult(
                available=False,
                unavailable_reason=(
                    f"Capability discovery exceeded its {self._deadline_seconds}s overall deadline."
                ),
            )

    async def _discover(self, request: CapabilityDiscoveryRequest) -> CapabilityDiscoveryResult:
        headers = await self._headers()
        try:
            catalog = await self._get_json("v1/providers", headers)
        except (httpx.HTTPError, ValueError, CapabilityResponseTooLargeError) as exc:
            return CapabilityDiscoveryResult(
                available=False,
                unavailable_reason=f"Capability provider catalog request failed: {exc}",
            )
        if not isinstance(catalog, dict):
            return CapabilityDiscoveryResult(
                available=False,
                unavailable_reason="Capability provider catalog response was not a JSON object.",
            )
        if catalog.get("provider_contract_version") != EXPECTED_PROVIDER_CONTRACT_VERSION:
            return CapabilityDiscoveryResult(
                available=False,
                unavailable_reason=(
                    "Capability provider catalog reported an unexpected "
                    f"provider_contract_version {catalog.get('provider_contract_version')!r}; this "
                    f"adapter only translates {EXPECTED_PROVIDER_CONTRACT_VERSION!r}."
                ),
            )
        if catalog.get("canonicalization_version") != EXPECTED_CANONICALIZATION_VERSION:
            return CapabilityDiscoveryResult(
                available=False,
                unavailable_reason=(
                    "Capability provider catalog reported an unexpected canonicalization_version "
                    f"{catalog.get('canonicalization_version')!r}."
                ),
            )
        providers_payload = catalog.get("providers")
        if not isinstance(providers_payload, list):
            return CapabilityDiscoveryResult(
                available=False,
                unavailable_reason="Capability provider catalog 'providers' was not a JSON array.",
            )
        catalog_warnings = _wire_warnings(catalog.get("warnings"), source_label="Capability provider catalog")
        # Bound cardinality against the raw declared collection, before any
        # filtering or de-duplication could mask an over-large catalog.
        if len(providers_payload) > self._max_providers:
            return CapabilityDiscoveryResult(
                available=False,
                unavailable_reason=(
                    f"Capability provider catalog lists {len(providers_payload)} providers, exceeding "
                    f"the adapter cap of {self._max_providers}; refusing to present a truncated catalog "
                    "as complete."
                ),
            )
        entry_warnings: list[str] = []
        seen_provider_ids: set[str] = set()
        provider_ids: list[str] = []
        for position, entry in enumerate(providers_payload):
            candidate = entry.get("provider_id") if isinstance(entry, dict) else None
            if not isinstance(candidate, str) or not candidate:
                # Surface the dropped entry rather than silently narrowing the
                # catalog, so an available result never hides an incomplete one.
                entry_warnings.append(
                    f"Capability provider catalog entry #{position} has no usable string provider_id "
                    f"({candidate!r}); skipped."
                )
                continue
            if not _is_safe_provider_id(candidate):
                # Reject unsafe ids at the catalog boundary, not merely before
                # the request. This keeps them out of ``provider_ids`` entirely,
                # and therefore out of ``refresh_metadata.provider_ids`` -- which
                # exists to seed a future refresh scheduler and so must never
                # carry an unsanitised wire value that a later consumer could
                # interpolate into a URL. Structural impossibility rather than
                # downstream re-validation.
                entry_warnings.append(
                    f"Capability provider catalog entry #{position} declares provider_id {candidate!r}, "
                    "which is not a safe opaque identifier; skipped."
                )
                continue
            if candidate in seen_provider_ids:
                entry_warnings.append(
                    f"Provider {candidate!r} appears more than once in the catalog; the duplicate was "
                    "ignored."
                )
                continue
            seen_provider_ids.add(candidate)
            provider_ids.append(candidate)
        refresh = DiscoveryRefreshMetadata(
            provider_contract_version=EXPECTED_PROVIDER_CONTRACT_VERSION,
            canonicalization_version=EXPECTED_CANONICALIZATION_VERSION,
            provider_ids=tuple(provider_ids),
        )
        if not provider_ids:
            return CapabilityDiscoveryResult(
                available=True,
                warnings=catalog_warnings + tuple(entry_warnings),
                refresh_metadata=refresh,
            )

        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def _guarded(provider_id: str) -> _ProviderDiscoveryOutcome:
            async with semaphore:
                return await self._discover_one_provider(provider_id, headers, request.principal)

        outcomes = await asyncio.gather(
            *(_guarded(provider_id) for provider_id in provider_ids),
            return_exceptions=True,
        )

        warnings: list[str] = list(catalog_warnings) + entry_warnings
        collected_descriptors: list[tuple[str, CapabilityDescriptor, ProviderDescriptorPins]] = []
        collected_instances: list[tuple[str, CapabilityInstance, ProviderInstancePins]] = []

        for provider_id, outcome in zip(provider_ids, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                warnings.append(f"Provider {provider_id} discovery failed: {outcome}")
                continue
            (
                provider_descriptors,
                provider_descriptor_pins,
                provider_instances,
                provider_instance_pins,
                provider_warnings,
            ) = outcome
            warnings.extend(provider_warnings)
            for descriptor, descriptor_pin in zip(
                provider_descriptors, provider_descriptor_pins, strict=True
            ):
                collected_descriptors.append((provider_id, descriptor, descriptor_pin))
            for instance, instance_pin in zip(provider_instances, provider_instance_pins, strict=True):
                collected_instances.append((provider_id, instance, instance_pin))

        # Duplicate identity resolution must be deterministic on *content*, never
        # on arrival position. A keep-first (or keep-last) rule would let the
        # provider's array ordering decide which descriptor is retained -- and
        # therefore decide every downstream digest and whether a given instance
        # correlates and becomes bindable at all. So every occurrence of a
        # duplicated identity is rejected (fail closed): the outcome is identical
        # under any permutation of the wire payload.
        duplicate_descriptor_ids = {
            descriptor.id
            for descriptor in _duplicates(descriptor for _, descriptor, _ in collected_descriptors)
        }
        duplicate_instance_ids = {
            instance.id for instance in _duplicates(instance for _, instance, _ in collected_instances)
        }
        for descriptor_id in sorted(duplicate_descriptor_ids):
            warnings.append(
                f"Descriptor id {descriptor_id!r} was declared more than once; every occurrence was "
                "rejected because the correct one cannot be determined from content."
            )
        for instance_id in sorted(duplicate_instance_ids):
            warnings.append(
                f"Instance id {instance_id!r} was declared more than once; every occurrence was "
                "rejected because the correct one cannot be determined from content."
            )

        descriptors: list[CapabilityDescriptor] = []
        descriptor_pins: list[ProviderDescriptorPins] = []
        for _, descriptor, descriptor_pin in collected_descriptors:
            if descriptor.id in duplicate_descriptor_ids:
                continue
            descriptors.append(descriptor)
            descriptor_pins.append(descriptor_pin)
        kept_descriptor_ids = {descriptor.id for descriptor in descriptors}

        instances: list[CapabilityInstance] = []
        instance_pins: list[ProviderInstancePins] = []
        for provider_id, instance, instance_pin in collected_instances:
            if instance.id in duplicate_instance_ids:
                continue
            if instance.descriptor_id not in kept_descriptor_ids:
                warnings.append(
                    f"Provider {provider_id} instance id {instance.id!r} references descriptor "
                    f"{instance.descriptor_id!r} which was not discovered/kept; skipped."
                )
                continue
            instances.append(instance)
            instance_pins.append(instance_pin)

        return CapabilityDiscoveryResult(
            descriptors=tuple(descriptors),
            instances=tuple(instances),
            warnings=tuple(warnings),
            available=True,
            descriptor_pins=tuple(descriptor_pins),
            instance_pins=tuple(instance_pins),
            refresh_metadata=refresh,
        )

    async def _discover_one_provider(
        self, provider_id: str, headers: dict[str, str], registered_by: str
    ) -> _ProviderDiscoveryOutcome:
        # Defence in depth. ``_discover`` already rejects unsafe ids at the
        # catalog boundary, so this is unreachable via ``discover()``; it is
        # retained (and unit-tested directly) so that any future caller of this
        # private method cannot reintroduce path injection by bypassing that
        # boundary check.
        if not _is_safe_provider_id(provider_id):
            raise CapabilityProviderProtocolError(
                f"Provider id {provider_id!r} is not a safe opaque identifier; refusing to request it."
            )
        payload = await self._get_json(f"v1/providers/{provider_id}/capabilities", headers)
        if not isinstance(payload, dict):
            raise CapabilityProviderProtocolError(
                f"Provider {provider_id} capabilities response was not a JSON object."
            )
        if payload.get("provider_contract_version") != EXPECTED_PROVIDER_CONTRACT_VERSION:
            raise CapabilityProviderProtocolError(
                f"Provider {provider_id} reported unexpected provider_contract_version "
                f"{payload.get('provider_contract_version')!r}."
            )
        if payload.get("canonicalization_version") != EXPECTED_CANONICALIZATION_VERSION:
            raise CapabilityProviderProtocolError(
                f"Provider {provider_id} reported unexpected canonicalization_version "
                f"{payload.get('canonicalization_version')!r}."
            )
        # Compared as a *string*, never via ``str()`` coercion: ``str(None)`` is
        # the literal "None", which would let a null echo satisfy this
        # cross-check for a provider legitimately named "None".
        echoed_provider_id = payload.get("provider_id")
        if not isinstance(echoed_provider_id, str) or echoed_provider_id != provider_id:
            raise CapabilityProviderProtocolError(
                f"Provider {provider_id} capabilities response provider_id mismatch: "
                f"{echoed_provider_id!r}."
            )
        descriptors_payload = payload.get("descriptors")
        if descriptors_payload is None:
            descriptors_payload = []
        if not isinstance(descriptors_payload, list):
            raise CapabilityProviderProtocolError(
                f"Provider {provider_id} 'descriptors' was not a JSON array "
                f"({type(descriptors_payload).__name__})."
            )
        if len(descriptors_payload) > self._max_descriptors_per_provider:
            raise CapabilityProviderProtocolError(
                f"Provider {provider_id} returned {len(descriptors_payload)} descriptors, exceeding the "
                f"adapter cap of {self._max_descriptors_per_provider}."
            )
        instances_payload = payload.get("instances")
        if instances_payload is None:
            instances_payload = []
        if not isinstance(instances_payload, list):
            raise CapabilityProviderProtocolError(
                f"Provider {provider_id} 'instances' was not a JSON array "
                f"({type(instances_payload).__name__})."
            )
        if len(instances_payload) > self._max_instances_per_provider:
            raise CapabilityProviderProtocolError(
                f"Provider {provider_id} returned {len(instances_payload)} instances, exceeding the "
                f"adapter cap of {self._max_instances_per_provider}."
            )
        warnings: list[str] = list(
            _wire_warnings(payload.get("warnings"), source_label=f"Provider {provider_id}")
        )
        descriptors: list[CapabilityDescriptor] = []
        descriptor_pins: list[ProviderDescriptorPins] = []
        for position, descriptor_payload in enumerate(descriptors_payload):
            if not isinstance(descriptor_payload, Mapping):
                warnings.append(
                    f"Provider {provider_id} descriptor #{position} was not a JSON object "
                    f"({type(descriptor_payload).__name__}); skipped."
                )
                continue
            try:
                descriptor, descriptor_pin = _map_descriptor(
                    descriptor_payload,
                    provider_id=provider_id,
                    max_operations=self._max_operations_per_descriptor,
                )
            except (
                CapabilityProviderProtocolError,
                ValidationError,
                KeyError,
                ValueError,
                TypeError,
                AttributeError,
            ) as exc:
                descriptor_id = descriptor_payload.get("descriptor_id")
                warnings.append(
                    f"Provider {provider_id} descriptor {descriptor_id!r} could not be translated "
                    f"and was skipped: {exc}"
                )
                continue
            descriptors.append(descriptor)
            descriptor_pins.append(descriptor_pin)
        instances: list[CapabilityInstance] = []
        instance_pins: list[ProviderInstancePins] = []
        # Correlate each instance back to its descriptor's verbatim pins so a
        # provider that ships an instance disagreeing with its own descriptor's
        # pinned version/digest is caught instead of being silently masked when
        # the registry later stamps the current descriptor digest. Only
        # unambiguous ids get a correlation entry: a raw id declared more than
        # once has no single correct pin, so picking either would reintroduce
        # arrival-order dependence. Those descriptors are rejected wholesale at
        # aggregation, and their instances fall out via the
        # "references descriptor which was not discovered/kept" guard.
        #
        # Keyed on the *namespaced* id so the lookup uses values both sides have
        # already validated, rather than re-reading and ``str()``-coercing the
        # raw wire field a second time.
        pin_counts: Counter[str] = Counter(pin.descriptor_backend_id for pin in descriptor_pins)
        descriptor_pin_by_backend_id = {
            pin.descriptor_backend_id: pin
            for pin in descriptor_pins
            if pin_counts[pin.descriptor_backend_id] == 1
        }
        for position, instance_payload in enumerate(instances_payload):
            if not isinstance(instance_payload, Mapping):
                warnings.append(
                    f"Provider {provider_id} instance #{position} was not a JSON object "
                    f"({type(instance_payload).__name__}); skipped."
                )
                continue
            try:
                instance, instance_pin = _map_instance(
                    instance_payload, provider_id=provider_id, registered_by=registered_by
                )
            except (
                CapabilityProviderProtocolError,
                ValidationError,
                KeyError,
                ValueError,
                TypeError,
                AttributeError,
            ) as exc:
                instance_id = instance_payload.get("instance_id")
                warnings.append(
                    f"Provider {provider_id} instance {instance_id!r} could not be translated and "
                    f"was skipped: {exc}"
                )
                continue
            reference = descriptor_pin_by_backend_id.get(instance.descriptor_id)
            if reference is not None and (
                instance_pin.descriptor_digest != reference.descriptor_digest
                or instance.descriptor_version != reference.descriptor_version
            ):
                warnings.append(
                    f"Provider {provider_id} instance {instance_pin.instance_id!r} disagrees with its "
                    "descriptor's pinned version/digest; skipped."
                )
                continue
            instances.append(instance)
            instance_pins.append(instance_pin)
        return (
            tuple(descriptors),
            tuple(descriptor_pins),
            tuple(instances),
            tuple(instance_pins),
            tuple(warnings),
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        if self._credential:
            await self._credential.close()


def build_capability_discovery_source(settings: Settings) -> CapabilityDiscoverySource:
    """Wires the real HTTP adapter when configured, else the explicit-unavailable default.

    Mirrors ``build_connector_gateway``: a credential is only constructed
    when a token scope is configured (matching the provider's own
    out-of-band bearer-token authentication), and the honest
    ``NullCapabilityDiscoverySource`` is returned whenever no provider URL is
    configured at all -- never a hard-coded seed catalog masquerading as
    discovery output.
    """

    if not settings.agent_studio_capability_provider_url:
        return NullCapabilityDiscoverySource()
    credential: AsyncTokenCredential | None = None
    if settings.agent_studio_capability_provider_token_scope:
        credential = ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    return HttpCapabilityDiscoverySource(
        settings.agent_studio_capability_provider_url,
        credential=credential,
        token_scope=settings.agent_studio_capability_provider_token_scope,
        max_response_bytes=settings.agent_studio_capability_provider_max_response_bytes,
        max_providers=settings.agent_studio_capability_provider_max_providers,
        max_descriptors_per_provider=settings.agent_studio_capability_provider_max_descriptors_per_provider,
        max_instances_per_provider=settings.agent_studio_capability_provider_max_instances_per_provider,
        max_operations_per_descriptor=settings.agent_studio_capability_provider_max_operations_per_descriptor,
        max_concurrency=settings.agent_studio_capability_provider_max_concurrency,
        deadline_seconds=settings.agent_studio_capability_provider_deadline_seconds,
    )
