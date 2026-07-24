"""Server-owned client-to-deployment authority for runtime auth.

Authority over *which* deployment an authenticated client may touch is
server-owned and resolved **before** any mapping is read. This closes the
oracle where a role-holding-but-unauthorized caller could point-read arbitrary
deployment partitions and use the in-mapping allowlist as the sole gate.

Ratified design constraints (do not reinvent):

1. **Exact membership, never selection.** The index may hold multiple
   ``(client_app_id -> deployment_id)`` bindings per client. Authorization is an
   *exact membership test* on the pair ``(authenticated_client_app_id,
   asserted_deployment_id)``. There is deliberately no "look up this client's
   deployment" path and no implicit default: a caller that omits/mismatches the
   deployment is denied, never resolved to some chosen deployment.
2. **Keyed by the authenticated client.** The index is partitioned by
   ``client_app_id`` and point-read by it -- never queried by ``deployment_id``
   or scanned, so it is not itself an enumeration surface.
3. **No timing padding across not-bound vs no-such-mapping.** An unbound caller
   must never touch the mapping container at all (that is correct and
   desirable). We deliberately do NOT equalize timing between "not bound" and
   "bound but mapping absent" -- the residual difference only distinguishes
   states inside the caller's own authorized set, which it already knows.
4. **Writes are control-plane only.** The index is the one *mutable* authority
   here, so its write path is the crown jewel. ``ClientDeploymentBindingResolver``
   (read-only ``is_bound``) is the ONLY surface the runtime plane is given;
   grants/revocations live on ``ClientDeploymentBindingWriter``, exercised only
   by the human-authorized control-plane deployment/release path. The runtime
   app-role identity must have read-only data-plane access to the index (see the
   IaC RBAC for the durable adapter); it can never write a binding, so an
   attacker who can land a mapping cannot also land a binding.
5. **Fail-closed ordering (no cross-container atomicity).** Binding partition is
   ``client_app_id`` and mapping partition is ``deployment_id``; no Cosmos
   transactional batch spans them. ``grant_access`` writes the mapping FIRST then
   the binding (a binding never points at a missing mapping); ``revoke_access``
   removes the binding FIRST then retires the mapping (authority is withdrawn
   before the object). A binding-without-mapping is a denial, never a repairable
   state -- the loader already returns ``None`` for it.
6. **Access is not usability.** A present binding authorizes *access* only.
   After the binding check and mapping load, the full lifecycle evaluation still
   applies (``is_effective_at``: expired/revoked/superseded/retired all still
   deny); a binding never short-circuits any lifecycle check (enforced in
   ``runtime_authz``).

This module defines the read-only resolver protocol, the control-plane writer
protocol, an in-memory index implementing both, and the fail-closed loader. A
durable Cosmos adapter (binding container partitioned by ``client_app_id``,
runtime read-only, control-plane write, ordered grant/revoke) is a separately
reviewed adapter of these protocols.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping
from research_assistant_api.agent_studio.runtime_mapping_store import RuntimeDeploymentMappingStore

#: A loader taking (trusted_client_app_id, asserted_deployment_id) and returning
#: the authorized mapping or ``None`` (uniformly, without leaking why).
AuthorizedMappingLoader = Callable[[str, str], RuntimeDeploymentMapping | None]


class ClientDeploymentBindingResolver(Protocol):
    """Read-only authority the runtime plane depends on: exact membership only."""

    def is_bound(self, client_app_id: str, deployment_id: str) -> bool:
        """True iff ``client_app_id`` is server-bound to exactly ``deployment_id``."""
        ...


class ClientDeploymentBindingWriter(Protocol):
    """Control-plane-only mutation surface (never handed to the runtime plane)."""

    def grant(self, client_app_id: str, deployment_id: str) -> None:
        """Bind ``client_app_id`` to ``deployment_id`` (mapping must already exist)."""
        ...

    def revoke(self, client_app_id: str, deployment_id: str) -> None:
        """Remove the ``(client_app_id, deployment_id)`` binding."""
        ...


class InMemoryClientDeploymentBindingIndex:
    """In-memory client->deployment binding index (tests/local).

    Implements both the read-only resolver and the control-plane writer; the
    runtime is only ever handed the ``is_bound`` surface (typed as
    ``ClientDeploymentBindingResolver``), never this concrete writer. A client
    may hold multiple bindings; authorization is exact membership.
    """

    def __init__(self) -> None:
        self._bindings: dict[str, set[str]] = {}

    def grant(self, client_app_id: str, deployment_id: str) -> None:
        self._bindings.setdefault(client_app_id, set()).add(deployment_id)

    def revoke(self, client_app_id: str, deployment_id: str) -> None:
        deployments = self._bindings.get(client_app_id)
        if deployments is not None:
            deployments.discard(deployment_id)
            if not deployments:
                del self._bindings[client_app_id]

    def is_bound(self, client_app_id: str, deployment_id: str) -> bool:
        return deployment_id in self._bindings.get(client_app_id, frozenset())


def build_authorized_mapping_loader(
    resolver: ClientDeploymentBindingResolver,
    mapping_store: RuntimeDeploymentMappingStore,
) -> AuthorizedMappingLoader:
    """Compose a read-only binding resolver + mapping store into an authorized loader.

    Authorizes the ``(client, deployment)`` binding by exact membership FIRST; an
    unbound caller returns ``None`` immediately WITHOUT touching the mapping
    container (constraint 3). Only a bound caller point-reads the mapping; a
    binding pointing at an absent mapping also returns ``None`` (fail-closed
    reconciliation, constraint 5). No selection, no default, no enumeration.
    """

    def _load(client_app_id: str, asserted_deployment_id: str) -> RuntimeDeploymentMapping | None:
        if not resolver.is_bound(client_app_id, asserted_deployment_id):
            return None
        return mapping_store.get(asserted_deployment_id)

    return _load
