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

from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from research_assistant_core.azure_auth import azure_credential

from research_assistant_api.agent_studio.models import (
    AgentManifest,
    MemoryAuditAction,
    MemoryAuditRecord,
    MemoryEntry,
    MemoryScopeBinding,
    MemoryScopeKind,
    utc_now,
)
from research_assistant_api.agent_studio.scope import compute_scope_key
from research_assistant_api.config import Settings

if TYPE_CHECKING:
    from azure.core.credentials import TokenCredential


#: Non-persistent scopes (``persistent=False``, the default even when
#: ``enabled=True``) never store an entry indefinitely: when neither the
#: entry nor the scope binding declares a retention period, this bounded
#: fallback TTL is applied so "not persistent" is a real, enforced fact
#: rather than an aspiration.
_DEFAULT_NON_PERSISTENT_TTL_DAYS = 1


class MemoryPolicyError(RuntimeError):
    pass


class MemoryAccessError(MemoryPolicyError):
    """Raised when an actor lacks the read/write ACL required for a memory
    inspect/correct/forget/export action on a specific entry."""


def validate_memory_scopes(manifest: AgentManifest, scope_kind: MemoryScopeKind) -> MemoryScopeBinding:
    """Reject access to a memory scope that is disabled, undeclared, or bound
    to a non-GA mechanism; return the resolved binding on success.

    There is no manifest-wide "memory enabled" bit: conversation memory may
    be enabled while user/project/private-agent scopes stay disabled (the
    default), so every check is scoped to exactly the ``scope_kind`` being
    accessed, never a global flag.
    """
    binding = manifest.memory_policy.scope(scope_kind)
    if binding is None or not binding.enabled:
        raise MemoryPolicyError(
            f"Manifest '{manifest.logical_agent_id}' does not have the '{scope_kind.value}' memory scope enabled."
        )
    if not binding.mechanism.is_ga:
        raise MemoryPolicyError(
            f"Memory mechanism '{binding.mechanism.value}' is not GA and cannot be attached; "
            "it is only available as a preview capability."
        )
    return binding


def _is_active(entry: MemoryEntry) -> bool:
    """An entry is recallable/exportable only while not forgotten and not expired."""
    if entry.deleted_at is not None:
        return False
    return entry.expires_at is None or entry.expires_at > utc_now()


def _reject_if_inactive(entry: MemoryEntry, entry_id: str, *, action: str) -> None:
    """Governance access (``inspect``/``correct``) must refuse an entry that
    is forgotten OR expired. An expired-but-not-yet-purged entry is not
    meaningfully different from a deleted one from the caller's perspective,
    so it must not remain readable or correctable purely because a
    retention/TTL sweep has not physically removed it yet."""
    if entry.deleted_at is not None:
        raise MemoryPolicyError(f"Memory entry '{entry_id}' has been forgotten and cannot be {action}.")
    if entry.expires_at is not None and entry.expires_at <= utc_now():
        raise MemoryPolicyError(f"Memory entry '{entry_id}' has expired and cannot be {action}.")


def _can_read(entry: MemoryEntry, actor_id: str) -> bool:
    """Empty ``read_acl`` means "creator + agent context only"; a non-empty
    ACL additionally allows the listed principals."""
    return actor_id == entry.created_by or actor_id in entry.read_acl


def _can_write(entry: MemoryEntry, actor_id: str) -> bool:
    return actor_id == entry.created_by or actor_id in entry.write_acl


class MemoryStore(Protocol):
    def append(self, entry: MemoryEntry) -> MemoryEntry: ...

    def list_entries(
        self,
        *,
        tenant_id: str,
        project_id: str,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        logical_agent_id: str,
        limit: int = 100,
    ) -> tuple[MemoryEntry, ...]: ...

    def get_entry(self, *, tenant_id: str, project_id: str, entry_id: str) -> MemoryEntry | None: ...

    def replace_entry(self, entry: MemoryEntry) -> MemoryEntry: ...

    def record_audit(self, record: MemoryAuditRecord) -> MemoryAuditRecord: ...

    def list_audit(
        self, *, tenant_id: str, project_id: str, logical_agent_id: str, entry_id: str
    ) -> tuple[MemoryAuditRecord, ...]: ...


class InMemoryMemoryStore:
    """Deterministic in-process memory store, used in tests and as the base
    class overridden by the Cosmos-backed implementation for production.
    """

    def __init__(self) -> None:
        self._entries: dict[str, MemoryEntry] = {}
        self._audit: list[MemoryAuditRecord] = []

    def append(self, entry: MemoryEntry) -> MemoryEntry:
        self._entries[entry.id] = entry
        return entry

    def list_entries(
        self,
        *,
        tenant_id: str,
        project_id: str,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        logical_agent_id: str,
        limit: int = 100,
    ) -> tuple[MemoryEntry, ...]:
        matches = [
            entry
            for entry in self._entries.values()
            if entry.tenant_id == tenant_id
            and entry.project_id == project_id
            and entry.scope_kind == scope_kind
            and entry.scope_id == scope_id
            and entry.logical_agent_id == logical_agent_id
        ]
        matches.sort(key=lambda entry: entry.created_at)
        return tuple(matches[-limit:])

    def get_entry(self, *, tenant_id: str, project_id: str, entry_id: str) -> MemoryEntry | None:
        entry = self._entries.get(entry_id)
        if entry is None or entry.tenant_id != tenant_id or entry.project_id != project_id:
            return None
        return entry

    def replace_entry(self, entry: MemoryEntry) -> MemoryEntry:
        self._entries[entry.id] = entry
        return entry

    def record_audit(self, record: MemoryAuditRecord) -> MemoryAuditRecord:
        self._audit.append(record)
        return record

    def list_audit(
        self, *, tenant_id: str, project_id: str, logical_agent_id: str, entry_id: str
    ) -> tuple[MemoryAuditRecord, ...]:
        matches = [
            record
            for record in self._audit
            if record.tenant_id == tenant_id
            and record.project_id == project_id
            and record.logical_agent_id == logical_agent_id
            and record.entry_id == entry_id
        ]
        matches.sort(key=lambda record: record.created_at)
        return tuple(matches)


class MemoryStoreUnavailableError(MemoryPolicyError):
    """Raised when the production memory store factory has no cloud backend
    configured. Never silently falls back to an in-memory store outside
    tests.
    """


class CosmosMemoryStore:
    """Cosmos DB-backed ``MemoryStore``.

    Persists to the dedicated ``agentStudioMemoryV1`` container (per Phase 2
    partitioning), partitioned by ``/scope_key`` — the same synthetic
    tenant+project partition key computed by ``ScopeContext.scope_key`` for
    the metadata store. Every point read/write is scoped to a single
    partition (never ``enable_cross_partition_query=True``): entry and audit
    documents both carry a ``scope_key`` field and a ``documentType``
    discriminator, and every list/query call runs with an explicit
    ``partition_key=`` for exactly one ``(tenant_id, project_id)`` pair.
    """

    def __init__(
        self,
        endpoint: str,
        database_name: str,
        credential: TokenCredential,
        container_name: str = "agentStudioMemoryV1",
    ) -> None:
        from azure.cosmos import CosmosClient  # local import: optional heavy dependency

        client = CosmosClient(endpoint, credential=credential)
        database = client.get_database_client(database_name)
        self._container = database.get_container_client(container_name)

    @staticmethod
    def _entry_document(entry: MemoryEntry) -> dict[str, Any]:
        return {
            "id": entry.id,
            "documentType": "entry",
            "scope_key": compute_scope_key(entry.tenant_id, entry.project_id),
            "tenantId": entry.tenant_id,
            "projectId": entry.project_id,
            "scopeKind": entry.scope_kind.value,
            "scopeId": entry.scope_id,
            "logicalAgentId": entry.logical_agent_id,
            "payload": entry.model_dump(mode="json"),
        }

    def append(self, entry: MemoryEntry) -> MemoryEntry:
        self._container.upsert_item(self._entry_document(entry))
        return entry

    def list_entries(
        self,
        *,
        tenant_id: str,
        project_id: str,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        logical_agent_id: str,
        limit: int = 100,
    ) -> tuple[MemoryEntry, ...]:
        documents = list(
            self._container.query_items(
                query=(
                    "SELECT * FROM c WHERE c.documentType = @documentType "
                    "AND c.scopeKind = @scopeKind AND c.scopeId = @scopeId "
                    "AND c.logicalAgentId = @logicalAgentId"
                ),
                parameters=[
                    {"name": "@documentType", "value": "entry"},
                    {"name": "@scopeKind", "value": scope_kind.value},
                    {"name": "@scopeId", "value": scope_id},
                    {"name": "@logicalAgentId", "value": logical_agent_id},
                ],
                partition_key=compute_scope_key(tenant_id, project_id),
            )
        )
        entries = [MemoryEntry.model_validate(document["payload"]) for document in documents]
        entries.sort(key=lambda entry: entry.created_at)
        return tuple(entries[-limit:])

    def get_entry(self, *, tenant_id: str, project_id: str, entry_id: str) -> MemoryEntry | None:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            document = self._container.read_item(
                item=entry_id, partition_key=compute_scope_key(tenant_id, project_id)
            )
        except CosmosResourceNotFoundError:
            return None
        if document.get("documentType") != "entry":
            return None
        return MemoryEntry.model_validate(document["payload"])

    def replace_entry(self, entry: MemoryEntry) -> MemoryEntry:
        self._container.upsert_item(self._entry_document(entry))
        return entry

    def record_audit(self, record: MemoryAuditRecord) -> MemoryAuditRecord:
        self._container.upsert_item(
            {
                "id": f"audit::{record.id}",
                "documentType": "audit",
                "scope_key": compute_scope_key(record.tenant_id, record.project_id),
                "tenantId": record.tenant_id,
                "projectId": record.project_id,
                "logicalAgentId": record.logical_agent_id,
                "entryId": record.entry_id,
                "payload": record.model_dump(mode="json"),
            }
        )
        return record

    def list_audit(
        self, *, tenant_id: str, project_id: str, logical_agent_id: str, entry_id: str
    ) -> tuple[MemoryAuditRecord, ...]:
        documents = list(
            self._container.query_items(
                query=(
                    "SELECT * FROM c WHERE c.documentType = @documentType "
                    "AND c.logicalAgentId = @logicalAgentId AND c.entryId = @entryId"
                ),
                parameters=[
                    {"name": "@documentType", "value": "audit"},
                    {"name": "@logicalAgentId", "value": logical_agent_id},
                    {"name": "@entryId", "value": entry_id},
                ],
                partition_key=compute_scope_key(tenant_id, project_id),
            )
        )
        records = [MemoryAuditRecord.model_validate(document["payload"]) for document in documents]
        records.sort(key=lambda record: record.created_at)
        return tuple(records)


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
    credential = azure_credential(settings.managed_identity_client_id)
    return CosmosMemoryStore(
        settings.cosmos_endpoint,
        settings.agent_studio_cosmos_database,
        credential,
        settings.agent_studio_memory_container,
    )


class MemoryService:
    """Facade enforcing manifest validation, TTL, and ACL governance before
    any memory access, and recording an audit trail for every governance
    action (remember/recall/inspect/correct/forget/export).
    """

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    def remember(self, manifest: AgentManifest, entry: MemoryEntry) -> MemoryEntry:
        binding = validate_memory_scopes(manifest, entry.scope_kind)
        ttl_days = entry.ttl_days
        if ttl_days is None:
            # A scope that isn't ``persistent`` never stores indefinitely;
            # fall back to the scope's own retention or a conservative
            # bounded default rather than leaving ``expires_at`` unset.
            if binding.persistent:
                ttl_days = binding.retention_days
            else:
                ttl_days = binding.retention_days or _DEFAULT_NON_PERSISTENT_TTL_DAYS
        if ttl_days is not None and entry.expires_at is None:
            entry = entry.model_copy(
                update={"ttl_days": ttl_days, "expires_at": entry.created_at + timedelta(days=ttl_days)}
            )
        stored = self._store.append(entry)
        self._store.record_audit(
            MemoryAuditRecord(
                id=str(uuid4()),
                tenant_id=entry.tenant_id,
                project_id=entry.project_id,
                logical_agent_id=manifest.logical_agent_id,
                entry_id=entry.id,
                action=MemoryAuditAction.REMEMBER,
                actor_id=entry.created_by,
                detail=f"scope={entry.scope_kind.value}",
            )
        )
        return stored

    def recall(
        self,
        manifest: AgentManifest,
        *,
        tenant_id: str,
        project_id: str,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        actor_id: str,
        limit: int = 100,
    ) -> tuple[MemoryEntry, ...]:
        validate_memory_scopes(manifest, scope_kind)
        entries = self._store.list_entries(
            tenant_id=tenant_id,
            project_id=project_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            logical_agent_id=manifest.logical_agent_id,
            limit=limit,
        )
        visible = tuple(entry for entry in entries if _is_active(entry) and _can_read(entry, actor_id))
        self._store.record_audit(
            MemoryAuditRecord(
                id=str(uuid4()),
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=manifest.logical_agent_id,
                entry_id=f"scope:{scope_kind.value}:{scope_id}",
                action=MemoryAuditAction.RECALL,
                actor_id=actor_id,
                detail=f"count={len(visible)}",
            )
        )
        return visible

    def inspect(
        self, manifest: AgentManifest, *, tenant_id: str, project_id: str, entry_id: str, actor_id: str
    ) -> MemoryEntry:
        """Read a single memory entry (governance action, distinct from
        ordinary ``recall``): raises if it does not exist for this agent, has
        been forgotten, the actor lacks read access, or the scope's
        ``allow_user_inspect`` self-service control is off."""
        entry = self._get_owned_entry(manifest, tenant_id=tenant_id, project_id=project_id, entry_id=entry_id)
        binding = validate_memory_scopes(manifest, entry.scope_kind)
        if not binding.allow_user_inspect:
            raise MemoryAccessError(
                f"Scope '{entry.scope_kind.value}' does not allow user-initiated inspect for this agent."
            )
        _reject_if_inactive(entry, entry_id, action="inspected")
        if not _can_read(entry, actor_id):
            raise MemoryAccessError(f"Actor '{actor_id}' does not have read access to memory entry '{entry_id}'.")
        self._store.record_audit(
            MemoryAuditRecord(
                id=str(uuid4()),
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=manifest.logical_agent_id,
                entry_id=entry_id,
                action=MemoryAuditAction.INSPECT,
                actor_id=actor_id,
            )
        )
        return entry

    def correct(
        self,
        manifest: AgentManifest,
        *,
        tenant_id: str,
        project_id: str,
        entry_id: str,
        actor_id: str,
        content: str,
    ) -> MemoryEntry:
        entry = self._get_owned_entry(manifest, tenant_id=tenant_id, project_id=project_id, entry_id=entry_id)
        validate_memory_scopes(manifest, entry.scope_kind)
        _reject_if_inactive(entry, entry_id, action="corrected")
        if not _can_write(entry, actor_id):
            raise MemoryAccessError(f"Actor '{actor_id}' does not have write access to memory entry '{entry_id}'.")
        updated = entry.model_copy(update={"content": content, "provenance": "operator_correction"})
        self._store.replace_entry(updated)
        self._store.record_audit(
            MemoryAuditRecord(
                id=str(uuid4()),
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=manifest.logical_agent_id,
                entry_id=entry_id,
                action=MemoryAuditAction.CORRECT,
                actor_id=actor_id,
                detail="content corrected",
            )
        )
        return updated

    def forget(
        self,
        manifest: AgentManifest,
        *,
        tenant_id: str,
        project_id: str,
        entry_id: str,
        actor_id: str,
        reason: str = "",
    ) -> MemoryEntry:
        entry = self._get_owned_entry(manifest, tenant_id=tenant_id, project_id=project_id, entry_id=entry_id)
        binding = validate_memory_scopes(manifest, entry.scope_kind)
        if not binding.allow_user_forget:
            raise MemoryAccessError(
                f"Scope '{entry.scope_kind.value}' does not allow user-initiated forget for this agent."
            )
        if not _can_write(entry, actor_id):
            raise MemoryAccessError(f"Actor '{actor_id}' does not have write access to memory entry '{entry_id}'.")
        updated = entry.model_copy(update={"deleted_at": utc_now()})
        self._store.replace_entry(updated)
        self._store.record_audit(
            MemoryAuditRecord(
                id=str(uuid4()),
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=manifest.logical_agent_id,
                entry_id=entry_id,
                action=MemoryAuditAction.FORGET,
                actor_id=actor_id,
                detail=reason,
            )
        )
        return updated

    def export(
        self,
        manifest: AgentManifest,
        *,
        tenant_id: str,
        project_id: str,
        scope_kind: MemoryScopeKind,
        scope_id: str,
        actor_id: str,
        limit: int = 1000,
    ) -> tuple[MemoryEntry, ...]:
        binding = validate_memory_scopes(manifest, scope_kind)
        if not binding.allow_user_export:
            raise MemoryAccessError(f"Scope '{scope_kind.value}' does not allow user-initiated export for this agent.")
        entries = self._store.list_entries(
            tenant_id=tenant_id,
            project_id=project_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            logical_agent_id=manifest.logical_agent_id,
            limit=limit,
        )
        visible = tuple(entry for entry in entries if _is_active(entry) and _can_read(entry, actor_id))
        self._store.record_audit(
            MemoryAuditRecord(
                id=str(uuid4()),
                tenant_id=tenant_id,
                project_id=project_id,
                logical_agent_id=manifest.logical_agent_id,
                entry_id=f"scope:{scope_kind.value}:{scope_id}",
                action=MemoryAuditAction.EXPORT,
                actor_id=actor_id,
                detail=f"count={len(visible)}",
            )
        )
        return visible

    def audit_trail(
        self, manifest: AgentManifest, *, tenant_id: str, project_id: str, entry_id: str, actor_id: str
    ) -> tuple[MemoryAuditRecord, ...]:
        """Governance audit history for a single memory entry.

        Requires the enclosing ``manifest`` so the entry can be resolved
        through its owning logical agent (never by ``entry_id`` alone): the
        underlying store filters by ``(tenant_id, project_id,
        logical_agent_id, entry_id)``, so no cross-agent audit history can be
        surfaced even if the caller can guess another agent's entry ID.
        Scope-level aggregate pseudo-IDs (``scope:{kind}:{scope_id}``, used
        internally for ``recall``/``export`` audit records) are rejected here
        rather than resolved, since there is no per-entry ACL to check for
        them and allowing lookups by that pattern would let a caller
        enumerate scope activity without an owned entry to authorize
        against. The caller must also hold read access to the concrete entry
        (creator or ``read_acl`` member) and the scope must permit
        user-initiated inspection, matching ``inspect()``'s visibility rule.
        """
        if entry_id.startswith("scope:"):
            raise MemoryAccessError(
                "Audit trail lookups are only permitted for a concrete memory entry ID, "
                "not an aggregate scope identifier."
            )
        entry = self._get_owned_entry(manifest, tenant_id=tenant_id, project_id=project_id, entry_id=entry_id)
        binding = validate_memory_scopes(manifest, entry.scope_kind)
        if not binding.allow_user_inspect:
            raise MemoryAccessError(
                f"Scope '{entry.scope_kind.value}' does not allow user-initiated audit inspection for this agent."
            )
        if not _can_read(entry, actor_id):
            raise MemoryAccessError(f"Actor '{actor_id}' does not have read access to memory entry '{entry_id}'.")
        return self._store.list_audit(
            tenant_id=tenant_id,
            project_id=project_id,
            logical_agent_id=manifest.logical_agent_id,
            entry_id=entry_id,
        )

    def _get_owned_entry(
        self, manifest: AgentManifest, *, tenant_id: str, project_id: str, entry_id: str
    ) -> MemoryEntry:
        entry = self._store.get_entry(tenant_id=tenant_id, project_id=project_id, entry_id=entry_id)
        if entry is None or entry.logical_agent_id != manifest.logical_agent_id:
            raise MemoryPolicyError(f"Memory entry '{entry_id}' not found for agent '{manifest.logical_agent_id}'.")
        return entry

