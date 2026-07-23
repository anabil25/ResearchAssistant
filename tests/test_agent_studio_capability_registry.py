from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.capability_registry import (
    CapabilityAttachmentError,
    CapabilityRegistry,
    default_registry,
)
from research_assistant_api.agent_studio.models import (
    CapabilityDescriptor,
    CapabilityOperation,
    OperationClass,
    OperationMaturity,
)


def test_default_registry_seeds_known_capabilities() -> None:
    registry = default_registry()
    catalog = registry.catalog()
    ids = {descriptor.id for descriptor in catalog}
    assert "foundry.web_search" in ids
    assert "foundry.memory" in ids
    assert "custom.hosted_code" in ids
    assert registry.get("foundry.web_search") is not None
    assert registry.get("missing.capability") is None
    assert registry.as_mapping()["foundry.web_search"].id == "foundry.web_search"


def test_validate_attachment_accepts_ga_operation() -> None:
    registry = default_registry()
    resolved = registry.validate_attachment(descriptor_id="foundry.web_search", operation="search")
    assert resolved.maturity == OperationMaturity.GA


def test_validate_attachment_rejects_unknown_descriptor() -> None:
    registry = default_registry()
    with pytest.raises(CapabilityAttachmentError, match="not in the catalog"):
        registry.validate_attachment(descriptor_id="unknown.capability", operation="search")


def test_validate_attachment_rejects_unknown_operation() -> None:
    registry = default_registry()
    with pytest.raises(CapabilityAttachmentError, match="has no operation"):
        registry.validate_attachment(descriptor_id="foundry.web_search", operation="unknown_op")


def test_validate_attachment_rejects_preview_operation_with_reason() -> None:
    registry = default_registry()
    with pytest.raises(CapabilityAttachmentError, match="Documented as preview"):
        registry.validate_attachment(descriptor_id="foundry.memory", operation="recall")


def test_validate_attachment_rejects_unavailable_operation_with_reason() -> None:
    registry = default_registry()
    with pytest.raises(CapabilityAttachmentError, match="cannot run inside Managed Foundry"):
        registry.validate_attachment(descriptor_id="custom.hosted_code", operation="run")


def test_validate_attachment_uses_generic_reason_when_none_supplied() -> None:
    registry = CapabilityRegistry(
        (
            CapabilityDescriptor(
                id="custom.no_reason",
                provider="custom",
                title="No Reason",
                description="Operation with no explicit reason.",
                operations=(CapabilityOperation(name="run", maturity=OperationMaturity.PREVIEW),),
            ),
        )
    )
    with pytest.raises(CapabilityAttachmentError, match="is preview"):
        registry.validate_attachment(descriptor_id="custom.no_reason", operation="run")


def test_attach_returns_capability_instance_for_ga_operation() -> None:
    registry = default_registry()
    instance = registry.attach(
        descriptor_id="foundry.azure_ai_search",
        operation="search",
        attached_by="user-1",
        workspace_connection_id="conn-1",
        config={"index": "my-index"},
    )
    assert instance.descriptor_id == "foundry.azure_ai_search"
    assert instance.operation == "search"
    assert instance.attached_by == "user-1"
    assert instance.workspace_connection_id == "conn-1"
    assert instance.config == {"index": "my-index"}
    descriptor = registry.get("foundry.azure_ai_search")
    assert descriptor is not None
    assert instance.descriptor_version == descriptor.version


def test_attach_defaults_config_to_empty_dict() -> None:
    registry = default_registry()
    instance = registry.attach(descriptor_id="foundry.web_search", operation="search", attached_by="user-1")
    assert instance.config == {}
    assert instance.workspace_connection_id is None


def test_attach_rejects_preview_operation() -> None:
    registry = default_registry()
    with pytest.raises(CapabilityAttachmentError):
        registry.attach(descriptor_id="foundry.memory", operation="store", attached_by="user-1")


def test_seeded_operations_carry_operation_class_and_side_effect_metadata() -> None:
    registry = default_registry()
    search = registry.get("foundry.web_search")
    assert search is not None
    search_op = search.operation("search")
    assert search_op is not None
    assert search_op.operation_class == OperationClass.READ
    assert search_op.side_effect_destinations == ("public_web",)
    assert search_op.requires_approval is False

    functions = registry.get("foundry.azure_functions")
    assert functions is not None
    invoke_op = functions.operation("invoke")
    assert invoke_op is not None
    assert invoke_op.operation_class == OperationClass.WRITE_IRREVERSIBLE
    assert invoke_op.requires_approval is True

    function_calling = registry.get("foundry.function_calling")
    assert function_calling is not None
    fc_op = function_calling.operation("invoke")
    assert fc_op is not None
    assert fc_op.operation_class == OperationClass.PURE


def test_custom_registry_can_override_seed_descriptors() -> None:
    custom_descriptor = CapabilityDescriptor(
        id="custom.only",
        provider="custom",
        title="Only",
        description="Custom only descriptor.",
        operations=(CapabilityOperation(name="run", maturity=OperationMaturity.GA),),
    )
    registry = CapabilityRegistry((custom_descriptor,))
    assert registry.catalog() == (custom_descriptor,)
    assert registry.get("foundry.web_search") is None
