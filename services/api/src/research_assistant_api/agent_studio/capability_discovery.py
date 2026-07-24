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
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol

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
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.config import Settings

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
#: ``capability_registry._canonical_digest`` (sorted ``json.dumps`` + SHA-256,
#: ``sha256:``-prefixed) -- the two are not byte-comparable. Provider-reported
#: digests are recorded verbatim, namespaced with ``_PROVIDER_DIGEST_PREFIX``
#: for traceability only, and are never substituted for this backend's own
#: authoritative digests, which ``CapabilityRegistry.register_instance``
#: always recomputes itself over the mapped domain objects.
EXPECTED_CANONICALIZATION_VERSION = "research-assistant.canonical-json.v1"

#: Prefix distinguishing a provider-reported (RFC 8785) digest from this
#: backend's own (sorted-JSON) ``sha256:``-prefixed digests, so the two
#: algorithms' outputs are never mistaken for one another downstream.
_PROVIDER_DIGEST_PREFIX = "provider-rfc8785-sha256:"

_HEX_DIGITS = frozenset("0123456789abcdef")

#: Provider ``ApprovalPolicy``/``Idempotency`` wire values (no backend enum
#: equivalent exists -- these collapse to booleans on ``CapabilityOperation``,
#: see module docstring/coordination notes on the lossy-but-intentional
#: mapping).
_APPROVAL_POLICIES = frozenset({"never", "policy_evaluated", "required"})
_IDEMPOTENCY_MODES = frozenset({"none", "caller_key", "provider_native"})

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


def _namespaced_id(provider_id: str, raw_id: str) -> str:
    """Namespaces a provider-local id so cross-provider collisions can't happen.

    ``CapabilityDiscoveryResult`` requires globally unique descriptor/instance
    identities across an *entire* result, but the provider wire contract only
    guarantees uniqueness *within* one provider's own catalog. Prefixing with
    ``provider_id`` keeps identities unique and traceable to their origin
    across a multi-provider catalog.
    """

    return f"{provider_id}:{raw_id}"


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


def _tag_provider_digest(value: Any) -> str | None:
    """Namespaces a provider-reported RFC-8785 digest, or rejects a malformed one.

    ``None`` is a legitimate input (an optional/absent digest); anything else
    must be a well-formed lowercase 64-character SHA-256 hex string, since a
    malformed digest string is a protocol violation, not a value to silently
    pass through.
    """

    if value is None:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(char not in _HEX_DIGITS for char in value)
    ):
        raise CapabilityProviderProtocolError(f"expected a lowercase SHA-256 hex digest, got {value!r}")
    return f"{_PROVIDER_DIGEST_PREFIX}{value}"


def _operation_risk(operation_class: OperationClass) -> str:
    if operation_class in _HIGH_RISK_OPERATION_CLASSES:
        return "high"
    if operation_class in _MEDIUM_RISK_OPERATION_CLASSES:
        return "medium"
    return "low"


def _map_operation(payload: Mapping[str, Any]) -> CapabilityOperation:
    approval_policy = payload["approval_policy"]
    if approval_policy not in _APPROVAL_POLICIES:
        raise CapabilityProviderProtocolError(f"unrecognized approval_policy value {approval_policy!r}")
    idempotency = payload["idempotency"]
    if idempotency not in _IDEMPOTENCY_MODES:
        raise CapabilityProviderProtocolError(f"unrecognized idempotency value {idempotency!r}")
    operation_class = _map_operation_class(payload["operation_class"])
    docs = tuple(str(doc) for doc in (payload.get("docs") or ()))
    timeout_raw = payload["timeout_seconds"]
    timeout_seconds = (
        int(timeout_raw) if isinstance(timeout_raw, int | float) and 1 <= timeout_raw <= 3600 else None
    )
    max_retries_raw = payload.get("max_retries", 0)
    max_retries = int(max_retries_raw) if isinstance(max_retries_raw, int | float) and 0 <= max_retries_raw <= 10 else 0
    return CapabilityOperation(
        name=str(payload["operation_id"]),
        version=str(payload["operation_version"]),
        maturity=_map_maturity(payload["maturity"]),
        lifecycle=_map_lifecycle(payload["lifecycle"]),
        operation_class=operation_class,
        risk=_operation_risk(operation_class),
        side_effect_destinations=tuple(str(d) for d in (payload.get("side_effect_destinations") or ())),
        requires_approval=approval_policy != "never",
        approval_policy_ref=approval_policy if approval_policy != "never" else None,
        reason=None,
        source_url=docs[0] if docs else None,
        source_version=None,
        last_verified_at=None,
        input_schema_digest=_tag_provider_digest(payload.get("input_schema_digest")),
        output_schema_digest=_tag_provider_digest(payload.get("output_schema_digest")),
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        idempotent=idempotency != "none",
        least_privilege_scopes=tuple(str(s) for s in (payload.get("least_privilege_scopes") or ())),
        least_privilege_roles=tuple(str(s) for s in (payload.get("least_privilege_roles") or ())),
    )


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


def _map_descriptor(payload: Mapping[str, Any], *, provider_id: str) -> CapabilityDescriptor:
    name = str(payload["name"])
    family = str(payload["family"])
    resource_kind = str(payload["resource_kind"])
    operations_payload = payload.get("operations") or ()
    if not operations_payload:
        raise CapabilityProviderProtocolError("descriptor has no operations")
    operations = tuple(_map_operation(op) for op in operations_payload)
    return CapabilityDescriptor(
        id=_namespaced_id(provider_id, str(payload["descriptor_id"])),
        version=str(payload["descriptor_version"]),
        provider=provider_id,
        title=name,
        description=_descriptor_description(name, family, resource_kind),
        operations=operations,
        auth_requirements=tuple(str(a) for a in (payload.get("auth_modes") or ())),
        risk_tier=_descriptor_risk_tier(operations),
        data_boundary="project",
        config_schema={},
        managed_foundry_native=(family == "microsoft_foundry"),
    )


def _map_instance(payload: Mapping[str, Any], *, provider_id: str, registered_by: str) -> CapabilityInstance:
    readiness = _map_readiness(payload["readiness"])
    health_status = _map_health(payload["health"])
    unavailable_reason = payload.get("unavailable_reason")
    if readiness != InstanceReadiness.READY and not unavailable_reason:
        evidence = tuple(str(item) for item in (payload.get("status_evidence") or ()))
        unavailable_reason = "; ".join(evidence) or f"Provider reported readiness={readiness.value}."
    try:
        discovered_at = datetime.fromisoformat(str(payload["last_checked_at"]))
    except ValueError as exc:
        raise CapabilityProviderProtocolError("last_checked_at is not a valid ISO-8601 timestamp") from exc
    return CapabilityInstance(
        id=_namespaced_id(provider_id, str(payload["instance_id"])),
        tenant_id=str(payload["tenant_id"]),
        project_id=str(payload["project_id"]),
        descriptor_id=_namespaced_id(provider_id, str(payload["descriptor_id"])),
        descriptor_version=str(payload["descriptor_version"]),
        # Authoritative digest/fingerprint are always recomputed by
        # CapabilityRegistry.register_instance over the mapped domain
        # objects; the provider's own (RFC 8785) values are never trusted as
        # backend-equivalent, so they are deliberately left unset here.
        descriptor_digest=None,
        discovered_provider_version=(str(payload["discovered_provider_version"]) or None)
        if payload.get("discovered_provider_version")
        else None,
        readiness=readiness,
        health_status=health_status,
        config_fingerprint=None,
        instance_fingerprint=None,
        unavailable_reason=unavailable_reason if readiness != InstanceReadiness.READY else None,
        discovered_at=discovered_at,
        registered_by=registered_by,
    )


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
    fabricates or blindly forwards a value it cannot stand behind.

    A single malformed provider/descriptor/instance degrades to a
    ``warnings`` entry (skipping only that item); the whole pass only
    reports ``available=False`` when the provider catalog itself could not
    be reached, parsed, or version-validated at all.
    """

    def __init__(
        self,
        base_url: str,
        *,
        credential: AsyncTokenCredential | None = None,
        token_scope: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._credential = credential
        self._token_scope = token_scope
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            timeout=httpx.Timeout(20.0, connect=8.0),
            follow_redirects=False,
        )

    async def _headers(self) -> dict[str, str]:
        if self._credential and self._token_scope:
            token = await self._credential.get_token(self._token_scope)
            return {"Authorization": f"Bearer {token.token}"}
        return {}

    async def discover(self, request: CapabilityDiscoveryRequest) -> CapabilityDiscoveryResult:
        headers = await self._headers()
        try:
            catalog_response = await self._client.get("v1/providers", headers=headers)
            catalog_response.raise_for_status()
            catalog = catalog_response.json()
        except (httpx.HTTPError, ValueError) as exc:
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
        providers_payload = catalog.get("providers") or []
        catalog_warnings = tuple(
            str(warning.get("message") or warning.get("reason_code") or "unknown catalog warning")
            for warning in (catalog.get("warnings") or [])
        )
        provider_ids = [str(entry["provider_id"]) for entry in providers_payload if "provider_id" in entry]
        if not provider_ids:
            return CapabilityDiscoveryResult(available=True, warnings=catalog_warnings)

        outcomes = await asyncio.gather(
            *(self._discover_one_provider(provider_id, headers, request.principal) for provider_id in provider_ids),
            return_exceptions=True,
        )

        descriptors: list[CapabilityDescriptor] = []
        instances: list[CapabilityInstance] = []
        warnings: list[str] = list(catalog_warnings)
        seen_descriptor_ids: set[str] = set()
        seen_instance_ids: set[str] = set()

        for provider_id, outcome in zip(provider_ids, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                warnings.append(f"Provider {provider_id} discovery failed: {outcome}")
                continue
            provider_descriptors, provider_instances, provider_warnings = outcome
            warnings.extend(provider_warnings)
            for descriptor in provider_descriptors:
                if descriptor.id in seen_descriptor_ids:
                    warnings.append(
                        f"Provider {provider_id} descriptor id {descriptor.id!r} collided with an "
                        "already-discovered descriptor; skipped."
                    )
                    continue
                seen_descriptor_ids.add(descriptor.id)
                descriptors.append(descriptor)
            for instance in provider_instances:
                if instance.descriptor_id not in seen_descriptor_ids:
                    warnings.append(
                        f"Provider {provider_id} instance id {instance.id!r} references descriptor "
                        f"{instance.descriptor_id!r} which was not discovered/kept; skipped."
                    )
                    continue
                if instance.id in seen_instance_ids:
                    warnings.append(
                        f"Provider {provider_id} instance id {instance.id!r} collided with an "
                        "already-discovered instance; skipped."
                    )
                    continue
                seen_instance_ids.add(instance.id)
                instances.append(instance)

        return CapabilityDiscoveryResult(
            descriptors=tuple(descriptors),
            instances=tuple(instances),
            warnings=tuple(warnings),
            available=True,
        )

    async def _discover_one_provider(
        self, provider_id: str, headers: dict[str, str], registered_by: str
    ) -> tuple[tuple[CapabilityDescriptor, ...], tuple[CapabilityInstance, ...], tuple[str, ...]]:
        response = await self._client.get(f"v1/providers/{provider_id}/capabilities", headers=headers)
        response.raise_for_status()
        payload = response.json()
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
        if str(payload.get("provider_id")) != provider_id:
            raise CapabilityProviderProtocolError(
                f"Provider {provider_id} capabilities response provider_id mismatch: "
                f"{payload.get('provider_id')!r}."
            )
        warnings: list[str] = [
            str(warning.get("message") or warning.get("reason_code") or "unknown discovery warning")
            for warning in (payload.get("warnings") or [])
        ]
        descriptors: list[CapabilityDescriptor] = []
        for descriptor_payload in payload.get("descriptors") or []:
            try:
                descriptors.append(_map_descriptor(descriptor_payload, provider_id=provider_id))
            except (CapabilityProviderProtocolError, ValidationError, KeyError, ValueError, TypeError) as exc:
                descriptor_id = (
                    descriptor_payload.get("descriptor_id") if isinstance(descriptor_payload, dict) else None
                )
                warnings.append(
                    f"Provider {provider_id} descriptor {descriptor_id!r} could not be translated "
                    f"and was skipped: {exc}"
                )
        instances: list[CapabilityInstance] = []
        for instance_payload in payload.get("instances") or []:
            try:
                instances.append(
                    _map_instance(instance_payload, provider_id=provider_id, registered_by=registered_by)
                )
            except (CapabilityProviderProtocolError, ValidationError, KeyError, ValueError, TypeError) as exc:
                instance_id = instance_payload.get("instance_id") if isinstance(instance_payload, dict) else None
                warnings.append(
                    f"Provider {provider_id} instance {instance_id!r} could not be translated and "
                    f"was skipped: {exc}"
                )
        return tuple(descriptors), tuple(instances), tuple(warnings)

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
    )
