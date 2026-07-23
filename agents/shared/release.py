from __future__ import annotations

import importlib.metadata
import os
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .approvals import approval_contract_schema_digest
from .capabilities import PROVIDER_CONTRACT_ARTIFACT_DIGEST, ToolRegistration
from .contracts import AgentManifest, ObjectiveGate, canonical_digest
from .errors import ConfigurationError, HarnessError, ReleaseAttestationError
from .idempotency import idempotency_contract_schema_digest


class DependencyRisk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    package: str
    version: str
    maturity: Literal["beta", "preview"]
    feature: str
    risk: str


class ReleaseAttestationStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class ObjectiveGateAttestation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: ObjectiveGate
    passed: Literal[True] = True
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReleaseAttestation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attestation_id: str = Field(min_length=1, max_length=512)
    issuer: str = Field(min_length=1, max_length=512)
    version: str = Field(min_length=1, max_length=128)
    release_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_attestation_contract_schema_digest: str = Field(
        pattern=r"^[0-9a-f]{64}$"
    )
    source_bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_deployment_ref: str = Field(min_length=1, max_length=2048)
    model_version: str = Field(min_length=1, max_length=128)
    provider_contracts: tuple[tuple[str, str, str], ...]
    provider_artifacts: tuple[tuple[str, str, str], ...]
    objective_gates: tuple[ObjectiveGateAttestation, ...]
    status: ReleaseAttestationStatus = ReleaseAttestationStatus.ACTIVE
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def attestation_is_canonical(self) -> ReleaseAttestation:
        gates = tuple(item.gate for item in self.objective_gates)
        if gates != tuple(sorted(set(gates))):
            raise ValueError("objective gate attestations must be sorted and unique")
        if self.provider_contracts != tuple(sorted(set(self.provider_contracts))):
            raise ValueError("provider contracts must be sorted and unique")
        if self.provider_artifacts != tuple(sorted(set(self.provider_artifacts))):
            raise ValueError("provider artifacts must be sorted and unique")
        if self.issued_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("release attestation timestamps must be timezone-aware")
        if self.issued_at > self.expires_at:
            raise ValueError("release attestation expires before issuance")
        return self


class ReleaseAttestor(Protocol):
    is_durable: bool

    def attest(self, release: ReleaseMetadata) -> ReleaseAttestation: ...


class ReleaseMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_id: str
    agent_name: str
    agent_version: str
    release_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    parent_release_id: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_manifest_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    input_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str
    model_deployment: str
    model_deployment_ref: str
    model_version: str
    runtime_kind: str
    built_at: datetime
    dependencies: tuple[tuple[str, str], ...]
    dependency_risks: tuple[DependencyRisk, ...]
    capability_versions: tuple[tuple[str, str], ...]
    toolbox_versions: tuple[tuple[str, str], ...]
    knowledge_versions: tuple[tuple[str, str], ...]
    protocol: str = "responses"
    protocol_version: str = "2.0.0"
    contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    release_attestation_contract_schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_contracts: tuple[tuple[str, str, str], ...]
    provider_artifacts: tuple[tuple[str, str, str], ...]


def manifest_digest(manifest: AgentManifest) -> str:
    return canonical_digest(manifest.model_dump(mode="json"))


def source_bundle_digest(root: Path | None = None) -> str:
    source_root = root or Path(__file__).resolve().parents[1]
    entries: list[tuple[str, str]] = []
    for path in source_root.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix != ".py" and path.name != "requirements.txt":
            continue
        entries.append(
            (
                path.relative_to(source_root).as_posix(),
                path.read_bytes().hex(),
            )
        )
    entries.sort(key=lambda item: item[0])
    return canonical_digest(entries)


def release_attestation_contract_schema_digest() -> str:
    return canonical_digest(
        {
            "objective_gate": ObjectiveGateAttestation.model_json_schema(),
            "release_attestation": ReleaseAttestation.model_json_schema(),
        }
    )


def build_release_metadata(
    manifest: AgentManifest,
    *,
    model_deployment: str,
    source_revision: str | None = None,
    source_bundle_hash: str | None = None,
    parent_release_id: str | None = None,
    built_at: datetime | None = None,
    registrations: tuple[ToolRegistration, ...] = (),
) -> ReleaseMetadata:
    if model_deployment != manifest.model_policy.deployment_name:
        raise ValueError("resolved model deployment does not match manifest model policy")
    if tuple(registration.binding for registration in registrations) != manifest.capability_bindings or any(
        not registration.runtime_attested for registration in registrations
    ):
        raise ConfigurationError(
            "Release requires continuously attested provider registrations",
            context={"agent": manifest.id},
        )
    packages = (
        "agent-framework-core",
        "agent-framework-foundry",
        "agent-framework-foundry-hosting",
        "azure-ai-projects",
        "openai",
        "pydantic",
    )
    dependencies: list[tuple[str, str]] = []
    for package in packages:
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = "not-installed"
        dependencies.append((package, version))
    dependency_versions = dict(dependencies)
    hosting_version = dependency_versions["agent-framework-foundry-hosting"]
    if hosting_version != "1.0.0b260721":
        raise ConfigurationError(
            "Hosted Agent serving requires the reviewed beta package pin",
            context={
                "package": "agent-framework-foundry-hosting",
                "expected": "1.0.0b260721",
                "actual": hosting_version,
            },
        )
    dependency_risks = (
        DependencyRisk(
            package="agent-framework-foundry-hosting",
            version=hosting_version,
            maturity="beta",
            feature="direct-code Hosted Agent Responses server",
            risk="Beta hosting APIs may change before general availability.",
        ),
        DependencyRisk(
            package="agent-framework-core",
            version=dependency_versions["agent-framework-core"],
            maturity="preview",
            feature="deterministic coordinator workflow orchestration",
            risk="Workflow APIs may change before general availability.",
        ),
    )
    revision = source_revision or os.getenv("GIT_COMMIT_SHA") or "local"
    manifest_hash = manifest_digest(manifest)
    contract_schema_hash = canonical_digest(AgentManifest.model_json_schema())
    idempotency_schema_hash = idempotency_contract_schema_digest()
    approval_schema_hash = approval_contract_schema_digest()
    release_attestation_schema_hash = release_attestation_contract_schema_digest()
    bundle_hash = source_bundle_hash or source_bundle_digest()
    capability_versions = tuple(
        sorted((binding.descriptor_ref.id, binding.descriptor_ref.version) for binding in manifest.capability_bindings)
    )
    toolbox_versions = tuple(
        sorted(
            (binding.operation_ref.id, binding.operation_ref.version)
            for binding in manifest.capability_bindings
            if binding.operation_ref.id.startswith("foundry.toolbox.")
        )
    )
    knowledge_versions = tuple(
        sorted((binding.binding_id, binding.pinned_version) for binding in manifest.knowledge_bindings)
    )
    provider_contracts = tuple(
        sorted(
            {
                (
                    binding.instance_ref.provider_id,
                    binding.provider_contract_version,
                    binding.provider_contract_schema_digest,
                )
                for binding in manifest.capability_bindings
            }
        )
    )
    provider_artifacts = tuple(
        sorted(
            {
                (
                    registration.binding.instance_ref.provider_id,
                    registration.binding.instance_ref.discovered_provider_version,
                    PROVIDER_CONTRACT_ARTIFACT_DIGEST,
                )
                for registration in registrations
            }
        )
    )
    release_payload = {
        "agent_id": manifest.id,
        "agent_name": manifest.name,
        "parent_release_id": parent_release_id,
        "manifest_digest": manifest_hash,
        "parent_manifest_hash": manifest.parent_manifest_hash,
        "input_schema_hash": manifest.input_schema.sha256,
        "output_schema_hash": manifest.output_schema.sha256,
        "source_bundle_hash": bundle_hash,
        "source_revision": revision,
        "model_deployment": model_deployment,
        "model_deployment_ref": manifest.model_policy.selected_deployment_ref,
        "model_version": manifest.model_policy.pinned_model_version,
        "runtime_kind": manifest.runtime_requirements.selected_runtime,
        "dependencies": dependencies,
        "dependency_risks": [risk.model_dump(mode="json") for risk in dependency_risks],
        "capability_versions": capability_versions,
        "toolbox_versions": toolbox_versions,
        "knowledge_versions": knowledge_versions,
        "protocol": "responses",
        "protocol_version": "2.0.0",
        "contract_schema_digest": contract_schema_hash,
        "idempotency_contract_schema_digest": idempotency_schema_hash,
        "approval_contract_schema_digest": approval_schema_hash,
        "release_attestation_contract_schema_digest": release_attestation_schema_hash,
        "provider_contracts": provider_contracts,
        "provider_artifacts": provider_artifacts,
    }
    return ReleaseMetadata(
        agent_id=manifest.id,
        agent_name=manifest.name,
        agent_version=manifest.version,
        release_id=f"sha256:{canonical_digest(release_payload)}",
        parent_release_id=parent_release_id,
        manifest_digest=manifest_hash,
        parent_manifest_hash=manifest.parent_manifest_hash,
        input_schema_hash=manifest.input_schema.sha256,
        output_schema_hash=manifest.output_schema.sha256,
        source_bundle_hash=bundle_hash,
        source_revision=revision,
        model_deployment=model_deployment,
        model_deployment_ref=manifest.model_policy.selected_deployment_ref,
        model_version=manifest.model_policy.pinned_model_version,
        runtime_kind=manifest.runtime_requirements.selected_runtime,
        built_at=built_at or datetime.now(UTC),
        dependencies=tuple(dependencies),
        dependency_risks=dependency_risks,
        capability_versions=capability_versions,
        toolbox_versions=toolbox_versions,
        knowledge_versions=knowledge_versions,
        contract_schema_digest=contract_schema_hash,
        idempotency_contract_schema_digest=idempotency_schema_hash,
        approval_contract_schema_digest=approval_schema_hash,
        release_attestation_contract_schema_digest=release_attestation_schema_hash,
        provider_contracts=provider_contracts,
        provider_artifacts=provider_artifacts,
    )


def validate_release_attestation(
    release: ReleaseMetadata,
    manifest: AgentManifest,
    attestor: ReleaseAttestor | None,
    *,
    allow_test_attestor: bool = False,
    now: datetime | None = None,
) -> ReleaseAttestation:
    if attestor is None or (
        not getattr(attestor, "is_durable", False) and not allow_test_attestor
    ):
        raise ReleaseAttestationError(
            "Hosted serving requires an app-owned durable release attestor",
            context={"agent": manifest.id},
        )
    try:
        raw = attestor.attest(release)
        if not isinstance(raw, ReleaseAttestation):
            raise TypeError("invalid release attestation type")
        attestation = ReleaseAttestation.model_validate(raw.model_dump(mode="json"))
    except HarnessError:
        raise
    except Exception as exc:
        raise ReleaseAttestationError(
            "Release attestor returned an invalid attestation",
            context={"agent": manifest.id},
        ) from exc
    expected_gates = tuple(sorted(manifest.evaluation.objective_hard_gates))
    actual_gates = tuple(item.gate for item in attestation.objective_gates)
    if (
        attestation.release_id != release.release_id
        or attestation.manifest_digest != release.manifest_digest
        or attestation.contract_schema_digest != release.contract_schema_digest
        or attestation.idempotency_contract_schema_digest
        != release.idempotency_contract_schema_digest
        or attestation.approval_contract_schema_digest
        != release.approval_contract_schema_digest
        or attestation.release_attestation_contract_schema_digest
        != release.release_attestation_contract_schema_digest
        or attestation.source_bundle_hash != release.source_bundle_hash
        or attestation.model_deployment_ref != release.model_deployment_ref
        or attestation.model_version != release.model_version
        or attestation.provider_contracts != release.provider_contracts
        or attestation.provider_artifacts != release.provider_artifacts
        or actual_gates != expected_gates
    ):
        raise ReleaseAttestationError(
            "Release attestation does not match the immutable release",
            context={"agent": manifest.id},
        )
    current = now or datetime.now(UTC)
    if (
        attestation.status != ReleaseAttestationStatus.ACTIVE
        or attestation.issued_at > current
        or attestation.expires_at <= current
    ):
        raise ReleaseAttestationError(
            "Release attestation is revoked or expired",
            context={"agent": manifest.id},
        )
    return attestation


class InMemoryReleaseAttestor:
    is_durable = False
    is_test_only = True

    def __init__(self, objective_gates: tuple[ObjectiveGate, ...]) -> None:
        self._objective_gates = tuple(sorted(objective_gates))

    def attest(self, release: ReleaseMetadata) -> ReleaseAttestation:
        issued_at = datetime.now(UTC)
        return ReleaseAttestation(
            attestation_id=f"local:{release.release_id}",
            issuer="local-test-harness",
            version="1",
            release_id=release.release_id,
            manifest_digest=release.manifest_digest,
            contract_schema_digest=release.contract_schema_digest,
            idempotency_contract_schema_digest=(
                release.idempotency_contract_schema_digest
            ),
            approval_contract_schema_digest=release.approval_contract_schema_digest,
            release_attestation_contract_schema_digest=(
                release.release_attestation_contract_schema_digest
            ),
            source_bundle_hash=release.source_bundle_hash,
            model_deployment_ref=release.model_deployment_ref,
            model_version=release.model_version,
            provider_contracts=release.provider_contracts,
            provider_artifacts=release.provider_artifacts,
            objective_gates=tuple(
                ObjectiveGateAttestation(
                    gate=gate,
                    passed=True,
                    evidence_digest=canonical_digest(
                        {
                            "gate": gate.value,
                            "release_id": release.release_id,
                        }
                    ),
                )
                for gate in self._objective_gates
            ),
            issued_at=issued_at,
            expires_at=issued_at + timedelta(hours=1),
        )
