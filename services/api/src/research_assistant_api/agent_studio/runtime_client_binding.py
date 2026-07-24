"""Server-owned client-to-deployment authority for runtime auth.

Authority over *which* deployment an authenticated client may touch is
server-owned and resolved **before** any mapping is read. This closes the
oracle where a role-holding-but-unauthorized caller could point-read arbitrary
deployment partitions and use the in-mapping allowlist as the sole gate.

Ratified design constraints (do not reinvent):

1. **Exact membership, never selection.** One client identity is bound to
   exactly ONE deployment (one-to-one; multi-deployment scenarios require
   separate app registrations). Authorization is an *exact membership test* on
   the pair ``(authenticated_client_app_id, asserted_deployment_id)``: the
   asserted deployment is an INPUT, never a value the resolver returns. There is
   deliberately no ``authorized_deployment_id(client) -> deployment`` path (one
   refactor away from being used as a *source* for ``deployment_id``); the
   resolver only ever answers "is this pair bound, and which revision is
   current?". A caller that omits/mismatches the deployment is denied.
2. **Keyed by the authenticated client alone.** The index is partitioned by and
   point-read by ``client_app_id`` -- never queried by ``deployment_id`` or
   scanned, and the role is NEVER part of the key. Roles come only from the
   validated token; putting a role in the index key would make the index a
   second, divergent source of role authority.
3. **No timing padding across not-bound vs no-such-mapping.** An unbound caller
   must never touch the mapping container at all (that is correct and
   desirable). We deliberately do NOT equalize timing between "not bound" and
   "bound but mapping absent" -- the residual difference only distinguishes
   states inside the caller's own authorized set, which it already knows.
4. **Writes are control-plane only.** The index is the one *mutable* authority
   here, so its write path is the crown jewel. ``ClientDeploymentBindingResolver``
   (read-only ``resolve_binding``) is the ONLY surface the runtime plane is
   given; grants/revocations live on ``ClientDeploymentBindingWriter``, exercised
   only by the human-authorized control-plane deployment/release path. The
   runtime app-role identity must have read-only data-plane access to the index
   (see the IaC RBAC for the durable adapter); it can never write a binding, so
   an attacker who can land a mapping cannot also land a binding.
5. **Fail-closed ordering (no cross-container atomicity).** Binding partition is
   ``client_app_id`` and mapping partition is ``deployment_id``; no Cosmos
   transactional batch spans them. The producer's GRANT writes the mapping
   revision FIRST then the binding (a binding never points at a missing
   revision); REVOKE removes/repoints the binding FIRST then writes the retiring
   revision (authority is withdrawn before the object). A binding-without-mapping
   is a denial, never a repairable state -- the loader returns ``None`` for it.
6. **Access is not usability.** A present binding authorizes *access* only.
   After the binding check and mapping load, the full lifecycle evaluation still
   applies (``lifecycle_fault``: not-yet-effective/expired/revoked/superseded/
   retired all still deny); a binding never short-circuits any lifecycle check
   (enforced in ``runtime_authz``).

This module defines the read-only resolver protocol, the control-plane writer
protocol, the ``BindingResolution`` the resolver returns, an in-memory index
implementing both, the fail-closed loader, and a durable Cosmos adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from pydantic import BaseModel, ConfigDict

from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping
from research_assistant_api.agent_studio.runtime_mapping_store import RuntimeDeploymentMappingStore

#: A loader taking (trusted_client_app_id, asserted_deployment_id) and returning
#: the authorized mapping or ``None`` (uniformly, without leaking why).
AuthorizedMappingLoader = Callable[[str, str], RuntimeDeploymentMapping | None]


class RuntimeBindingStatus(StrEnum):
    """Lifecycle status of a client->deployment binding row.

    ``ACTIVE`` is the only status a live binding carries. ``REVOKED`` is a
    soft-revoked tombstone: a revoked binding is present but denies (the loader
    treats a non-ACTIVE resolution as a denial, without reading the mapping).
    """

    ACTIVE = "active"
    REVOKED = "revoked"


class BindingResolution(BaseModel):
    """The result of an exact ``(client, asserted_deployment)`` membership test.

    Carries WHICH revision of the asserted deployment is current -- both the
    content-addressed ``revision_id`` (a digest pin: repointing to a different
    document changes it) and the monotonic ``revision_sequence`` the control
    plane uses to reject a rollback -- plus the binding ``status``. It never
    carries a deployment the caller did not assert. The store then performs an
    exact ``(deployment_id, revision_id)`` point read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    deployment_id: str
    revision_id: str
    revision_sequence: int
    status: RuntimeBindingStatus


class ClientDeploymentBindingResolver(Protocol):
    """Read-only authority the runtime plane depends on: exact membership only."""

    def resolve_binding(self, client_app_id: str, asserted_deployment_id: str) -> BindingResolution | None:
        """Resolve the binding for the exact ``(client, asserted_deployment)`` pair, else ``None``.

        Returns ``None`` when the client is not bound to *this* asserted
        deployment (unbound, or bound to a different deployment). When the pair
        is bound it returns the current revision + status; it never selects or
        returns a deployment the caller did not assert.
        """
        ...


class ClientDeploymentBindingWriter(Protocol):
    """Control-plane-only mutation surface (never handed to the runtime plane)."""

    def grant(self, client_app_id: str, deployment_id: str, revision_id: str, revision_sequence: int) -> None:
        """Bind ``client_app_id`` to exactly ``deployment_id`` at the given revision (one-to-one; replaces)."""
        ...

    def revoke(self, client_app_id: str, deployment_id: str) -> None:
        """Remove the client's binding iff it currently points at ``deployment_id``."""
        ...


class _StoredBinding:
    """A client's single one-to-one binding record."""

    __slots__ = ("deployment_id", "revision_id", "revision_sequence", "status")

    def __init__(
        self, deployment_id: str, revision_id: str, revision_sequence: int, status: RuntimeBindingStatus
    ) -> None:
        self.deployment_id = deployment_id
        self.revision_id = revision_id
        self.revision_sequence = revision_sequence
        self.status = status


class InMemoryClientDeploymentBindingIndex:
    """In-memory one-to-one client->deployment binding index (tests/local).

    Implements both the read-only resolver and the control-plane writer; the
    runtime is only ever handed the ``resolve_binding`` surface (typed as
    ``ClientDeploymentBindingResolver``), never this concrete writer. A client
    holds exactly one binding; a ``grant`` replaces it.
    """

    def __init__(self) -> None:
        self._by_client: dict[str, _StoredBinding] = {}

    def grant(self, client_app_id: str, deployment_id: str, revision_id: str, revision_sequence: int) -> None:
        self._by_client[client_app_id] = _StoredBinding(
            deployment_id, revision_id, revision_sequence, RuntimeBindingStatus.ACTIVE
        )

    def revoke(self, client_app_id: str, deployment_id: str) -> None:
        binding = self._by_client.get(client_app_id)
        if binding is not None and binding.deployment_id == deployment_id:
            del self._by_client[client_app_id]

    def resolve_binding(self, client_app_id: str, asserted_deployment_id: str) -> BindingResolution | None:
        binding = self._by_client.get(client_app_id)
        if binding is None or binding.deployment_id != asserted_deployment_id:
            return None
        return BindingResolution(
            deployment_id=binding.deployment_id,
            revision_id=binding.revision_id,
            revision_sequence=binding.revision_sequence,
            status=binding.status,
        )


def build_authorized_mapping_loader(
    resolver: ClientDeploymentBindingResolver,
    mapping_store: RuntimeDeploymentMappingStore,
) -> AuthorizedMappingLoader:
    """Compose a read-only binding resolver + mapping store into an authorized loader.

    Authorizes the ``(client, asserted_deployment)`` binding by exact membership
    FIRST; an unbound (or wrong-deployment) caller returns ``None`` immediately
    WITHOUT touching the mapping container (constraint 3 -- zero mapping reads).
    A soft-revoked binding (status != ACTIVE) also denies WITHOUT a mapping read.
    Only an ACTIVE binding point-reads the EXACT current revision the binding
    supplies -- the index supplies WHICH revision, never WHICH deployment. A
    binding pointing at an absent revision also returns ``None`` (fail-closed
    reconciliation, constraint 5). No selection, no default, no enumeration.
    """

    def _load(client_app_id: str, asserted_deployment_id: str) -> RuntimeDeploymentMapping | None:
        resolution = resolver.resolve_binding(client_app_id, asserted_deployment_id)
        if resolution is None:
            return None
        if resolution.status is not RuntimeBindingStatus.ACTIVE:
            return None
        return mapping_store.get(resolution.deployment_id, resolution.revision_id)

    return _load


#: Cosmos ``documentType`` discriminator and partition-key path for the durable
#: client->deployment binding index. The container is partitioned by
#: ``/client_app_id`` and each client has exactly ONE item (id == client_app_id)
#: so a runtime authorization is a single-partition ``read_item`` keyed by the
#: AUTHENTICATED client (constraint 2) -- never a query by ``deployment_id``,
#: never a scan, and the role is never part of the key.
RUNTIME_BINDING_DOCUMENT_TYPE = "runtimeClientDeploymentBindingV1"
RUNTIME_BINDING_PARTITION_KEY_PATH = "/client_app_id"


class CosmosClientDeploymentBindingIndex:
    """Durable one-to-one binding index partitioned by ``/client_app_id``.

    Implements both the read-only resolver and the control-plane writer, but the
    two surfaces are governed by *different* data-plane identities in IaC: the
    runtime app-role identity is granted Cosmos **Data Reader** on this
    container (it only ever calls ``resolve_binding``), while grants/revocations
    run under the control-plane identity with **Data Contributor**. The type
    split here is the code half; the RBAC split is the enforcement half.

    ``resolve_binding`` is a fresh single-partition point ``read_item`` (404 ->
    ``None``); it returns ``None`` unless the stored row's ``deployment_id``
    equals the asserted one, so it can never redirect the caller. ``grant``
    upserts the client's single binding to the current revision (the binding is
    the one mutable authority; a re-grant repoints it). ``revoke`` deletes the
    client's binding only when it currently points at ``deployment_id``; a
    missing/mismatched binding is an idempotent no-op.
    """

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container

    def grant(self, client_app_id: str, deployment_id: str, revision_id: str, revision_sequence: int) -> None:
        self._container.upsert_item(
            {
                "id": client_app_id,
                "documentType": RUNTIME_BINDING_DOCUMENT_TYPE,
                "client_app_id": client_app_id,
                "deployment_id": deployment_id,
                "current_revision_id": revision_id,
                "current_revision_sequence": revision_sequence,
                "status": RuntimeBindingStatus.ACTIVE.value,
            }
        )

    def revoke(self, client_app_id: str, deployment_id: str) -> None:
        document = self._read(client_app_id)
        if document is None or document.get("deployment_id") != deployment_id:
            return
        self._container.delete_item(item=client_app_id, partition_key=client_app_id)

    def resolve_binding(self, client_app_id: str, asserted_deployment_id: str) -> BindingResolution | None:
        document = self._read(client_app_id)
        if document is None or document.get("deployment_id") != asserted_deployment_id:
            return None
        return BindingResolution(
            deployment_id=str(document["deployment_id"]),
            revision_id=str(document["current_revision_id"]),
            revision_sequence=int(str(document["current_revision_sequence"])),
            status=RuntimeBindingStatus(str(document["status"])),
        )

    def _read(self, client_app_id: str) -> dict[str, object] | None:
        try:
            return dict(self._container.read_item(item=client_app_id, partition_key=client_app_id))
        except CosmosResourceNotFoundError:
            return None
