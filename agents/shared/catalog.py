from __future__ import annotations

from urllib.parse import urlsplit

from .capabilities import (
    ApprovalMode,
    CapabilityDescriptor,
    IdempotencyMode,
    OperationClass,
    RetryPolicy,
)
from .contracts import AgentManifest
from .settings import HarnessSettings


def capabilities_for_manifest(
    manifest: AgentManifest,
    settings: HarnessSettings | None = None,
) -> tuple[CapabilityDescriptor, ...]:
    destination = _toolbox_destination(settings)
    definitions = {
        "specialist.delegate": CapabilityDescriptor(
            id="specialist.delegate",
            operation=OperationClass.READ,
            required_scopes=frozenset({"research.specialist.invoke"}),
            allowed_destinations=(
                "literature-agent",
                "grant-agent",
                "matching-agent",
                "dataset-agent",
                "institution-agent",
                "literature-online-agent",
                "grant-online-agent",
                "matching-online-agent",
            ),
            retry=RetryPolicy(max_attempts=4, delays_seconds=(15, 30, 60)),
            idempotency=IdempotencyMode.OPTIONAL,
            redact_fields=frozenset({"request"}),
        ),
        "dataset.compute": CapabilityDescriptor(
            id="dataset.compute",
            operation=OperationClass.PRIVILEGED,
            required_scopes=frozenset({"research.dataset.compute"}),
            allowed_destinations=(destination,) if destination else (),
            side_effect_destinations=(destination,) if destination else ("unconfigured",),
            approval=ApprovalMode.REQUIRED,
            timeout_seconds=120,
            idempotency=IdempotencyMode.REQUIRED,
            retry=RetryPolicy(max_attempts=1),
            redact_fields=frozenset({"dataset"}),
        ),
        "literature.files": _session_file_analysis("literature.files", destination),
        "grant.files": _session_file_analysis("grant.files", destination),
        "matching.files": _session_file_analysis("matching.files", destination),
        "literature.public_lookup": _public_lookup("literature.public_lookup", destination),
        "grant.public_lookup": _public_lookup("grant.public_lookup", destination),
        "matching.public_lookup": _public_lookup("matching.public_lookup", destination),
    }
    return tuple(definitions[item] for item in manifest.capability_ids)


def _session_file_analysis(capability_id: str, destination: str | None) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=capability_id,
        operation=OperationClass.READ,
        required_scopes=frozenset({"research.session-files.read"}),
        allowed_destinations=(destination,) if destination else (),
        timeout_seconds=120,
        retry=RetryPolicy(max_attempts=1),
        idempotency=IdempotencyMode.OPTIONAL,
        redact_fields=frozenset({"attachments"}),
    )


def _public_lookup(capability_id: str, destination: str | None) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        id=capability_id,
        operation=OperationClass.READ,
        required_scopes=frozenset({"research.public.read"}),
        allowed_destinations=(destination,) if destination else (),
        timeout_seconds=120,
        retry=RetryPolicy(max_attempts=3, delays_seconds=(1, 3)),
        idempotency=IdempotencyMode.OPTIONAL,
        redact_fields=frozenset({"query"}),
    )


def _toolbox_destination(settings: HarnessSettings | None) -> str | None:
    if settings is None or settings.toolbox_endpoint is None:
        return None
    parsed = urlsplit(str(settings.toolbox_endpoint))
    return parsed.hostname
