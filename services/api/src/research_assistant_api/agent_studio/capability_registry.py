"""Capability catalog and attach-time maturity enforcement.

The registry is populated only from scope-aware provider discovery. No local
catalog is mixed into provider results or used when discovery is unavailable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from research_assistant_api.agent_studio.capability_discovery import (
    CapabilityDiscoveryRequest,
    CapabilityDiscoverySource,
    discover_with_timeout,
)
from research_assistant_api.agent_studio.models import (
    CapabilityBinding,
    CapabilityBindingView,
    CapabilityConfigurationRef,
    CapabilityConnectionRef,
    CapabilityDescriptor,
    CapabilityDescriptorRef,
    CapabilityInstance,
    CapabilityInstanceRef,
    CapabilityOperation,
    CapabilityOperationRef,
    CapabilityPolicyRef,
    OperationClass,
    OperationLifecycle,
    OperationMaturity,
    utc_now,
)

_LEARN_TOOL_CATALOG_URL = (
    "https://learn.microsoft.com/azure/ai-foundry/agents/how-to/tools/tool-catalog"
)

#: The provider integration *contract* generation this backend's
#: ``CapabilityBinding``s are validated against. Until a real external
#: provider adapter is wired (see ``capability_discovery.CapabilityDiscoverySource``)
#: and reports its own negotiated contract version, every binding is honestly
#: pinned to this backend-local contract identifier — never a copied/fabricated
#: external "v2"/"v3" provider contract version this backend has not actually
#: integrated against.
LOCAL_PROVIDER_CONTRACT_VERSION = "agent-studio.capability-registry.v1"


class CapabilityAttachmentError(ValueError):
    """Raised when an attempted capability attachment is not GA-eligible."""


def _canonical_digest(payload: Any) -> str:
    """Canonical ``sha256:``-prefixed digest of a JSON-serializable payload.

    Same convention used by ``schema_ref_resolver.compute_schema_digest``
    (sorted-key, compact-separator canonical JSON) so every content digest
    in this package is computed identically.
    """

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def compute_descriptor_digest(descriptor: CapabilityDescriptor) -> str:
    """Content digest of a ``CapabilityDescriptor``, pinned by attaching bindings.

    Pins the descriptor's *content*, not just its ``version`` string, so a
    catalog edit that changes semantics without bumping the version cannot
    silently change an already-attached binding's behavior.
    """

    return _canonical_digest(descriptor.model_dump(mode="json"))


def compute_config_hash(config: dict[str, Any]) -> str:
    """Canonical digest of a binding's non-secret ``config`` dict."""

    return _canonical_digest(config)


def _connection_ref(connection_ref: str | None) -> CapabilityConnectionRef | None:
    """Wrap an attach-time raw connection id into a typed ``CapabilityConnectionRef``.

    ``auth_mode``/``authorization_digest`` are honestly left ``None`` here:
    this registry has no workspace-connection resolution service wired in to
    verify them, so it never fabricates a value it cannot back with evidence.
    """

    if connection_ref is None:
        return None
    return CapabilityConnectionRef(id=connection_ref)


def _policy_ref(policy_ref: str | None) -> CapabilityPolicyRef | None:
    """Wrap an attach-time raw policy id into a typed ``CapabilityPolicyRef``.

    ``version``/``digest`` are honestly left ``None`` until a real approval
    policy registry is wired in to resolve and verify them.
    """

    if policy_ref is None:
        return None
    return CapabilityPolicyRef(id=policy_ref)


def compute_instance_fingerprint(
    descriptor: CapabilityDescriptor, instance: CapabilityInstance
) -> str:
    """Canonical digest pinning a discovered ``CapabilityInstance``.

    Pins provider/descriptor/operation identity, operation definitions and
    versions, side-effect destinations, tenant/data boundaries, and
    non-secret discovered configuration. Deliberately excludes
    health/timestamps/secrets/credential material so a readiness flap alone
    never invalidates a pinned binding — only a genuine reconfiguration
    (different descriptor content, different operations, different
    instance/tenant/project/connection identity, or different discovered
    config) changes this value.
    """

    payload = {
        "provider": descriptor.provider,
        "descriptor_id": descriptor.id,
        "descriptor_version": descriptor.version,
        "descriptor_digest": compute_descriptor_digest(descriptor),
        "operations": sorted(
            (
                {
                    "name": op.name,
                    "version": op.version,
                    "maturity": op.maturity.value,
                    "lifecycle": op.lifecycle.value,
                    "operation_class": op.operation_class.value,
                    "input_schema_digest": op.input_schema_digest,
                    "output_schema_digest": op.output_schema_digest,
                    "side_effect_destinations": sorted(op.side_effect_destinations),
                }
                for op in descriptor.operations
            ),
            key=lambda entry: str(entry["name"]),
        ),
        "instance_id": instance.id,
        "tenant_id": instance.tenant_id,
        "project_id": instance.project_id,
        "discovered_provider_version": instance.discovered_provider_version,
        "config_fingerprint": instance.config_fingerprint,
    }
    return _canonical_digest(payload)


def _ga(
    name: str,
    *,
    version: str = "1",
    operation_class: OperationClass = OperationClass.READ,
    side_effect_destinations: tuple[str, ...] = (),
    requires_approval: bool = False,
    approval_policy_ref: str | None = None,
    source_url: str = _LEARN_TOOL_CATALOG_URL,
    source_version: str | None = None,
    last_verified_at: datetime | None = None,
) -> CapabilityOperation:
    return CapabilityOperation(
        name=name,
        version=version,
        maturity=OperationMaturity.GA,
        operation_class=operation_class,
        side_effect_destinations=side_effect_destinations,
        requires_approval=requires_approval,
        approval_policy_ref=approval_policy_ref,
        source_url=source_url,
        source_version=source_version,
        last_verified_at=last_verified_at,
    )


def _preview(
    name: str,
    reason: str,
    *,
    version: str = "1",
    operation_class: OperationClass = OperationClass.READ,
    side_effect_destinations: tuple[str, ...] = (),
    requires_approval: bool = False,
    approval_policy_ref: str | None = None,
    source_url: str = _LEARN_TOOL_CATALOG_URL,
    source_version: str | None = None,
    last_verified_at: datetime | None = None,
) -> CapabilityOperation:
    return CapabilityOperation(
        name=name,
        version=version,
        maturity=OperationMaturity.PREVIEW,
        operation_class=operation_class,
        side_effect_destinations=side_effect_destinations,
        requires_approval=requires_approval,
        approval_policy_ref=approval_policy_ref,
        reason=reason,
        source_url=source_url,
        source_version=source_version,
        last_verified_at=last_verified_at,
    )


def _retired(
    name: str,
    reason: str,
    *,
    version: str = "1",
    maturity: OperationMaturity = OperationMaturity.GA,
    operation_class: OperationClass = OperationClass.PRIVILEGED,
    source_url: str = _LEARN_TOOL_CATALOG_URL,
    source_version: str | None = None,
    last_verified_at: datetime | None = None,
) -> CapabilityOperation:
    """An operation the provider has documented as retired/removed.

    ``maturity`` defaults to ``GA`` — a retired operation typically *was* GA
    before withdrawal, and its maturity claim does not change on retirement.
    ``lifecycle=RETIRED`` is what actually fails it closed:
    ``CapabilityOperation.is_catalog_eligible`` requires both ``GA`` maturity
    **and** ``ACTIVE`` lifecycle, so a retired operation is never attachable
    regardless of its (possibly still-``GA``) maturity value.
    """
    return CapabilityOperation(
        name=name,
        version=version,
        maturity=maturity,
        lifecycle=OperationLifecycle.RETIRED,
        operation_class=operation_class,
        reason=reason,
        source_url=source_url,
        source_version=source_version,
        last_verified_at=last_verified_at,
    )


def _unknown(
    name: str,
    *,
    version: str = "1",
    operation_class: OperationClass = OperationClass.PRIVILEGED,
    reason: str = "Maturity has not yet been verified against official provenance.",
) -> CapabilityOperation:
    """An operation whose maturity has not been verified (or does not apply).

    ``unknown`` is deliberately fail-closed and non-attachable until
    provenance (``source_url``/``source_version``/``last_verified_at``) is
    recorded and the maturity is re-classified as ``ga``/``preview``. Also
    used for operations that are structurally inapplicable in a given
    runtime (e.g. custom hosted code under Managed Foundry) via the
    ``reason`` override, since ``OperationMaturity`` has no separate
    "unavailable" tier — an inapplicable operation is, honestly, one whose
    GA-maturity has not (and will never be) confirmed here.
    """
    return CapabilityOperation(
        name=name,
        version=version,
        maturity=OperationMaturity.UNKNOWN,
        operation_class=operation_class,
        reason=reason,
    )


_PREVIEW_REASON = "Documented as preview in the Foundry Agent Service tool catalog."


class CapabilityRegistry:
    """In-memory capability catalog with GA-only attach enforcement.

    Also holds the *discovered* ``CapabilityInstance`` set (tenant/project
    resources such as a specific Azure AI Search index connection). Instances
    remain governance-adjacent catalog data — they are provider-discovered
    facts about what a tenant/project actually has available, not part of any
    agent's manifest — so they live alongside the descriptor catalog rather
    than in a separate store.
    """

    def __init__(
        self,
        descriptors: tuple[CapabilityDescriptor, ...] | None = None,
        *,
        available: bool = True,
        unavailable_reason: str | None = None,
    ) -> None:
        """Construct a registry directly from ``descriptors`` (default: empty).

        A bare ``CapabilityRegistry()`` is an honest, empty catalog.

        ``available``/``unavailable_reason`` mirror
        ``capability_discovery.CapabilityDiscoveryResult``'s honest-empty
        vs explicitly-unavailable distinction so a registry built with no
        configured source (see ``default_registry``) can say so, rather
        than looking identical to a registry whose source genuinely
        reported zero capabilities.
        """
        if not available and not unavailable_reason:
            raise ValueError("An unavailable registry must carry a non-empty unavailable_reason")
        if available and unavailable_reason is not None:
            raise ValueError("An available registry must not carry an unavailable_reason")
        seed = descriptors if descriptors is not None else ()
        self._descriptors: dict[str, CapabilityDescriptor] = {descriptor.id: descriptor for descriptor in seed}
        self._instances: dict[str, CapabilityInstance] = {}
        self._warnings: tuple[str, ...] = ()
        self._refreshed_at: datetime = utc_now()
        self._available = available
        self._unavailable_reason = unavailable_reason

    @classmethod
    async def from_source(
        cls, source: CapabilityDiscoverySource, request: CapabilityDiscoveryRequest
    ) -> CapabilityRegistry:
        """Build a registry entirely from a scope-aware ``CapabilityDiscoverySource``.

        No local seed catalog is mixed in: the injected source is treated
        as the authoritative, real discovery output (or an honestly
        unavailable/empty one), never merged with or silently overridden by
        hard-coded data. The call is bounded by ``request.timeout_seconds``
        (see ``capability_discovery.discover_with_timeout``) so a hung or
        cancelled provider degrades to an honest ``available=False``
        registry rather than hanging or raising.

        Every returned instance is validated against ``request.scope``:
        an instance whose ``tenant_id``/``project_id`` does not match the
        requested scope is a **cross-scope rejection** — it is dropped and
        recorded as a warning rather than trusted, because a source must
        never be able to leak another tenant/project's discovered
        instances into this scope's registry. Discovered instances that do
        match are registered immediately so they resolve via
        ``get_instance``/``instances_for`` without a separate wiring step.
        Discovery ``warnings`` are preserved (surfaced via ``/capabilities/
        discovery`` for admins/operators) and ``refreshed_at`` records when
        this discovery pass ran.
        """
        result = await discover_with_timeout(source, request)
        registry = cls(
            descriptors=result.descriptors,
            available=result.available,
            unavailable_reason=result.unavailable_reason,
        )
        warnings = list(result.warnings)
        for instance in result.instances:
            if instance.tenant_id != request.scope.tenant_id or instance.project_id != request.scope.project_id:
                warnings.append(
                    f"Discovery source returned instance '{instance.id}' scoped to tenant "
                    f"'{instance.tenant_id}'/project '{instance.project_id}', which does not match the "
                    f"requested scope ('{request.scope.tenant_id}'/'{request.scope.project_id}'); rejected."
                )
                continue
            registry.register_instance(instance)
        registry._warnings = tuple(warnings)
        registry._refreshed_at = utc_now()
        return registry

    @property
    def available(self) -> bool:
        """Whether this registry's catalog reflects a usable discovery source.

        ``False`` means no capability discovery provider is configured/
        reachable right now (see ``default_registry``/
        ``NullCapabilityDiscoverySource``) — distinct from ``True`` with an
        empty ``catalog()``, which means discovery ran and honestly found
        nothing.
        """
        return self._available

    @property
    def unavailable_reason(self) -> str | None:
        """Honest reason ``available`` is ``False``; ``None`` when available."""
        return self._unavailable_reason

    @property
    def warnings(self) -> tuple[str, ...]:
        """Honest, non-fatal discovery caveats from the last discovery pass."""
        return self._warnings

    @property
    def refreshed_at(self) -> datetime:
        """When this registry's catalog/instances were last (re)discovered."""
        return self._refreshed_at

    def catalog(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._descriptors.values())

    def get(self, descriptor_id: str) -> CapabilityDescriptor | None:
        return self._descriptors.get(descriptor_id)

    def as_mapping(self) -> dict[str, CapabilityDescriptor]:
        return dict(self._descriptors)

    def register_instance(self, instance: CapabilityInstance) -> CapabilityInstance:
        """Register (or replace) a discovered capability instance.

        Registration is dynamic-discovery-shaped (callers supply the
        discovered facts; nothing here fabricates readiness/health), but
        held in-memory for now — consistent with the rest of the registry,
        which is itself an in-memory catalog seeded at process start.

        The instance must reference a known ``descriptor_id`` — a
        fingerprint cannot honestly pin a descriptor this registry does not
        have, so an unknown reference fails closed rather than registering
        an un-pinnable instance. ``instance_fingerprint`` is always
        (re)computed here from the current descriptor + instance facts,
        never trusted verbatim from the caller, so registration cannot be
        used to smuggle in a stale or fabricated pin.
        """
        descriptor = self._descriptors.get(instance.descriptor_id)
        if descriptor is None:
            raise CapabilityAttachmentError(
                f"Capability instance references unknown descriptor '{instance.descriptor_id}'."
            )
        stamped = instance.model_copy(
            update={
                "instance_fingerprint": compute_instance_fingerprint(descriptor, instance),
                "descriptor_digest": compute_descriptor_digest(descriptor),
            }
        )
        self._instances[stamped.id] = stamped
        return stamped

    def get_instance(self, instance_id: str) -> CapabilityInstance | None:
        return self._instances.get(instance_id)

    def instances_for(self, *, tenant_id: str, project_id: str | None = None) -> tuple[CapabilityInstance, ...]:
        return tuple(
            instance
            for instance in self._instances.values()
            if instance.tenant_id == tenant_id and (project_id is None or instance.project_id == project_id)
        )

    def validate_attachment(
        self,
        *,
        descriptor_id: str,
        operation: str,
    ) -> CapabilityOperation:
        """Validate that ``operation`` on ``descriptor_id`` is GA+ACTIVE-attachable.

        Returns the resolved ``CapabilityOperation`` on success; raises
        ``CapabilityAttachmentError`` with an honest reason otherwise.
        Bindability requires ``CapabilityOperation.is_catalog_eligible``
        (both ``OperationMaturity.GA`` and ``OperationLifecycle.ACTIVE`` — a
        GA operation that has been deprecated/retired is rejected the same
        as a non-GA one). When the operation ``requires_approval``, its
        declared ``approval_policy_ref`` must be present — an operation
        flagged as approval-gated with no governing policy reference is an
        unsatisfiable, catalog-authoring inconsistency and must never be
        silently treated as attachable.
        """
        descriptor = self._descriptors.get(descriptor_id)
        if descriptor is None:
            raise CapabilityAttachmentError(f"Capability '{descriptor_id}' is not in the catalog.")
        resolved = descriptor.operation(operation)
        if resolved is None:
            raise CapabilityAttachmentError(f"Capability '{descriptor_id}' has no operation '{operation}'.")
        if not resolved.is_catalog_eligible:
            if resolved.maturity != OperationMaturity.GA:
                default_reason = f"Operation '{operation}' is {resolved.maturity.value}."
            else:
                default_reason = (
                    f"Operation '{operation}' is GA maturity but {resolved.lifecycle.value} lifecycle "
                    "(not active)."
                )
            reason = resolved.reason or default_reason
            raise CapabilityAttachmentError(
                f"Cannot attach '{descriptor_id}.{operation}': {reason}"
            )
        if resolved.requires_approval and resolved.approval_policy_ref is None:
            raise CapabilityAttachmentError(
                f"Cannot attach '{descriptor_id}.{operation}': it requires approval but declares no "
                "approval_policy_ref, so the approval requirement is unsatisfiable."
            )
        return resolved

    def attach(
        self,
        *,
        descriptor_id: str,
        operation: str,
        attached_by: str,
        instance_id: str | None = None,
        connection_ref: str | None = None,
        policy_ref: str | None = None,
        config: dict[str, object] | None = None,
    ) -> CapabilityBinding:
        """Validate and construct a ``CapabilityBinding`` for a GA operation.

        When ``instance_id`` is supplied it must resolve to a registered
        ``CapabilityInstance`` for the same ``descriptor_id``; the binding
        pins the instance's ``discovered_provider_version`` so a later
        instance re-discovery never silently changes an already-attached
        binding's behavior. When the resolved operation ``requires_approval``,
        a ``policy_ref`` must be supplied — attach-time satisfiability of an
        approval-gated operation means the caller has identified *how*
        approval will be sought, not that it has already been granted (that
        is enforced later, at the APPROVAL release gate and again at
        deploy time).
        """
        resolved = self.validate_attachment(descriptor_id=descriptor_id, operation=operation)
        if resolved.requires_approval and policy_ref is None:
            raise CapabilityAttachmentError(
                f"Cannot attach '{descriptor_id}.{operation}': it requires approval, so a policy_ref "
                "identifying the governing approval policy must be supplied."
            )
        descriptor = self._descriptors[descriptor_id]
        pinned_provider_version: str | None = None
        instance_fingerprint: str | None = None
        if instance_id is not None:
            instance = self._instances.get(instance_id)
            if instance is None:
                raise CapabilityAttachmentError(f"Capability instance '{instance_id}' is not registered.")
            if instance.descriptor_id != descriptor_id:
                raise CapabilityAttachmentError(
                    f"Capability instance '{instance_id}' belongs to descriptor "
                    f"'{instance.descriptor_id}', not '{descriptor_id}'."
                )
            if not instance.is_bindable:
                raise CapabilityAttachmentError(
                    f"Capability instance '{instance_id}' is not ready to attach "
                    f"({instance.readiness.value}): {instance.unavailable_reason or 'no reason supplied'}."
                )
            pinned_provider_version = instance.discovered_provider_version
            instance_fingerprint = instance.instance_fingerprint or compute_instance_fingerprint(
                descriptor, instance
            )
        resolved_config = dict(config or {})
        instance_ref: CapabilityInstanceRef | None = None
        if instance_id is not None:
            instance_ref = CapabilityInstanceRef(
                provider_id=descriptor.provider,
                id=instance_id,
                discovered_version=pinned_provider_version,
                fingerprint=instance_fingerprint,
            )
        return CapabilityBinding(
            provider_contract_version=LOCAL_PROVIDER_CONTRACT_VERSION,
            descriptor_ref=CapabilityDescriptorRef(
                id=descriptor_id,
                version=descriptor.version,
                digest=compute_descriptor_digest(descriptor),
            ),
            operation_ref=CapabilityOperationRef(
                id=operation,
                version=resolved.version,
                input_schema_digest=resolved.input_schema_digest,
                output_schema_digest=resolved.output_schema_digest,
            ),
            instance_ref=instance_ref,
            configuration_ref=CapabilityConfigurationRef(digest=compute_config_hash(resolved_config)),
            config=resolved_config,
            connection_ref=_connection_ref(connection_ref),
            policy_ref=_policy_ref(policy_ref),
            destination_constraints=resolved.side_effect_destinations,
            destination_constraints_digest=_canonical_digest(sorted(resolved.side_effect_destinations)),
            attached_by=attached_by,
        )

    def check_binding_freshness(self, binding: CapabilityBinding) -> str | None:
        """Return a stale-binding reason, or ``None`` if the binding is fresh.

        Re-resolves the binding's ``descriptor_ref.id``/``instance_ref.id``
        against the *current* registry state and compares digests/
        fingerprints. A release/invoke path must call this and hard-fail on
        a non-``None`` result — a binding whose pinned descriptor/instance no
        longer matches the live catalog must never be silently honored.

        A missing ``descriptor_ref.digest``/``operation_ref.version``/
        ``instance_ref.fingerprint`` (when an instance is attached) is
        itself a failure, not something to skip: only ``CapabilityRegistry
        .attach`` produces a fully-pinned binding, so an unpinned binding
        reaching this check can only be a hand-constructed/client-submitted
        one that never went through attach-time validation. Treating a
        missing pin as "nothing to compare, so trivially fresh" would let
        such a binding coast through cut/gate/deploy unchecked forever.
        """
        descriptor_id = binding.descriptor_ref.id
        operation_id = binding.operation_ref.id
        descriptor = self._descriptors.get(descriptor_id)
        if descriptor is None:
            return f"Descriptor '{descriptor_id}' is no longer in the catalog."
        current_descriptor_digest = compute_descriptor_digest(descriptor)
        if binding.descriptor_ref.digest is None:
            return (
                f"Capability binding for '{descriptor_id}.{operation_id}' has no descriptor_ref.digest "
                "pinned — an unpinned descriptor digest cannot be verified as fresh and is rejected."
            )
        if current_descriptor_digest != binding.descriptor_ref.digest:
            return (
                f"Descriptor '{descriptor_id}' content has changed since attach "
                "(descriptor_ref.digest mismatch)."
            )
        current_operation = descriptor.operation(operation_id)
        if current_operation is None:
            return f"Operation '{operation_id}' no longer exists on descriptor '{descriptor_id}'."
        if not current_operation.is_catalog_eligible:
            return (
                f"Operation '{descriptor_id}.{operation_id}' is no longer bindable "
                f"({current_operation.maturity.value} maturity / {current_operation.lifecycle.value} "
                "lifecycle) — rebind and re-review before release/invoke."
            )
        if binding.operation_ref.version is None:
            return (
                f"Capability binding for '{descriptor_id}.{operation_id}' has no operation_ref.version "
                "pinned — an unpinned operation version cannot be verified as fresh and is rejected."
            )
        if current_operation.version != binding.operation_ref.version:
            return (
                f"Operation '{descriptor_id}.{operation_id}' version has changed since attach "
                f"(operation_ref.version mismatch: pinned '{binding.operation_ref.version}', now "
                f"'{current_operation.version}') — rebind and re-review before release/invoke."
            )
        if (
            binding.destination_constraints
            and tuple(current_operation.side_effect_destinations) != tuple(binding.destination_constraints)
        ):
            return (
                f"Operation '{descriptor_id}.{operation_id}' side-effect destinations have "
                "changed since attach (destination_constraints mismatch) — rebind and re-review before "
                "release/invoke."
            )
        if binding.instance_ref is not None and binding.instance_ref.id is not None:
            instance_id = binding.instance_ref.id
            instance = self._instances.get(instance_id)
            if instance is None:
                return f"Capability instance '{instance_id}' is no longer registered."
            if not instance.is_bindable:
                return (
                    f"Capability instance '{instance_id}' is no longer ready ({instance.readiness.value}): "
                    f"{instance.unavailable_reason or 'no reason supplied'} — rebind and re-review before "
                    "release/invoke."
                )
            if binding.instance_ref.fingerprint is None:
                return (
                    f"Capability binding for '{descriptor_id}.{operation_id}' attaches instance "
                    f"'{instance_id}' with no instance_ref.fingerprint pinned — an unpinned instance "
                    "fingerprint cannot be verified as fresh and is rejected."
                )
            current_fingerprint = instance.instance_fingerprint or compute_instance_fingerprint(
                descriptor, instance
            )
            if current_fingerprint != binding.instance_ref.fingerprint:
                return (
                    f"Capability instance '{instance_id}' has been reconfigured since attach "
                    "(instance_ref.fingerprint mismatch) — rebind and re-review before release/invoke."
                )
        return None

    def resolve_binding_view(self, binding: CapabilityBinding) -> CapabilityBindingView:
        """Compute a volatile, current-state expansion of ``binding``.

        Re-resolves the pinned ``descriptor_ref``/``instance_ref`` against
        the *current* registry state (never the stale attach-time snapshot)
        and reuses :meth:`check_binding_freshness` for the single source of
        staleness truth, so this view can never disagree with the hard gate.
        The returned view is read-only presentation data: the underlying
        ``binding`` is echoed unchanged, and callers must never write this
        view's ``resolved_descriptor``/``resolved_instance``/``bindable``
        back into persisted state.
        """
        descriptor = self._descriptors.get(binding.descriptor_ref.id)
        instance = (
            self._instances.get(binding.instance_ref.id)
            if binding.instance_ref is not None and binding.instance_ref.id is not None
            else None
        )
        stale_reason = self.check_binding_freshness(binding)
        return CapabilityBindingView(
            binding=binding,
            resolved_descriptor=descriptor,
            resolved_instance=instance,
            bindable=stale_reason is None,
            stale_reason=stale_reason,
            resolved_at=utc_now(),
        )

    def resolve_binding_views(
        self, bindings: Sequence[CapabilityBinding]
    ) -> tuple[CapabilityBindingView, ...]:
        """Batch form of :meth:`resolve_binding_view`, preserving order."""
        return tuple(self.resolve_binding_view(binding) for binding in bindings)


def default_registry() -> CapabilityRegistry:
    """Build the process-default capability registry with no source configured.

    Returns an honest, empty, explicitly ``unavailable`` registry. Production
    composition that has not yet wired a real ``CapabilityDiscoverySource``
    adapter should use this directly, or call ``build_registry_from_source`` with a
    ``NullCapabilityDiscoverySource``; both surface the same explicit
    "provider integration unavailable" signal rather than an
    indistinguishable empty success.
    """
    return CapabilityRegistry(
        available=False,
        unavailable_reason=(
            "No capability discovery provider is configured for this deployment; provider "
            "integration is unavailable."
        ),
    )


async def build_registry_from_source(
    source: CapabilityDiscoverySource, request: CapabilityDiscoveryRequest
) -> CapabilityRegistry:
    """Build a registry from a real, scope-aware ``CapabilityDiscoverySource``.

    Thin, explicitly-named wrapper over ``CapabilityRegistry.from_source``
    for call sites (route handlers, app composition) that want a
    module-level function rather than the classmethod. This is the call
    production composition should use once a real provider adapter exists;
    until then, ``default_registry()`` (or this function called with
    ``NullCapabilityDiscoverySource``) is the honest default.
    """
    return await CapabilityRegistry.from_source(source, request)
