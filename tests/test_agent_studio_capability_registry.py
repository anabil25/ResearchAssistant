# mypy: disable-error-code=import-untyped

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from research_assistant_api.agent_studio.capability_discovery import (
    CapabilityDiscoveryResult,
    InMemoryCapabilityDiscoverySource,
    NullCapabilityDiscoverySource,
)
from research_assistant_api.agent_studio.capability_registry import (
    CapabilityAttachmentError,
    CapabilityRegistry,
    _retired,
    _unknown,
    compute_config_hash,
    compute_descriptor_digest,
    compute_instance_fingerprint,
    default_registry,
)
from research_assistant_api.agent_studio.models import (
    CapabilityBinding,
    CapabilityDescriptor,
    CapabilityDescriptorRef,
    CapabilityInstance,
    CapabilityInstanceRef,
    CapabilityOperation,
    CapabilityOperationRef,
    InstanceReadiness,
    OperationClass,
    OperationLifecycle,
    OperationMaturity,
)


def _descriptor(
    descriptor_id: str,
    *operations: CapabilityOperation,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=descriptor_id,
        provider="custom",
        title=descriptor_id,
        description=f"Descriptor {descriptor_id}",
        operations=operations,
    )


def _instance(
    *,
    instance_id: str,
    descriptor_id: str,
    tenant_id: str = "tenant-1",
    project_id: str = "project-1",
    readiness: InstanceReadiness = InstanceReadiness.READY,
    version: str | None = "2026.07",
    unavailable_reason: str | None = None,
) -> CapabilityInstance:
    return CapabilityInstance(
        id=instance_id,
        tenant_id=tenant_id,
        project_id=project_id,
        descriptor_id=descriptor_id,
        discovered_provider_version=version,
        readiness=readiness,
        unavailable_reason=unavailable_reason,
        registered_by="system",
    )


def test_default_registry_seeds_known_capabilities_and_operation_metadata() -> None:
    registry = default_registry()

    ids = {descriptor.id for descriptor in registry.catalog()}
    assert "foundry.web_search" in ids
    assert "foundry.memory" in ids
    assert "custom.hosted_code" in ids
    assert registry.get("missing.capability") is None

    mapping = registry.as_mapping()
    assert mapping["foundry.web_search"].id == "foundry.web_search"

    search = registry.get("foundry.web_search")
    assert search is not None
    search_op = search.operation("search")
    assert search_op is not None
    assert search_op.maturity is OperationMaturity.GA
    assert search_op.operation_class is OperationClass.READ
    assert search_op.side_effect_destinations == ("public_web",)
    assert search_op.requires_approval is False
    assert search_op.source_url is not None

    functions = registry.get("foundry.azure_functions")
    assert functions is not None
    invoke_op = functions.operation("invoke")
    assert invoke_op is not None
    assert invoke_op.operation_class is OperationClass.WRITE_IRREVERSIBLE
    assert invoke_op.requires_approval is True

    function_calling = registry.get("foundry.function_calling")
    assert function_calling is not None
    fc_op = function_calling.operation("invoke")
    assert fc_op is not None
    assert fc_op.operation_class is OperationClass.PURE


def test_validate_attachment_accepts_ga_operation() -> None:
    resolved = default_registry().validate_attachment(
        descriptor_id="foundry.web_search",
        operation="search",
    )
    assert resolved.maturity is OperationMaturity.GA


@pytest.mark.parametrize(
    ("descriptor_id", "operation", "message"),
    [
        ("unknown.capability", "search", "is not in the catalog"),
        ("foundry.web_search", "unknown_op", "has no operation 'unknown_op'"),
    ],
)
def test_validate_attachment_rejects_unknown_descriptor_or_operation(
    descriptor_id: str,
    operation: str,
    message: str,
) -> None:
    with pytest.raises(CapabilityAttachmentError, match=message):
        default_registry().validate_attachment(descriptor_id=descriptor_id, operation=operation)


def test_validate_attachment_fails_closed_for_preview_retired_unknown_and_deprecated() -> None:
    verified_at = datetime(2026, 7, 23, tzinfo=UTC)
    registry = CapabilityRegistry(
        (
            _descriptor(
                "custom.preview",
                CapabilityOperation(
                    name="run",
                    maturity=OperationMaturity.PREVIEW,
                    reason="Preview only.",
                    source_url="https://example.test/preview",
                    source_version="2026-07",
                    last_verified_at=verified_at,
                ),
            ),
            _descriptor(
                "custom.retired",
                _retired(
                    "run",
                    "Removed by the provider.",
                    source_version="2026-07",
                    last_verified_at=verified_at,
                ),
            ),
            _descriptor("custom.unknown", _unknown("run")),
            _descriptor(
                "custom.deprecated",
                CapabilityOperation(
                    name="run",
                    maturity=OperationMaturity.GA,
                    lifecycle=OperationLifecycle.DEPRECATED,
                ),
            ),
        )
    )

    with pytest.raises(CapabilityAttachmentError, match=re.escape("Preview only.")):
        registry.validate_attachment(descriptor_id="custom.preview", operation="run")
    with pytest.raises(CapabilityAttachmentError, match=re.escape("Removed by the provider.")):
        registry.validate_attachment(descriptor_id="custom.retired", operation="run")
    with pytest.raises(CapabilityAttachmentError, match="Maturity has not yet been verified"):
        registry.validate_attachment(descriptor_id="custom.unknown", operation="run")
    with pytest.raises(
        CapabilityAttachmentError,
        match=re.escape("is GA maturity but deprecated lifecycle (not active)"),
    ):
        registry.validate_attachment(descriptor_id="custom.deprecated", operation="run")


def test_default_registry_uses_seed_when_no_source_supplied() -> None:
    registry = default_registry()

    assert registry.get("foundry.web_search") is not None


def test_default_registry_builds_entirely_from_source_when_supplied() -> None:
    custom_descriptor = _descriptor(
        "custom.only",
        CapabilityOperation(name="run", maturity=OperationMaturity.GA),
    )
    instance = _instance(instance_id="instance-a", descriptor_id="custom.only")
    source = InMemoryCapabilityDiscoverySource(
        CapabilityDiscoveryResult(descriptors=(custom_descriptor,), instances=(instance,))
    )

    registry = default_registry(source=source)

    # Only the source's descriptor is present — the local seed is not mixed in.
    assert registry.catalog() == (custom_descriptor,)
    assert registry.get("foundry.web_search") is None
    stored = registry.get_instance("instance-a")
    assert stored is not None
    assert stored.model_copy(update={"instance_fingerprint": None, "descriptor_digest": None}) == instance
    assert stored.instance_fingerprint is not None
    assert stored.descriptor_digest is not None


def test_from_source_builds_registry_and_registers_instances() -> None:
    custom_descriptor = _descriptor(
        "custom.only",
        CapabilityOperation(name="run", maturity=OperationMaturity.GA),
    )
    instance = _instance(instance_id="instance-a", descriptor_id="custom.only")
    source = InMemoryCapabilityDiscoverySource(
        CapabilityDiscoveryResult(descriptors=(custom_descriptor,), instances=(instance,))
    )

    registry = CapabilityRegistry.from_source(source)

    assert registry.catalog() == (custom_descriptor,)
    stored = registry.get_instance("instance-a")
    assert stored is not None
    assert stored.model_copy(update={"instance_fingerprint": None, "descriptor_digest": None}) == instance
    assert stored.instance_fingerprint is not None
    assert stored.descriptor_digest is not None


def test_from_source_with_null_source_yields_empty_registry() -> None:
    registry = CapabilityRegistry.from_source(NullCapabilityDiscoverySource())

    assert registry.catalog() == ()
    assert registry.get("foundry.web_search") is None


def test_custom_registry_can_replace_seed_catalog() -> None:
    custom_descriptor = _descriptor(
        "custom.only",
        CapabilityOperation(name="run", maturity=OperationMaturity.GA),
    )
    registry = CapabilityRegistry((custom_descriptor,))

    assert registry.catalog() == (custom_descriptor,)
    assert registry.get("foundry.web_search") is None


def test_register_instance_lookup_and_filtering_by_tenant_and_project() -> None:
    registry = CapabilityRegistry(
        (
            _descriptor(
                "custom.search",
                CapabilityOperation(name="search", maturity=OperationMaturity.GA),
            ),
        )
    )
    instance_a = registry.register_instance(_instance(instance_id="instance-a", descriptor_id="custom.search"))
    instance_b = registry.register_instance(
        _instance(
            instance_id="instance-b",
            descriptor_id="custom.search",
            project_id="project-2",
        )
    )
    registry.register_instance(
        _instance(
            instance_id="instance-c",
            descriptor_id="custom.search",
            tenant_id="tenant-2",
        )
    )

    assert registry.get_instance("instance-a") == instance_a
    assert registry.get_instance("missing") is None
    assert registry.instances_for(tenant_id="tenant-1") == (instance_a, instance_b)
    assert registry.instances_for(tenant_id="tenant-1", project_id="project-1") == (instance_a,)


def test_attach_returns_binding_with_instance_pin_and_copied_config() -> None:
    registry = default_registry()
    instance = registry.register_instance(
        _instance(
            instance_id="search-1",
            descriptor_id="foundry.azure_ai_search",
            version="2026.07",
        )
    )
    config: dict[str, object] = {"index": "docs"}

    binding = registry.attach(
        descriptor_id="foundry.azure_ai_search",
        operation="search",
        attached_by="user-1",
        instance_id=instance.id,
        connection_ref="conn-1",
        policy_ref="policy://search",
        config=config,
    )
    config["index"] = "mutated"

    assert binding.descriptor_ref.id == "foundry.azure_ai_search"
    assert binding.descriptor_ref.version == "1"
    assert binding.operation_ref.id == "search"
    assert binding.instance_ref is not None
    assert binding.instance_ref.id == "search-1"
    assert binding.instance_ref.discovered_version == "2026.07"
    assert binding.connection_ref is not None
    assert binding.connection_ref.id == "conn-1"
    assert binding.policy_ref is not None
    assert binding.policy_ref.id == "policy://search"
    assert binding.config == {"index": "docs"}


def test_attach_without_instance_defaults_to_empty_config_and_no_pin() -> None:
    binding = default_registry().attach(
        descriptor_id="foundry.web_search",
        operation="search",
        attached_by="user-1",
    )

    assert binding.instance_ref is None
    assert binding.config == {}
    assert binding.connection_ref is None
    assert binding.policy_ref is None


def test_attach_rejects_missing_instance_wrong_descriptor_and_unavailable_instance() -> None:
    registry = default_registry()

    with pytest.raises(CapabilityAttachmentError, match="is not registered"):
        registry.attach(
            descriptor_id="foundry.azure_ai_search",
            operation="search",
            attached_by="user-1",
            instance_id="missing-instance",
        )

    wrong_descriptor = registry.register_instance(
        _instance(instance_id="wrong-descriptor", descriptor_id="foundry.file_search")
    )
    with pytest.raises(
        CapabilityAttachmentError,
        match=re.escape("belongs to descriptor 'foundry.file_search'"),
    ):
        registry.attach(
            descriptor_id="foundry.azure_ai_search",
            operation="search",
            attached_by="user-1",
            instance_id=wrong_descriptor.id,
        )

    unavailable = registry.register_instance(
        _instance(
            instance_id="unavailable",
            descriptor_id="foundry.azure_ai_search",
            readiness=InstanceReadiness.UNAVAILABLE,
            unavailable_reason="index provisioning failed",
        )
    )
    with pytest.raises(CapabilityAttachmentError, match="index provisioning failed"):
        registry.attach(
            descriptor_id="foundry.azure_ai_search",
            operation="search",
            attached_by="user-1",
            instance_id=unavailable.id,
        )


def test_validate_attachment_rejects_requires_approval_operation_without_policy_ref() -> None:
    """An operation flagged ``requires_approval`` with no ``approval_policy_ref``
    declared in the catalog is an unsatisfiable authoring inconsistency."""
    registry = CapabilityRegistry(
        descriptors=(
            CapabilityDescriptor(
                id="custom.unsatisfiable",
                provider="custom_hosted",
                title="Unsatisfiable approval",
                description="An operation that requires approval but has no policy ref.",
                operations=(
                    CapabilityOperation(
                        name="run",
                        maturity=OperationMaturity.GA,
                        operation_class=OperationClass.WRITE_IRREVERSIBLE,
                        requires_approval=True,
                    ),
                ),
            ),
        ),
    )

    with pytest.raises(
        CapabilityAttachmentError,
        match="approval_policy_ref, so the approval requirement is unsatisfiable",
    ):
        registry.validate_attachment(descriptor_id="custom.unsatisfiable", operation="run")


def test_attach_rejects_requires_approval_operation_with_no_policy_ref_supplied() -> None:
    """Even when the catalog declares an ``approval_policy_ref``, the caller
    attaching the operation must still supply a ``policy_ref`` identifying how
    approval will be sought for this specific attachment."""
    with pytest.raises(
        CapabilityAttachmentError,
        match="so a policy_ref identifying the governing approval policy must be supplied",
    ):
        default_registry().attach(
            descriptor_id="foundry.azure_functions",
            operation="invoke",
            attached_by="user-1",
            connection_ref="conn-azure-functions",
        )


def test_register_instance_rejects_unknown_descriptor() -> None:
    """An instance cannot honestly pin a descriptor the registry doesn't have."""
    registry = CapabilityRegistry(descriptors=())

    with pytest.raises(CapabilityAttachmentError, match="unknown descriptor"):
        registry.register_instance(_instance(instance_id="orphan", descriptor_id="nowhere.search"))


def test_attach_stamps_digests_and_fingerprint_on_binding() -> None:
    registry = default_registry()
    descriptor = registry.get("foundry.azure_ai_search")
    assert descriptor is not None
    instance = registry.register_instance(
        _instance(instance_id="search-fp", descriptor_id="foundry.azure_ai_search", version="2026.07")
    )

    binding = registry.attach(
        descriptor_id="foundry.azure_ai_search",
        operation="search",
        attached_by="user-1",
        instance_id=instance.id,
        config={"index": "docs"},
    )

    assert binding.descriptor_ref.digest == compute_descriptor_digest(descriptor)
    assert binding.instance_ref is not None
    assert binding.instance_ref.fingerprint == instance.instance_fingerprint
    assert binding.configuration_ref.digest == compute_config_hash({"index": "docs"})
    operation = descriptor.operation("search")
    assert operation is not None
    assert binding.operation_ref.input_schema_digest == operation.input_schema_digest
    assert binding.operation_ref.output_schema_digest == operation.output_schema_digest


def test_compute_instance_fingerprint_is_deterministic_and_sensitive_to_content() -> None:
    descriptor = _descriptor("custom.fp", CapabilityOperation(name="run", maturity=OperationMaturity.GA))
    instance = _instance(instance_id="fp-1", descriptor_id="custom.fp")

    fingerprint_a = compute_instance_fingerprint(descriptor, instance)
    fingerprint_b = compute_instance_fingerprint(descriptor, instance)
    assert fingerprint_a == fingerprint_b
    assert fingerprint_a.startswith("sha256:")

    changed_instance = instance.model_copy(update={"discovered_provider_version": "2099.01"})
    assert compute_instance_fingerprint(descriptor, changed_instance) != fingerprint_a


def test_check_binding_freshness_fresh_binding_returns_none() -> None:
    registry = default_registry()
    instance = registry.register_instance(
        _instance(instance_id="fresh-1", descriptor_id="foundry.azure_ai_search", version="2026.07")
    )
    binding = registry.attach(
        descriptor_id="foundry.azure_ai_search",
        operation="search",
        attached_by="user-1",
        instance_id=instance.id,
    )

    assert registry.check_binding_freshness(binding) is None


def test_check_binding_freshness_no_instance_pin_is_fresh_when_descriptor_matches() -> None:
    registry = default_registry()
    binding = registry.attach(
        descriptor_id="foundry.web_search", operation="search", attached_by="user-1"
    )

    assert registry.check_binding_freshness(binding) is None


def test_check_binding_freshness_detects_unknown_descriptor() -> None:
    registry = default_registry()
    binding = registry.attach(descriptor_id="foundry.web_search", operation="search", attached_by="user-1")
    stale_registry = CapabilityRegistry(descriptors=())

    reason = stale_registry.check_binding_freshness(binding)
    assert reason is not None
    assert "no longer in the catalog" in reason


def test_check_binding_freshness_detects_descriptor_digest_drift() -> None:
    registry = default_registry()
    binding = registry.attach(descriptor_id="foundry.web_search", operation="search", attached_by="user-1")
    drifted = binding.model_copy(
        update={"descriptor_ref": binding.descriptor_ref.model_copy(update={"digest": "sha256:tampered"})}
    )

    reason = registry.check_binding_freshness(drifted)
    assert reason is not None
    assert "descriptor_ref.digest mismatch" in reason


def test_check_binding_freshness_detects_missing_instance() -> None:
    registry = default_registry()
    instance = registry.register_instance(
        _instance(instance_id="going-away", descriptor_id="foundry.azure_ai_search")
    )
    binding = registry.attach(
        descriptor_id="foundry.azure_ai_search",
        operation="search",
        attached_by="user-1",
        instance_id=instance.id,
    )
    fresh_registry = CapabilityRegistry(descriptors=registry.catalog())

    reason = fresh_registry.check_binding_freshness(binding)
    assert reason is not None
    assert "no longer registered" in reason


def test_check_binding_freshness_detects_unavailable_instance() -> None:
    registry = default_registry()
    instance = registry.register_instance(
        _instance(instance_id="flaky", descriptor_id="foundry.azure_ai_search")
    )
    binding = registry.attach(
        descriptor_id="foundry.azure_ai_search",
        operation="search",
        attached_by="user-1",
        instance_id=instance.id,
    )
    registry.register_instance(
        _instance(
            instance_id="flaky",
            descriptor_id="foundry.azure_ai_search",
            readiness=InstanceReadiness.UNAVAILABLE,
            unavailable_reason="connection revoked",
        )
    )

    reason = registry.check_binding_freshness(binding)
    assert reason is not None
    assert "connection revoked" in reason


def test_check_binding_freshness_detects_instance_fingerprint_drift() -> None:
    registry = default_registry()
    instance = registry.register_instance(
        _instance(instance_id="reconfig", descriptor_id="foundry.azure_ai_search", version="2026.07")
    )
    binding = registry.attach(
        descriptor_id="foundry.azure_ai_search",
        operation="search",
        attached_by="user-1",
        instance_id=instance.id,
    )
    # Reconfiguration: same instance id, different discovered provider version
    # changes the fingerprint without changing readiness.
    registry.register_instance(
        _instance(instance_id="reconfig", descriptor_id="foundry.azure_ai_search", version="2027.01")
    )

    reason = registry.check_binding_freshness(binding)
    assert reason is not None
    assert "reconfigured since attach" in reason


def test_check_binding_freshness_ignores_bindings_created_without_digest_pins() -> None:
    """A binding constructed directly (not via ``attach``) with no digest/
    fingerprint pins has nothing to compare against, so it is trivially
    reported fresh — the absence of a pin is a caller-authoring choice, not
    something this check can retroactively invent evidence for."""
    registry = default_registry()
    instance = registry.register_instance(
        _instance(instance_id="unpinned", descriptor_id="foundry.azure_ai_search")
    )
    binding = CapabilityBinding(
        provider_contract_version="agent-studio.capability-registry.v1",
        descriptor_ref=CapabilityDescriptorRef(id="foundry.azure_ai_search"),
        operation_ref=CapabilityOperationRef(id="search"),
        instance_ref=CapabilityInstanceRef(provider_id="foundry", id=instance.id),
        attached_by="user-1",
    )

    assert registry.check_binding_freshness(binding) is None


def test_attach_pins_destination_constraints_from_resolved_operation() -> None:
    descriptor = _descriptor(
        "custom.destination-test",
        CapabilityOperation(
            name="send",
            maturity=OperationMaturity.GA,
            side_effect_destinations=("webhook.example",),
        ),
    )
    registry = CapabilityRegistry(descriptors=(descriptor,))

    binding = registry.attach(descriptor_id="custom.destination-test", operation="send", attached_by="user-1")

    assert binding.destination_constraints == ("webhook.example",)


def test_check_binding_freshness_detects_operation_removed_from_descriptor() -> None:
    descriptor = _descriptor(
        "custom.destination-test",
        CapabilityOperation(
            name="send",
            maturity=OperationMaturity.GA,
            side_effect_destinations=("webhook.example",),
        ),
    )
    registry = CapabilityRegistry(descriptors=(descriptor,))
    binding = registry.attach(descriptor_id="custom.destination-test", operation="send", attached_by="user-1")

    # Catalog update replaces the descriptor content (operation renamed/removed).
    # Pin the binding's descriptor_digest to the *new* content so the digest
    # check passes and the operation-removed branch is isolated.
    replacement_descriptor = _descriptor(
        "custom.destination-test",
        CapabilityOperation(name="other", maturity=OperationMaturity.GA),
    )
    stale_registry = CapabilityRegistry(descriptors=(replacement_descriptor,))
    binding = binding.model_copy(
        update={
            "descriptor_ref": binding.descriptor_ref.model_copy(
                update={"digest": compute_descriptor_digest(replacement_descriptor)}
            )
        }
    )

    reason = stale_registry.check_binding_freshness(binding)
    assert reason is not None
    assert "no longer exists" in reason


def test_check_binding_freshness_detects_destination_constraints_drift() -> None:
    descriptor = _descriptor(
        "custom.destination-test",
        CapabilityOperation(
            name="send",
            maturity=OperationMaturity.GA,
            side_effect_destinations=("webhook.example",),
        ),
    )
    registry = CapabilityRegistry(descriptors=(descriptor,))
    binding = registry.attach(descriptor_id="custom.destination-test", operation="send", attached_by="user-1")
    drifted = binding.model_copy(update={"destination_constraints": ("webhook.other",)})

    reason = registry.check_binding_freshness(drifted)
    assert reason is not None
    assert "destination_constraints mismatch" in reason


def test_check_binding_freshness_detects_operation_no_longer_bindable() -> None:
    descriptor = _descriptor(
        "custom.lifecycle-test",
        CapabilityOperation(name="run", maturity=OperationMaturity.GA, version="1"),
    )
    registry = CapabilityRegistry(descriptors=(descriptor,))
    binding = registry.attach(descriptor_id="custom.lifecycle-test", operation="run", attached_by="user-1")

    # Catalog update deprecates the operation without changing its name/version.
    replacement_descriptor = _descriptor(
        "custom.lifecycle-test",
        CapabilityOperation(
            name="run",
            maturity=OperationMaturity.GA,
            version="1",
            lifecycle=OperationLifecycle.DEPRECATED,
        ),
    )
    stale_registry = CapabilityRegistry(descriptors=(replacement_descriptor,))
    binding = binding.model_copy(
        update={
            "descriptor_ref": binding.descriptor_ref.model_copy(
                update={"digest": compute_descriptor_digest(replacement_descriptor)}
            )
        }
    )

    reason = stale_registry.check_binding_freshness(binding)
    assert reason is not None
    assert "no longer bindable" in reason
    assert "deprecated lifecycle" in reason


def test_check_binding_freshness_detects_operation_version_drift() -> None:
    descriptor = _descriptor(
        "custom.version-test",
        CapabilityOperation(name="run", maturity=OperationMaturity.GA, version="1"),
    )
    registry = CapabilityRegistry(descriptors=(descriptor,))
    binding = registry.attach(descriptor_id="custom.version-test", operation="run", attached_by="user-1")
    assert binding.operation_ref.version == "1"

    # Catalog update bumps the operation's version while keeping it GA+ACTIVE.
    replacement_descriptor = _descriptor(
        "custom.version-test",
        CapabilityOperation(name="run", maturity=OperationMaturity.GA, version="2"),
    )
    stale_registry = CapabilityRegistry(descriptors=(replacement_descriptor,))
    binding = binding.model_copy(
        update={
            "descriptor_ref": binding.descriptor_ref.model_copy(
                update={"digest": compute_descriptor_digest(replacement_descriptor)}
            )
        }
    )

    reason = stale_registry.check_binding_freshness(binding)
    assert reason is not None
    assert "operation_ref.version mismatch" in reason
    assert "pinned '1'" in reason
    assert "now '2'" in reason
