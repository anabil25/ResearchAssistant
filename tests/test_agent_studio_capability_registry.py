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
    default_registry,
)
from research_assistant_api.agent_studio.models import (
    CapabilityDescriptor,
    CapabilityInstance,
    CapabilityOperation,
    InstanceReadiness,
    OperationClass,
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


def test_validate_attachment_fails_closed_for_preview_retired_unknown_and_unavailable() -> None:
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
                "custom.unavailable",
                CapabilityOperation(
                    name="run",
                    maturity=OperationMaturity.UNAVAILABLE,
                    reason="Unavailable in this runtime.",
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
    with pytest.raises(CapabilityAttachmentError, match=re.escape("Unavailable in this runtime.")):
        registry.validate_attachment(descriptor_id="custom.unavailable", operation="run")


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
    assert registry.get_instance("instance-a") == instance


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
    assert registry.get_instance("instance-a") == instance


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
    config = {"index": "docs"}

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

    assert binding.descriptor_id == "foundry.azure_ai_search"
    assert binding.descriptor_version == "1"
    assert binding.operation == "search"
    assert binding.instance_id == "search-1"
    assert binding.pinned_provider_version == "2026.07"
    assert binding.connection_ref == "conn-1"
    assert binding.policy_ref == "policy://search"
    assert binding.config == {"index": "docs"}


def test_attach_without_instance_defaults_to_empty_config_and_no_pin() -> None:
    binding = default_registry().attach(
        descriptor_id="foundry.web_search",
        operation="search",
        attached_by="user-1",
    )

    assert binding.instance_id is None
    assert binding.pinned_provider_version is None
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
