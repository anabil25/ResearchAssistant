"""Server-owned client-to-deployment authority for runtime auth.

A runtime request must never be able to reach *arbitrary* deployment mappings
by asserting a ``deployment_id`` and relying on an in-mapping allowlist checked
*after* the point-read: that would let any valid runtime-role client enumerate
and time-probe deployment ids. Authority over *which* deployment an
authenticated client may touch is therefore server-owned and resolved **before**
the mapping is ever loaded.

``ClientDeploymentBindingResolver`` maps an authenticated ``client_app_id`` to
the single deployment it is bound to. ``build_authorized_mapping_loader``
composes it with a mapping store into a loader that takes the *trusted* client
id + the *asserted* deployment id and returns the mapping **only** when the
client is bound to exactly that deployment (constant-time compared). For any
other input -- client not bound, bound to a different deployment, or no such
mapping -- it returns ``None`` uniformly, so ``runtime_authz`` renders one
indistinguishable denial and no enumeration/timing oracle exists.

The binding index is the authoritative record: revoking or re-binding a client
(on deployment supersession/revocation) makes every old mapping reference for
that client fail, independent of whether the old mapping document still exists.
A durable Cosmos-backed resolver (index updated atomically with mapping
lifecycle, or reconciliation-safe) is a separately-reviewed adapter of the same
protocol.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable
from typing import Protocol

from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping
from research_assistant_api.agent_studio.runtime_mapping_store import RuntimeDeploymentMappingStore

#: A loader taking (trusted_client_app_id, asserted_deployment_id) and returning
#: the authorized mapping or ``None`` (uniformly, without leaking why).
AuthorizedMappingLoader = Callable[[str, str], RuntimeDeploymentMapping | None]


class ClientDeploymentBindingResolver(Protocol):
    """Server-owned authority: the single deployment a client may load."""

    def authorized_deployment_id(self, client_app_id: str) -> str | None:
        """Return the one deployment ``client_app_id`` is bound to, or ``None``."""
        ...


class InMemoryClientDeploymentBindingResolver:
    """In-memory client->deployment binding index (tests/local).

    Each client is bound to exactly one deployment. ``revoke``/re-``bind`` model
    supersession/revocation: after them, an old deployment reference for that
    client no longer resolves.
    """

    def __init__(self) -> None:
        self._by_client: dict[str, str] = {}

    def bind(self, client_app_id: str, deployment_id: str) -> None:
        self._by_client[client_app_id] = deployment_id

    def revoke(self, client_app_id: str) -> None:
        self._by_client.pop(client_app_id, None)

    def authorized_deployment_id(self, client_app_id: str) -> str | None:
        return self._by_client.get(client_app_id)


def build_authorized_mapping_loader(
    resolver: ClientDeploymentBindingResolver,
    mapping_store: RuntimeDeploymentMappingStore,
) -> AuthorizedMappingLoader:
    """Compose a binding resolver + mapping store into an authorized loader.

    The returned loader authorizes the client->deployment binding **first**
    (constant-time) and only then point-reads the mapping; any failure yields a
    uniform ``None`` with no distinction between "not bound", "bound elsewhere",
    and "no such mapping".
    """

    def _load(client_app_id: str, asserted_deployment_id: str) -> RuntimeDeploymentMapping | None:
        authorized = resolver.authorized_deployment_id(client_app_id)
        if authorized is None or not hmac.compare_digest(authorized, asserted_deployment_id):
            return None
        return mapping_store.get(asserted_deployment_id)

    return _load
