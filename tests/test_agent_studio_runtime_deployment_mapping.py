from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from research_assistant_api.agent_studio.models import DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    RUNTIME_DEPLOYMENT_MAPPING_SCHEMA_VERSION,
    RUNTIME_DESTINATION_HASH_ALGORITHM,
    AllowedClientAppRoleBinding,
    RuntimeBindingDescriptor,
    RuntimeDeploymentMapping,
    RuntimeDescriptorRef,
    RuntimeDestinationHashPolicy,
    RuntimeInstanceRef,
    RuntimeMappingLifecycleState,
    RuntimeOperationRef,
    RuntimePolicyRef,
    compute_mapping_digest,
)


def _hash_policy(*, binding_id: str = "binding-1", operation_id: str = "search") -> RuntimeDestinationHashPolicy:
    return RuntimeDestinationHashPolicy(binding_id=binding_id, operation_id=operation_id)


FIXED_CREATED_AT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _binding(
    *,
    binding_id: str = "binding-1",
    operation_id: str = "search",
    hash_policy: RuntimeDestinationHashPolicy | None = None,
) -> RuntimeBindingDescriptor:
    resolved_policy = hash_policy or _hash_policy(binding_id=binding_id, operation_id=operation_id)
    return RuntimeBindingDescriptor(
        binding_id=binding_id,
        provider_contract_version="provider.contract.v7",
        descriptor_ref=RuntimeDescriptorRef(id="foundry.azure_ai_search", version="1", digest="sha256:aa"),
        operation_ref=RuntimeOperationRef(id=operation_id, version="1"),
        instance_ref=RuntimeInstanceRef(provider_id="prov-1", id="inst-1", fingerprint="fp-1"),
        policy_ref=RuntimePolicyRef(id="policy-1", version="1", digest="sha256:bb"),
        destination_constraints=("https://search.example",),
        destination_constraints_digest="sha256:cc",
        destination_hash_policy=resolved_policy,
    )


def _mapping(
    *,
    deployment_id: str = "dep-1",
    allowed: tuple[AllowedClientAppRoleBinding, ...] | None = None,
    supersedes_deployment_id: str | None = None,
    lifecycle_state: RuntimeMappingLifecycleState = RuntimeMappingLifecycleState.ACTIVE,
) -> RuntimeDeploymentMapping:
    return RuntimeDeploymentMapping(
        deployment_id=deployment_id,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        logical_agent_id="agent-1",
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:harness",
        backend_release_id="backend-release-1",
        backend_version="1.2.3",
        provider_contract_version="provider.contract.v7",
        provider_artifact_digest="sha256:provider-artifact",
        binding=_binding(),
        allowed_client_app_role_bindings=(
            allowed
            if allowed is not None
            else (AllowedClientAppRoleBinding(client_app_id="client-app-1", app_role="research-assistant.runtime"),)
        ),
        lifecycle_state=lifecycle_state,
        supersedes_deployment_id=supersedes_deployment_id,
        created_at=FIXED_CREATED_AT,
        created_by="release-service",
    )


# --- valid construction and derived values ---------------------------------


def test_mapping_schema_version_is_strict_constant() -> None:
    mapping = _mapping()
    assert mapping.schema_version == RUNTIME_DEPLOYMENT_MAPPING_SCHEMA_VERSION == "runtime-deployment-mapping:v1"


def test_mapping_ref_is_schema_version_and_opaque_deployment_id() -> None:
    mapping = _mapping(deployment_id="dep-xyz")
    assert mapping.mapping_ref == "runtime-deployment-mapping:v1:dep-xyz"
    # mapping_ref must not leak scope/partition values.
    assert "tenant-1" not in mapping.mapping_ref
    assert "project-1" not in mapping.mapping_ref


def test_mapping_digest_is_versioned_and_stable() -> None:
    mapping = _mapping()
    digest = mapping.mapping_digest
    assert digest.startswith("runtime-deployment-mapping:v1:sha256:")
    assert digest == compute_mapping_digest(mapping)
    # Re-materializing the identical mapping yields the identical digest.
    assert compute_mapping_digest(_mapping()) == digest


def test_mapping_digest_changes_when_any_authoritative_field_changes() -> None:
    base = _mapping().mapping_digest
    assert _mapping(deployment_id="dep-2").mapping_digest != base
    assert (
        _mapping(lifecycle_state=RuntimeMappingLifecycleState.SUPERSEDED).mapping_digest != base
    )
    assert _mapping(supersedes_deployment_id="dep-0").mapping_digest != base
    allowed = (
        AllowedClientAppRoleBinding(client_app_id="client-app-2", app_role="research-assistant.runtime"),
    )
    assert _mapping(allowed=allowed).mapping_digest != base


def test_destination_hash_policy_algorithm_is_pinned_constant() -> None:
    policy = _hash_policy()
    assert policy.algorithm == RUNTIME_DESTINATION_HASH_ALGORITHM == "destination:v1:sha256"


# --- binding descriptor validators -----------------------------------------


def test_binding_descriptor_valid_when_policy_matches() -> None:
    binding = _binding(binding_id="b-9", operation_id="write")
    assert binding.destination_hash_policy.binding_id == "b-9"
    assert binding.destination_hash_policy.operation_id == "write"


def test_binding_descriptor_rejects_hash_policy_binding_id_mismatch() -> None:
    with pytest.raises(ValidationError, match="must equal binding_id"):
        _binding(binding_id="b-1", hash_policy=_hash_policy(binding_id="other", operation_id="search"))


def test_binding_descriptor_rejects_hash_policy_operation_mismatch() -> None:
    with pytest.raises(ValidationError, match="must equal operation_ref"):
        _binding(
            binding_id="b-1",
            operation_id="search",
            hash_policy=_hash_policy(binding_id="b-1", operation_id="write"),
        )


# --- allowlist validators --------------------------------------------------


def test_mapping_rejects_empty_allowlist() -> None:
    with pytest.raises(ValidationError, match="must contain at least one entry"):
        _mapping(allowed=())


def test_mapping_rejects_duplicate_allowlist_pair() -> None:
    dup = AllowedClientAppRoleBinding(client_app_id="client-app-1", app_role="research-assistant.runtime")
    with pytest.raises(ValidationError, match="must not contain duplicate"):
        _mapping(allowed=(dup, dup))


def test_mapping_accepts_distinct_allowlist_entries() -> None:
    allowed = (
        AllowedClientAppRoleBinding(client_app_id="client-app-1", app_role="research-assistant.runtime"),
        AllowedClientAppRoleBinding(client_app_id="client-app-1", app_role="research-assistant.runtime.admin"),
        AllowedClientAppRoleBinding(client_app_id="client-app-2", app_role="research-assistant.runtime"),
    )
    mapping = _mapping(allowed=allowed)
    assert len(mapping.allowed_client_app_role_bindings) == 3


# --- supersession validator ------------------------------------------------


def test_mapping_rejects_self_supersession() -> None:
    with pytest.raises(ValidationError, match="supersedes_deployment_id must not equal deployment_id"):
        _mapping(deployment_id="dep-1", supersedes_deployment_id="dep-1")


def test_mapping_allows_absent_supersession() -> None:
    assert _mapping(supersedes_deployment_id=None).supersedes_deployment_id is None


def test_mapping_allows_distinct_supersession() -> None:
    mapping = _mapping(deployment_id="dep-2", supersedes_deployment_id="dep-1")
    assert mapping.supersedes_deployment_id == "dep-1"


# --- immutability / strictness ---------------------------------------------


def test_mapping_is_frozen_and_forbids_extra_fields() -> None:
    mapping = _mapping()
    with pytest.raises(ValidationError):
        mapping.deployment_id = "mutated"
    with pytest.raises(ValidationError):
        RuntimeDeploymentMapping(**{**mapping.model_dump(), "unexpected": "x"})


def test_lifecycle_states_are_exhaustive() -> None:
    assert {state.value for state in RuntimeMappingLifecycleState} == {"active", "superseded", "retired"}


# --- L1: aware-UTC timestamps ----------------------------------------------


def test_naive_created_at_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must be timezone-aware"):
        RuntimeDeploymentMapping(**{**_mapping().model_dump(), "created_at": datetime(2026, 1, 1, 12, 0, 0)})


def test_aware_non_utc_created_at_is_normalized_to_utc() -> None:
    from datetime import timedelta, timezone

    plus_two = timezone(timedelta(hours=2))
    mapping = RuntimeDeploymentMapping(
        **{**_mapping().model_dump(), "created_at": datetime(2026, 1, 1, 14, 0, 0, tzinfo=plus_two)}
    )
    assert mapping.created_at.utcoffset() == timedelta(0)
    assert mapping.created_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


# --- N1: expiry / revocation / lifecycle authority -------------------------


def test_is_effective_at_true_for_active_unexpired_unrevoked() -> None:
    mapping = _mapping()
    assert mapping.is_effective_at(datetime(2026, 6, 1, tzinfo=UTC)) is True


def test_is_effective_at_false_when_superseded() -> None:
    mapping = _mapping(lifecycle_state=RuntimeMappingLifecycleState.SUPERSEDED)
    assert mapping.is_effective_at(datetime(2026, 6, 1, tzinfo=UTC)) is False


def test_is_effective_at_false_when_revoked() -> None:
    mapping = _mapping().model_copy(update={"revoked_at": datetime(2026, 2, 1, tzinfo=UTC)})
    assert mapping.is_effective_at(datetime(2026, 6, 1, tzinfo=UTC)) is False


def test_is_effective_at_false_after_expiry() -> None:
    mapping = _mapping().model_copy(update={"expires_at": datetime(2026, 3, 1, tzinfo=UTC)})
    assert mapping.is_effective_at(datetime(2026, 3, 1, 0, 0, 1, tzinfo=UTC)) is False
    assert mapping.is_effective_at(datetime(2026, 2, 1, tzinfo=UTC)) is True


def test_expiry_and_revocation_change_the_digest() -> None:
    base = _mapping().mapping_digest
    assert _mapping().model_copy(update={"expires_at": datetime(2026, 3, 1, tzinfo=UTC)}).mapping_digest != base
    assert _mapping().model_copy(update={"revoked_at": datetime(2026, 3, 1, tzinfo=UTC)}).mapping_digest != base


# --- M1: nested refs are frozen (digest cannot drift) ----------------------


def test_nested_binding_refs_are_frozen() -> None:
    mapping = _mapping()
    with pytest.raises(ValidationError):
        mapping.binding.descriptor_ref.id = "tampered"
    with pytest.raises(ValidationError):
        mapping.binding.operation_ref.id = "tampered"


# --- N2: conditional instance pinning --------------------------------------


def _binding_no_instance() -> RuntimeBindingDescriptor:
    return RuntimeBindingDescriptor(
        binding_id="binding-1",
        provider_contract_version="provider.contract.v7",
        descriptor_ref=RuntimeDescriptorRef(id="foundry.azure_ai_search"),
        operation_ref=RuntimeOperationRef(id="search"),
        destination_hash_policy=_hash_policy(),
    )


def test_binding_without_instance_is_allowed() -> None:
    assert _binding_no_instance().instance_ref is None


def test_binding_with_partially_pinned_instance_is_rejected() -> None:
    with pytest.raises(ValidationError, match="must pin"):
        RuntimeBindingDescriptor(
            binding_id="binding-1",
            provider_contract_version="provider.contract.v7",
            descriptor_ref=RuntimeDescriptorRef(id="foundry.azure_ai_search"),
            operation_ref=RuntimeOperationRef(id="search"),
            instance_ref=RuntimeInstanceRef(provider_id="prov-1", id=None, fingerprint="fp-1"),
            destination_hash_policy=_hash_policy(),
        )
