# mypy: disable-error-code=import-untyped

from __future__ import annotations

import asyncio

import pytest
from research_assistant_api.agent_studio.capability_discovery import (
    CapabilityDiscoveryRequest,
    CapabilityDiscoveryResult,
    CapabilityDiscoverySource,
    InMemoryCapabilityDiscoverySource,
    NullCapabilityDiscoverySource,
    discover_with_timeout,
)
from research_assistant_api.agent_studio.models import (
    CapabilityDescriptor,
    CapabilityInstance,
    CapabilityOperation,
    InstanceReadiness,
    OperationMaturity,
)
from research_assistant_api.agent_studio.scope import ScopeContext


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


def _request(*, timeout_seconds: float = 5.0) -> CapabilityDiscoveryRequest:
    return CapabilityDiscoveryRequest(
        scope=ScopeContext(tenant_id="tenant-1", project_id="project-1"),
        principal="user-1",
        correlation_id="correlation-1",
        timeout_seconds=timeout_seconds,
    )


def test_discovery_result_defaults_to_empty_and_available() -> None:
    result = CapabilityDiscoveryResult()

    assert result.descriptors == ()
    assert result.instances == ()
    assert result.warnings == ()
    assert result.available is True
    assert result.unavailable_reason is None


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


def test_discovery_result_available_result_rejects_unavailable_reason() -> None:
    with pytest.raises(ValueError, match="must not carry an unavailable_reason"):
        CapabilityDiscoveryResult(available=True, unavailable_reason="should not be set")


def test_discovery_result_unavailable_result_requires_reason() -> None:
    with pytest.raises(ValueError, match="must carry a non-empty unavailable_reason"):
        CapabilityDiscoveryResult(available=False)


def test_discovery_result_unavailable_result_cannot_carry_descriptors_or_instances() -> None:
    descriptor = _descriptor("custom.only")

    with pytest.raises(ValueError, match="must be empty"):
        CapabilityDiscoveryResult(
            descriptors=(descriptor,), available=False, unavailable_reason="provider unreachable"
        )


def test_discovery_request_requires_explicit_scope_principal_and_correlation_id() -> None:
    request = _request()

    assert request.scope.tenant_id == "tenant-1"
    assert request.scope.project_id == "project-1"
    assert request.principal == "user-1"
    assert request.correlation_id == "correlation-1"
    assert request.timeout_seconds == 5.0


@pytest.mark.asyncio
async def test_null_source_returns_explicit_unavailable_result() -> None:
    """A null/unconfigured source must produce an explicit unavailable
    result, not an indistinguishable empty success."""
    source: CapabilityDiscoverySource = NullCapabilityDiscoverySource()

    result = await source.discover(_request())

    assert result.available is False
    assert result.unavailable_reason is not None
    assert result.descriptors == ()
    assert result.instances == ()
    assert result.warnings == ()


@pytest.mark.asyncio
async def test_in_memory_source_returns_fixed_result() -> None:
    descriptor = _descriptor("custom.only")
    fixed = CapabilityDiscoveryResult(descriptors=(descriptor,))

    source: CapabilityDiscoverySource = InMemoryCapabilityDiscoverySource(fixed)

    assert await source.discover(_request()) is fixed


@pytest.mark.asyncio
async def test_in_memory_source_defaults_to_honest_empty_success_when_none_given() -> None:
    """Distinct from ``NullCapabilityDiscoverySource``: an in-memory source
    with no explicit result models a reachable provider that genuinely has
    nothing to report -- ``available`` stays ``True``."""
    source = InMemoryCapabilityDiscoverySource()

    result = await source.discover(_request())

    assert result.descriptors == ()
    assert result.instances == ()
    assert result.available is True
    assert result.unavailable_reason is None


@pytest.mark.asyncio
async def test_discover_with_timeout_returns_underlying_result_when_within_budget() -> None:
    descriptor = _descriptor("custom.only")
    fixed = CapabilityDiscoveryResult(descriptors=(descriptor,))
    source = InMemoryCapabilityDiscoverySource(fixed, delay_seconds=0.01)

    result = await discover_with_timeout(source, _request(timeout_seconds=5.0))

    assert result is fixed


@pytest.mark.asyncio
async def test_discover_with_timeout_translates_slow_provider_into_unavailable() -> None:
    source = InMemoryCapabilityDiscoverySource(delay_seconds=0.2)

    result = await discover_with_timeout(source, _request(timeout_seconds=0.01))

    assert result.available is False
    assert result.unavailable_reason is not None
    assert "timed out" in result.unavailable_reason
    assert result.descriptors == ()
    assert result.instances == ()


@pytest.mark.asyncio
async def test_discover_with_timeout_translates_cancellation_into_unavailable() -> None:
    source = InMemoryCapabilityDiscoverySource(raise_cancelled=True)

    result = await discover_with_timeout(source, _request())

    assert result.available is False
    assert result.unavailable_reason is not None
    assert "cancelled" in result.unavailable_reason


@pytest.mark.asyncio
async def test_discover_with_timeout_propagates_outer_task_cancellation() -> None:
    """A caller-initiated cancellation of the awaiting task itself must still
    propagate as ``CancelledError`` -- only a *provider-raised* cancellation
    is translated into an honest unavailable result."""
    source = InMemoryCapabilityDiscoverySource(delay_seconds=1.0)

    task = asyncio.ensure_future(discover_with_timeout(source, _request(timeout_seconds=5.0)))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

