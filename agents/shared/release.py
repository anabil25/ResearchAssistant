from __future__ import annotations

import importlib.metadata
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .capabilities import ToolRegistration
from .contracts import AgentManifest, canonical_digest
from .errors import ConfigurationError
from .idempotency import idempotency_contract_schema_digest


class DependencyRisk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    package: str
    version: str
    maturity: Literal["beta"]
    feature: str
    risk: str


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
    provider_contracts: tuple[tuple[str, str], ...]


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
    )
    revision = source_revision or os.getenv("GIT_COMMIT_SHA") or "local"
    manifest_hash = manifest_digest(manifest)
    contract_schema_hash = canonical_digest(AgentManifest.model_json_schema())
    idempotency_schema_hash = idempotency_contract_schema_digest()
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
                )
                for binding in manifest.capability_bindings
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
        "provider_contracts": provider_contracts,
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
        provider_contracts=provider_contracts,
    )
