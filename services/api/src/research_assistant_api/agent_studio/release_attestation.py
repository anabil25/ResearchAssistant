"""Signed, objective ``ReleaseAttestation`` derived from a release's own
immutable ``ReleaseGateReport``, exposed for harness/runtime startup
verification that hard release gates passed for one exact release.

This is a read-only projection, not a new persisted record: an
``AgentRelease`` already carries ``gate_report_id`` (see ``release_service``,
which never transitions a release past ``GATED`` without one), and
``ReleaseGateReport.passed``/``blocking_gates()`` already compute the
objective schema/build/test/auth/policy/approval/security/smoke/binding
verdict. ``build_release_attestation`` only re-packages those two immutable
records into one signed, self-contained object -- it never re-runs gates,
never inspects (let alone is influenced by) the report's advisory
``evaluations``, and never accepts caller-supplied gate results.

This projection also carries forward ``AgentRelease.harness_release_id`` /
``harness_manifest_digest`` / ``harness_link_schema_version`` (harness
blocker #1: "signed release linkage") when a release has them, and covers
them under the same signature -- giving harness a genuinely signed link from
its own release identity to this exact release without either side ever
asserting that harness's own release/manifest hashing scheme and this
package's ``manifest_hash`` are the same digest (they are computed over
different canonical encodings and are not byte-comparable).

The "signature" is deliberately honest about what it actually is: a keyed
HMAC-SHA256 digest when a caller supplies an attestation-signing key, or a
plain (unkeyed) SHA-256 content digest when none is given. Both are
reproducible/tamper-evident, but only the keyed form is a genuine signature
a third party can trust without also trusting "nobody else could compute a
SHA-256 hash" -- ``signature_algorithm`` always says which one a caller
actually received, so nothing here is ever presented as more than it is.

A supplied signing key must always carry an explicit ``key_version``
label. ``key_version`` is embedded in the signed payload
itself (so it cannot be swapped after the fact) and on the resulting
``ReleaseAttestation``, so a verifier that retains multiple historical
secrets (because the active signing key has since been rotated) can look up
the one specific secret an older attestation was actually signed with,
rather than guessing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from research_assistant_api.agent_studio.models import (
    AgentRelease,
    ReleaseAttestation,
    ReleaseAttestationStatus,
    ReleaseGateReport,
    utc_now,
)
from research_assistant_api.agent_studio.scope import ScopeContext
from research_assistant_api.agent_studio.store import AgentStudioStore

#: Versioned prefix for the attestation signature payload, mirroring
#: ``scope.compute_scope_key``'s ``scope:v1:sha256:`` convention. Bumping
#: this to a new version is the only sanctioned way to ever change the
#: canonical encoding or algorithm choice below.
_SIGNATURE_PREFIX = "attestation:v1:"
_HMAC_ALGORITHM = "hmac-sha256"
_DIGEST_ALGORITHM = "sha256-digest"


class ReleaseAttestationError(Exception):
    """Raised when an attestation cannot be honestly built for the given
    release/gate-report pair (e.g. the report does not actually belong to
    the release), never for the ordinary "not gated yet" case, which the
    port surfaces as a structured ``NOT_FOUND`` outcome instead."""


def _canonical_payload(
    *,
    release_id: str,
    version_id: str,
    logical_agent_id: str,
    tenant_id: str,
    project_id: str,
    environment: str,
    manifest_hash: str,
    gate_report_id: str,
    status: str,
    gate_results: tuple[tuple[str, str], ...],
    blocking_gates: tuple[str, ...],
    attested_at_iso: str,
    key_version: str | None,
    harness_release_id: str | None = None,
    harness_manifest_digest: str | None = None,
    harness_link_schema_version: str | None = None,
) -> bytes:
    """Canonical, finite JSON encoding of every field the signature covers.

    Takes plain values (rather than the ``AgentRelease``/``ReleaseGateReport``
    domain objects) so both signing (``build_release_attestation``, from
    freshly-fetched records) and verification (``verify_release_attestation``,
    from a received ``ReleaseAttestation`` alone) compute the identical
    payload without either side needing to reconstruct a domain record.
    Uses a nested JSON object (never a separator-joined string) for the same
    collision-safety reason ``compute_scope_key`` documents: a canonical JSON
    encoding is unambiguous by construction, so no field value (however it is
    composed) can ever be crafted to produce the same payload as a different
    attestation.

    ``key_version`` is included so an attacker who tampers with which
    signing-key version an attestation *claims* to have been signed with
    also invalidates the signature -- a verifier can never be tricked into
    checking the wrong historical secret.

    ``harness_release_id``/``harness_manifest_digest``/
    ``harness_link_schema_version`` (harness blocker #1) are likewise
    included so a signed attestation is tamper-evident about *which* harness
    release identity it is linked to, not just about this package's own
    release facts -- a verifier can trust the cross-reference itself, not
    only the backend-local fields either side of it.
    """

    payload = {
        "release_id": release_id,
        "version_id": version_id,
        "logical_agent_id": logical_agent_id,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "environment": environment,
        "manifest_hash": manifest_hash,
        "gate_report_id": gate_report_id,
        "status": status,
        "gate_results": [list(pair) for pair in gate_results],
        "blocking_gates": list(blocking_gates),
        "attested_at": attested_at_iso,
        "key_version": key_version,
        "harness_release_id": harness_release_id,
        "harness_manifest_digest": harness_manifest_digest,
        "harness_link_schema_version": harness_link_schema_version,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return canonical.encode("utf-8")


def build_release_attestation(
    *,
    release: AgentRelease,
    gate_report: ReleaseGateReport,
    signing_key: str | None,
    key_version: str | None = None,
) -> ReleaseAttestation:
    """Build a signed ``ReleaseAttestation`` from an already-fetched release
    and its own gate report.

    Raises ``ReleaseAttestationError`` if the two records do not actually
    correspond to each other (version/tenant/project mismatch) -- a defensive
    check against ever attesting the wrong pairing, even though callers are
    expected to fetch ``gate_report`` via ``release.gate_report_id``.

    A truthy ``signing_key`` must always be paired with a truthy
    ``key_version``: an unversioned managed secret cannot be rotated or
    audited, so this is refused outright rather than silently accepted as an
    anonymous key. ``key_version`` is ignored (forced to ``None``) when no
    ``signing_key`` is given, since there is no key to version in the
    unkeyed digest form.
    """

    if signing_key and not key_version:
        raise ReleaseAttestationError(
            "A configured attestation signing key must have an explicit key_version "
            "so signed "
            "ReleaseAttestations can be rotated and audited by version; refusing to "
            "sign with an unversioned key."
        )
    if not signing_key:
        key_version = None

    if (
        gate_report.version_id != release.version_id
        or gate_report.tenant_id != release.tenant_id
        or gate_report.project_id != release.project_id
    ):
        raise ReleaseAttestationError(
            f"Gate report '{gate_report.id}' does not belong to release '{release.id}' "
            "(version/tenant/project mismatch)."
        )

    status = ReleaseAttestationStatus.ATTESTED if gate_report.passed else ReleaseAttestationStatus.FAILED
    blocking_gates = tuple(result.name for result in gate_report.blocking_gates())
    attested_at = utc_now()
    canonical = _canonical_payload(
        release_id=release.id,
        version_id=release.version_id,
        logical_agent_id=release.logical_agent_id,
        tenant_id=release.tenant_id,
        project_id=release.project_id,
        environment=release.environment.value,
        manifest_hash=release.manifest_hash,
        gate_report_id=gate_report.id,
        status=status.value,
        gate_results=tuple((result.name.value, result.status.value) for result in gate_report.results),
        blocking_gates=tuple(gate.value for gate in blocking_gates),
        attested_at_iso=attested_at.isoformat(),
        key_version=key_version,
        harness_release_id=release.harness_release_id,
        harness_manifest_digest=release.harness_manifest_digest,
        harness_link_schema_version=release.harness_link_schema_version,
    )
    if signing_key:
        digest = hmac.new(signing_key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        algorithm = _HMAC_ALGORITHM
    else:
        digest = hashlib.sha256(canonical).hexdigest()
        algorithm = _DIGEST_ALGORITHM

    return ReleaseAttestation(
        release_id=release.id,
        version_id=release.version_id,
        logical_agent_id=release.logical_agent_id,
        tenant_id=release.tenant_id,
        project_id=release.project_id,
        environment=release.environment,
        manifest_hash=release.manifest_hash,
        gate_report_id=gate_report.id,
        status=status,
        gate_results=gate_report.results,
        blocking_gates=blocking_gates,
        attested_at=attested_at,
        signature_algorithm=algorithm,
        signature=f"{_SIGNATURE_PREFIX}{algorithm}:{digest}",
        key_version=key_version,
        harness_release_id=release.harness_release_id,
        harness_manifest_digest=release.harness_manifest_digest,
        harness_link_schema_version=release.harness_link_schema_version,
    )


def verify_release_attestation(
    attestation: ReleaseAttestation,
    *,
    signing_key: str | None = None,
    signing_keys: Mapping[str, str] | None = None,
) -> bool:
    """Independently recompute an attestation's signature and compare.

    A caller (harness startup, or this package's own tests) can use this to
    confirm an attestation it received was not tampered with, given the same
    signing key (or the knowledge that none is configured) the issuer used.
    Comparison is constant-time (``hmac.compare_digest``) to avoid leaking
    timing information about a partially-correct signature.

    Two verification modes are supported:

    * ``signing_key``: the caller already knows the one specific secret this
      attestation should have been signed with (the simple, non-rotating
      case, or verifying a single known-current key).
    * ``signing_keys``: a mapping of every historical secret this verifier
      still retains, keyed by version. The attestation's own embedded
      ``key_version`` selects which secret to use -- this is what makes key
      rotation verifiable: an attestation signed under a retired key version
      remains verifiable as long as that version's secret is still in the
      map, while an attestation claiming an unknown or missing key version
      fails closed (returns ``False``) rather than guessing.

    If both are omitted, verification proceeds as the unsigned-digest case
    (``signing_key=None``).
    """

    if signing_keys is not None:
        if attestation.key_version is None:
            return False
        candidate = signing_keys.get(attestation.key_version)
        if candidate is None:
            return False
        signing_key = candidate

    canonical = _canonical_payload(
        release_id=attestation.release_id,
        version_id=attestation.version_id,
        logical_agent_id=attestation.logical_agent_id,
        tenant_id=attestation.tenant_id,
        project_id=attestation.project_id,
        environment=attestation.environment.value,
        manifest_hash=attestation.manifest_hash,
        gate_report_id=attestation.gate_report_id,
        status=attestation.status.value,
        gate_results=tuple((result.name.value, result.status.value) for result in attestation.gate_results),
        blocking_gates=tuple(gate.value for gate in attestation.blocking_gates),
        attested_at_iso=attestation.attested_at.isoformat(),
        key_version=attestation.key_version,
        harness_release_id=attestation.harness_release_id,
        harness_manifest_digest=attestation.harness_manifest_digest,
        harness_link_schema_version=attestation.harness_link_schema_version,
    )
    if signing_key:
        expected = hmac.new(signing_key.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        expected_signature = f"{_SIGNATURE_PREFIX}{_HMAC_ALGORITHM}:{expected}"
    else:
        expected = hashlib.sha256(canonical).hexdigest()
        expected_signature = f"{_SIGNATURE_PREFIX}{_DIGEST_ALGORITHM}:{expected}"
    return hmac.compare_digest(expected_signature, attestation.signature)


class ReleaseAttestationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: ScopeContext
    release_id: str = Field(min_length=1, max_length=200)


class ReleaseAttestationOutcome(StrEnum):
    ATTESTED = "attested"
    NOT_FOUND = "not_found"


class ReleaseAttestationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ReleaseAttestationOutcome
    attestation: ReleaseAttestation | None = None
    reason: str | None = None


class ReleaseAttestationPort(Protocol):
    """Port implemented by whatever composition root wires attestation
    requests to storage. Async, mirroring ``ApprovalConsumptionPort``/
    ``ApprovalContextResolver``, so a future adapter may perform I/O (e.g.
    calling out to a real KMS/HSM signing service instead of a local HMAC
    key) without changing this seam.
    """

    async def get_attestation(self, request: ReleaseAttestationRequest) -> ReleaseAttestationResult: ...


class StoreBackedReleaseAttestationPort:
    """Default, production-safe attestation port backed directly by
    ``AgentStudioStore`` and an optional local HMAC signing key.

    Like release-gate running itself, there is no external provider
    dependency for reading back a release's own already-computed gate
    report -- this package can attest its own gates correctly on its own.

    A truthy ``signing_key`` must always be paired with a truthy
    ``key_version`` (fails closed at construction time, not at first use):
    an unversioned managed secret cannot be rotated or audited, matching the
    same requirement ``build_release_attestation`` enforces.
    """

    def __init__(
        self, store: AgentStudioStore, *, signing_key: str | None = None, key_version: str | None = None
    ) -> None:
        if signing_key and not key_version:
            raise ReleaseAttestationError(
                "A configured attestation signing key must have an explicit key_version "
                "so signed ReleaseAttestations can be rotated and audited by version; "
                "refusing to construct StoreBackedReleaseAttestationPort with an "
                "unversioned key."
            )
        self._store = store
        self._signing_key = signing_key
        self._key_version = key_version if signing_key else None

    async def get_attestation(self, request: ReleaseAttestationRequest) -> ReleaseAttestationResult:
        scope = request.scope
        release = self._store.get_release(scope, request.release_id)
        if release is None:
            return ReleaseAttestationResult(
                outcome=ReleaseAttestationOutcome.NOT_FOUND,
                reason=f"Release '{request.release_id}' was not found in this scope.",
            )
        if release.gate_report_id is None:
            return ReleaseAttestationResult(
                outcome=ReleaseAttestationOutcome.NOT_FOUND,
                reason=f"Release '{request.release_id}' has never had release gates run against it.",
            )
        gate_report = self._store.get_gate_report(scope, release.gate_report_id)
        if gate_report is None:
            return ReleaseAttestationResult(
                outcome=ReleaseAttestationOutcome.NOT_FOUND,
                reason=(
                    f"Gate report '{release.gate_report_id}' referenced by release "
                    f"'{request.release_id}' was not found."
                ),
            )
        attestation = build_release_attestation(
            release=release,
            gate_report=gate_report,
            signing_key=self._signing_key,
            key_version=self._key_version,
        )
        return ReleaseAttestationResult(outcome=ReleaseAttestationOutcome.ATTESTED, attestation=attestation)
