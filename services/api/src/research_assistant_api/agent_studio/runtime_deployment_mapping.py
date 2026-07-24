"""Immutable runtime deployment mapping (``runtime-deployment-mapping:v1``).

A ``RuntimeDeploymentMapping`` is the single, server-authored object the
runtime-control plane (``/internal/v1/runtime/*``) resolves *before* any
runtime request is trusted. It binds one **opaque deployment id** to the exact
tenant/project/environment/logical agent, the exact harness and backend
release identities, the exact provider integration contract + artifact, the
full pinned capability *binding descriptor* (descriptor/operation/instance/
policy refs with their versions and content digests, plus the destination-hash
*policy* a runtime must reproduce), the deployment lifecycle/lineage, and the
**server-side allowlist** of authenticated client/app-id + internal app-role
pairs permitted to load this one deployment.

Design invariants (why this object exists at all):

* **No caller-selected scope or partition.** ``tenant_id``/``project_id``/
  ``environment``/``logical_agent_id`` live *inside* the mapping the server
  stored; a runtime request never supplies them. A runtime only ever presents
  an opaque ``deployment_id`` + the ``mapping_ref``/``mapping_digest`` it was
  issued, and the server loads the mapping from *its* stored partition and
  compares. A request can therefore never widen its own scope by editing a
  field.
* **Runtime identity is app-role + client allowlist, never human project
  membership.** ``allowed_client_app_role_bindings`` is a non-empty,
  server-owned allowlist; a runtime principal is authorized by matching an
  entry here, not by belonging to any human researcher group/project.
* **Deterministic, versioned digest.** ``mapping_digest`` is a pure function
  of every authoritative field via the same canonical (sorted-key,
  compact-separator) JSON + SHA-256 construction the rest of this package
  uses, prefixed with ``runtime-deployment-mapping:v1:sha256:`` so the
  encoding can never silently change meaning. ``mapping_ref`` is the opaque,
  stable reference (``<schema_version>:<deployment_id>``) a runtime echoes
  back; the server matches *both* ref and digest exactly.

This module owns only the immutable contract shape and its digest. Retrieval,
authorization ordering, and the internal router that serves it live in
separate, separately-reviewed modules.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from research_assistant_api.agent_studio.models import (
    CapabilityDescriptorRef,
    CapabilityInstanceRef,
    CapabilityOperationRef,
    CapabilityPolicyRef,
    DeploymentEnvironment,
    utc_now,
)

#: Strict schema/contract version for this object. Runtime consumers resolve
#: the mapping contract by this exact string; it is *not* a free-form label.
RUNTIME_DEPLOYMENT_MAPPING_SCHEMA_VERSION: Literal["runtime-deployment-mapping:v1"] = "runtime-deployment-mapping:v1"

#: Versioned prefix for ``compute_mapping_digest`` output, mirroring
#: ``scope.compute_scope_key``'s ``scope:v1:sha256:`` convention. Bumping this
#: to a new version is the only sanctioned way to change the encoding below --
#: never reuse ``v1`` for a different scheme, since that would silently change
#: every existing mapping digest's meaning.
_MAPPING_DIGEST_PREFIX = "runtime-deployment-mapping:v1:sha256:"

#: Canonical algorithm identifier a runtime must use to compute a
#: ``destination_hash`` for an invocation against this mapping's binding. It
#: names ``scope.compute_destination_hash``'s versioned scheme so a runtime
#: reproduces the identical value; it is never invented ad hoc per caller.
RUNTIME_DESTINATION_HASH_ALGORITHM: Literal["destination:v1:sha256"] = "destination:v1:sha256"


class RuntimeMappingLifecycleState(StrEnum):
    """Lifecycle state of one immutable ``RuntimeDeploymentMapping``.

    ``ACTIVE``: the mapping is the current, servable binding for its
    deployment. ``SUPERSEDED``: a newer mapping has replaced it (its
    ``deployment_id`` is named by the newer mapping's ``supersedes_deployment_id``)
    but it is retained for lineage/audit. ``RETIRED``: the deployment is
    permanently withdrawn and must never be served again.
    """

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETIRED = "retired"


class RuntimeDestinationHashPolicy(BaseModel):
    """The exact destination-hash *policy* a runtime must reproduce.

    A runtime never chooses how to hash the destination it is about to write
    to: it reproduces ``scope.compute_destination_hash`` under this named
    ``algorithm`` for this mapping's exact ``binding_id``/``operation_id``.
    Carrying the policy (not a precomputed hash) in the mapping lets the
    backend verify a runtime-supplied ``destination_hash`` against the one
    algorithm both sides agreed on, rather than trusting an opaque string.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    algorithm: Literal["destination:v1:sha256"] = RUNTIME_DESTINATION_HASH_ALGORITHM
    binding_id: str = Field(min_length=1, max_length=200)
    operation_id: str = Field(min_length=1, max_length=200)


class RuntimeBindingDescriptor(BaseModel):
    """Fully pinned capability binding this deployment is allowed to exercise.

    Mirrors the authoritative pin fields of ``CapabilityBinding`` (typed
    descriptor/operation/instance/policy refs with their versions and content
    digests, plus the resolved destination constraints and their digest) so a
    runtime's request can be matched, field by field, against exactly what the
    server released -- never against a flattened or lossy summary. The
    ``destination_hash_policy`` names the one algorithm a runtime must use to
    hash the destination it targets.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_id: str = Field(min_length=1, max_length=200)
    provider_contract_version: str = Field(min_length=1, max_length=80)
    descriptor_ref: CapabilityDescriptorRef
    operation_ref: CapabilityOperationRef
    instance_ref: CapabilityInstanceRef | None = None
    policy_ref: CapabilityPolicyRef | None = None
    destination_constraints: tuple[str, ...] = Field(default_factory=tuple)
    destination_constraints_digest: str | None = None
    destination_hash_policy: RuntimeDestinationHashPolicy

    @model_validator(mode="after")
    def _binding_id_matches_hash_policy(self) -> RuntimeBindingDescriptor:
        """The destination-hash policy must pin *this* binding/operation.

        A binding descriptor whose ``destination_hash_policy`` named a
        different ``binding_id`` would let a runtime reproduce a hash for the
        wrong destination while still matching the binding, so the two are
        required to agree by construction.
        """
        if self.destination_hash_policy.binding_id != self.binding_id:
            raise ValueError("destination_hash_policy.binding_id must equal binding_id.")
        if self.destination_hash_policy.operation_id != self.operation_ref.id:
            raise ValueError("destination_hash_policy.operation_id must equal operation_ref.id.")
        return self


class AllowedClientAppRoleBinding(BaseModel):
    """One server-authored (authenticated client/app-id, internal app-role) pair
    permitted to load exactly this deployment.

    Runtime authorization binds a validated platform identity to a single
    deployment through this allowlist. ``client_app_id`` is the Entra
    application/client identifier the platform authenticated (never a human
    user id); ``app_role`` is the exact internal application role value the
    principal must carry. Neither is a human project/group membership.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_app_id: str = Field(min_length=1, max_length=200)
    app_role: str = Field(min_length=1, max_length=200)


class RuntimeDeploymentMapping(BaseModel):
    """Immutable, server-authored binding of an opaque deployment to its exact
    scope, releases, provider contract, capability binding, lifecycle, and the
    server-side client/app-role allowlist permitted to load it.

    Never constructed from a runtime request: the server materializes it from
    a released ``AgentVersion``/``DeploymentRecord`` and stores it in the
    ``(tenant_id, project_id)`` partition. A runtime only ever echoes the
    opaque ``deployment_id`` + issued ``mapping_ref``/``mapping_digest``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["runtime-deployment-mapping:v1"] = RUNTIME_DEPLOYMENT_MAPPING_SCHEMA_VERSION
    deployment_id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    project_id: str = Field(min_length=1, max_length=200)
    environment: DeploymentEnvironment
    logical_agent_id: str = Field(min_length=1, max_length=200)

    harness_release_id: str = Field(min_length=1, max_length=500)
    harness_manifest_digest: str = Field(min_length=1, max_length=200)
    backend_release_id: str = Field(min_length=1, max_length=500)
    backend_version: str = Field(min_length=1, max_length=200)
    provider_contract_version: str = Field(min_length=1, max_length=80)
    provider_artifact_digest: str = Field(min_length=1, max_length=200)

    binding: RuntimeBindingDescriptor
    allowed_client_app_role_bindings: tuple[AllowedClientAppRoleBinding, ...]

    lifecycle_state: RuntimeMappingLifecycleState = RuntimeMappingLifecycleState.ACTIVE
    supersedes_deployment_id: str | None = Field(default=None, max_length=200)

    created_at: datetime = Field(default_factory=utc_now)
    created_by: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _allowlist_is_non_empty_and_unique(self) -> RuntimeDeploymentMapping:
        """The server must pin at least one client/app-role binding, and no
        exact (client_app_id, app_role) pair may repeat.

        An empty allowlist would leave a deployment loadable by no runtime
        (dead) or, worse, tempt a caller-side fallback; duplicate pairs make
        the allowlist ambiguous to audit. Both are rejected structurally.
        """
        if not self.allowed_client_app_role_bindings:
            raise ValueError("allowed_client_app_role_bindings must contain at least one entry.")
        seen: set[tuple[str, str]] = set()
        for binding in self.allowed_client_app_role_bindings:
            pair = (binding.client_app_id, binding.app_role)
            if pair in seen:
                raise ValueError(
                    "allowed_client_app_role_bindings must not contain duplicate "
                    "(client_app_id, app_role) pairs."
                )
            seen.add(pair)
        return self

    @model_validator(mode="after")
    def _supersession_is_not_self_referential(self) -> RuntimeDeploymentMapping:
        """A mapping can never declare that it supersedes its own deployment."""
        if self.supersedes_deployment_id is not None and self.supersedes_deployment_id == self.deployment_id:
            raise ValueError("supersedes_deployment_id must not equal deployment_id.")
        return self

    @property
    def mapping_ref(self) -> str:
        """Opaque, stable reference a runtime echoes back in every request.

        Composed of the strict schema version and the opaque deployment id;
        it exposes no scope/partition value and is safe to hand to a runtime.
        """
        return f"{self.schema_version}:{self.deployment_id}"

    @property
    def mapping_digest(self) -> str:
        """Deterministic content digest over every authoritative field."""
        return compute_mapping_digest(self)


def compute_mapping_digest(mapping: RuntimeDeploymentMapping) -> str:
    """Canonical, versioned digest of a ``RuntimeDeploymentMapping``.

    Encodes every authoritative field (the derived ``mapping_ref``/
    ``mapping_digest`` properties are excluded, since they are functions of
    these fields) as a canonical, sorted-key, compact-separator JSON object
    and SHA-256 hashes it -- the same construction
    ``capability_registry._canonical_digest`` uses, so every content digest in
    this package is computed identically. Prefixed with
    ``runtime-deployment-mapping:v1:sha256:`` so the scheme can never silently
    change meaning.
    """

    payload: dict[str, Any] = {
        "schema_version": mapping.schema_version,
        "deployment_id": mapping.deployment_id,
        "tenant_id": mapping.tenant_id,
        "project_id": mapping.project_id,
        "environment": mapping.environment.value,
        "logical_agent_id": mapping.logical_agent_id,
        "harness_release_id": mapping.harness_release_id,
        "harness_manifest_digest": mapping.harness_manifest_digest,
        "backend_release_id": mapping.backend_release_id,
        "backend_version": mapping.backend_version,
        "provider_contract_version": mapping.provider_contract_version,
        "provider_artifact_digest": mapping.provider_artifact_digest,
        "binding": mapping.binding.model_dump(mode="json"),
        "allowed_client_app_role_bindings": [
            entry.model_dump(mode="json") for entry in mapping.allowed_client_app_role_bindings
        ],
        "lifecycle_state": mapping.lifecycle_state.value,
        "supersedes_deployment_id": mapping.supersedes_deployment_id,
        "created_at": mapping.created_at.isoformat(),
        "created_by": mapping.created_by,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{_MAPPING_DIGEST_PREFIX}{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
