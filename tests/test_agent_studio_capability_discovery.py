# mypy: disable-error-code=import-untyped

from __future__ import annotations

import pytest
from research_assistant_api.agent_studio.capability_discovery import (
    CapabilityDiscoveryResult,
    CapabilityDiscoverySource,
    InMemoryCapabilityDiscoverySource,
    NullCapabilityDiscoverySource,
)
from research_assistant_api.agent_studio.models import (
    CapabilityDescriptor,
    CapabilityInstance,
    CapabilityOperation,
    InstanceReadiness,
    OperationMaturity,
)


def _descriptor(descriptor_id: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=descriptor_id,
        provider="custom",
        title=descriptor_id,
        description=f"Descriptor {descriptor_id}",
        operations=(CapabilityOperation(name="run", maturity=OperationMaturity.GA),),
    )


def _instance(instance_id: str, descriptor_id: str) -> CapabilityInstance:
    return CapabilityInstance(
        id=instance_id,
        tenant_id="tenant-1",
        project_id="project-1",
        descriptor_id=descriptor_id,
        readiness=InstanceReadiness.READY,
        registered_by="system",
    )


def test_discovery_result_defaults_to_empty() -> None:
    result = CapabilityDiscoveryResult()

    assert result.descriptors == ()
    assert result.instances == ()
    assert result.warnings == ()


def test_discovery_result_accepts_consistent_descriptors_and_instances() -> None:
    descriptor = _descriptor("custom.only")
    instance = _instance("instance-a", "custom.only")

    result = CapabilityDiscoveryResult(
        descriptors=(descriptor,),
        instances=(instance,),
        warnings=("one provider timed out",),
    )

    assert result.descriptors == (descriptor,)
    assert result.instances == (instance,)
    assert result.warnings == ("one provider timed out",)


def test_discovery_result_rejects_duplicate_descriptor_ids() -> None:
    descriptor = _descriptor("custom.only")

    with pytest.raises(ValueError, match="descriptor identities must be unique"):
        CapabilityDiscoveryResult(descriptors=(descriptor, descriptor))


def test_discovery_result_rejects_duplicate_instance_ids() -> None:
    descriptor = _descriptor("custom.only")
    instance = _instance("instance-a", "custom.only")

    with pytest.raises(ValueError, match="instance identities must be unique"):
        CapabilityDiscoveryResult(descriptors=(descriptor,), instances=(instance, instance))


def test_discovery_result_rejects_instance_referencing_unknown_descriptor() -> None:
    instance = _instance("instance-a", "missing.descriptor")

    with pytest.raises(ValueError, match="must reference a returned descriptor"):
        CapabilityDiscoveryResult(instances=(instance,))


def test_null_source_returns_empty_result() -> None:
    source: CapabilityDiscoverySource = NullCapabilityDiscoverySource()

    result = source.discover()

    assert result.descriptors == ()
    assert result.instances == ()
    assert result.warnings == ()


def test_in_memory_source_returns_fixed_result() -> None:
    descriptor = _descriptor("custom.only")
    fixed = CapabilityDiscoveryResult(descriptors=(descriptor,))

    source: CapabilityDiscoverySource = InMemoryCapabilityDiscoverySource(fixed)

    assert source.discover() is fixed


def test_in_memory_source_defaults_to_empty_result_when_none_given() -> None:
    source = InMemoryCapabilityDiscoverySource()

    result = source.discover()

    assert result.descriptors == ()
    assert result.instances == ()
