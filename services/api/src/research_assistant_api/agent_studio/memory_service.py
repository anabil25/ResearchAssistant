"""Application-owned (GA) conversation/user/project/private-agent memory.

Only GA memory mechanisms are ever attached by this service. The Microsoft
Foundry native "Memory" feature is documented as **preview** (see
``capability_registry.py``) and is surfaced only as a preview capability
descriptor operation — it is never wired into ``MemoryService`` and never
attached by ``AgentManifest.memory_policy`` validation below.

Persistent memory is off by default: ``MemoryPolicy.enabled`` must be
explicitly set ``True`` on the manifest before any ``remember``/``recall``
call is permitted, even if ``scopes`` are declared.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from research_assistant_api.agent_studio.models import (
    AgentManifest,
    MemoryEntry,
    MemoryScopeKind,
)
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


class MemoryPolicyError(RuntimeError):
    pass


def validate_memory_scopes(manifest: AgentManifest) -> None:
    """Reject any manifest that attempts to bind a non-GA memory mechanism
    or that has not explicitly opted into persistent memory.
    """
    if not manifest.memory_policy.enabled:
        raise MemoryPolicyError(
            f"Manifest '{manifest.logical_agent_id}' has persistent memory disabled "
            "(MemoryPolicy.enabled=False by default); enable it explicitly to use memory."
        )
    for binding in manifest.memory_policy.scopes:
        if not binding.mechanism.is_ga:
            raise MemoryPolicyError(
                f"Memory mechanism '{binding.mechanism.value}' is not GA and cannot be attached; "
                "it is only available as a preview capability."
            )


class MemoryStore(Protocol):
    def append(self, entry: MemoryEntry) -> MemoryEntry: ...

    def list_entries(
        self,
        *,
        tenant_id: str,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        logical_agent_id: str,
        limit: int = 100,
    ) -> tuple[MemoryEntry, ...]: ...


class InMemoryMemoryStore:
    """Deterministic in-process memory store, used in tests and as the base
    class overridden by the Cosmos-backed implementation for production.
    """

    def __init__(self) -> None:
        self._entries: list[MemoryEntry] = []

    def append(self, entry: MemoryEntry) -> MemoryEntry:
        self._entries.append(entry)
        return entry

    def list_entries(
        self,
        *,
        tenant_id: str,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        logical_agent_id: str,
        limit: int = 100,
    ) -> tuple[MemoryEntry, ...]:
        matches = [
            entry
            for entry in self._entries
            if entry.tenant_id == tenant_id
            and entry.scope_kind == scope_kind
            and entry.scope_id == scope_id
            and entry.logical_agent_id == logical_agent_id
        ]
        matches.sort(key=lambda entry: entry.created_at)
        return tuple(matches[-limit:])


class MemoryStoreUnavailableError(MemoryPolicyError):
    """Raised when the production memory store factory has no cloud backend
    configured. Never silently falls back to an in-memory store outside
    tests.
    """


class CosmosMemoryStore:
    """Cosmos DB-backed ``MemoryStore``.

    Persists to a dedicated ``memory`` container in the Agent Studio Cosmos
    database (separate from the ``manifests``/``versions``/``governance``
    containers used by ``CosmosAgentStudioStore``) so memory volume/growth
    does not affect metadata query performance. Entries are immutable once
    appended (memory is append-only, never rewritten).
    """

    def __init__(self, endpoint: str, database_name: str, credential: TokenCredential) -> None:
        from azure.cosmos import CosmosClient  # local import: optional heavy dependency

        client = CosmosClient(endpoint, credential=credential)
        database = client.get_database_client(database_name)
        self._container = database.get_container_client("memory")

    def append(self, entry: MemoryEntry) -> MemoryEntry:
        self._container.upsert_item(
            {
                "id": entry.id,
                "tenantId": entry.tenant_id,
                "scopeKind": entry.scope_kind.value,
                "scopeId": entry.scope_id,
                "logicalAgentId": entry.logical_agent_id,
                "payload": entry.model_dump(mode="json"),
            }
        )
        return entry

    def list_entries(
        self,
        *,
        tenant_id: str,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        logical_agent_id: str,
        limit: int = 100,
    ) -> tuple[MemoryEntry, ...]:
        documents = list(
            self._container.query_items(
                query=(
                    "SELECT * FROM c WHERE c.tenantId = @tenantId AND c.scopeKind = @scopeKind "
                    "AND c.scopeId = @scopeId AND c.logicalAgentId = @logicalAgentId"
                ),
                parameters=[
                    {"name": "@tenantId", "value": tenant_id},
                    {"name": "@scopeKind", "value": scope_kind.value},
                    {"name": "@scopeId", "value": scope_id},
                    {"name": "@logicalAgentId", "value": logical_agent_id},
                ],
                enable_cross_partition_query=True,
            )
        )
        entries = [MemoryEntry.model_validate(document["payload"]) for document in documents]
        entries.sort(key=lambda entry: entry.created_at)
        return tuple(entries[-limit:])


def build_memory_store(settings: Settings) -> MemoryStore:
    """Production factory.

    Returns a Cosmos-backed store when ``cosmos_endpoint`` is configured.
    When it is not configured, memory persistence is explicitly unavailable
    in production: callers must not silently fall back to
    ``InMemoryMemoryStore`` outside of tests.
    """
    if not settings.cosmos_endpoint:
        raise MemoryStoreUnavailableError(
            "No Azure Cosmos DB endpoint is configured; Agent Studio memory persistence is unavailable."
        )
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential

    credential = (
        ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
        if settings.managed_identity_client_id
        else DefaultAzureCredential()
    )
    return CosmosMemoryStore(settings.cosmos_endpoint, settings.agent_studio_cosmos_database, credential)


class MemoryService:
    """Facade enforcing manifest validation before any memory access."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def remember(self, manifest: AgentManifest, entry: MemoryEntry) -> MemoryEntry:
        validate_memory_scopes(manifest)
        matching_scope = next(
            (scope for scope in manifest.memory_policy.scopes if scope.kind == entry.scope_kind),
            None,
        )
        if matching_scope is None:
            raise MemoryPolicyError(
                f"Manifest '{manifest.logical_agent_id}' does not declare a '{entry.scope_kind.value}' memory scope."
            )
        return self._store.append(entry)

    def recall(
        self,
        manifest: AgentManifest,
        *,
        tenant_id: str,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        limit: int = 100,
    ) -> tuple[MemoryEntry, ...]:
        validate_memory_scopes(manifest)
        return self._store.list_entries(
            tenant_id=tenant_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            logical_agent_id=manifest.logical_agent_id,
            limit=limit,
        )
