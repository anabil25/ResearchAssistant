from __future__ import annotations

import hmac
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts import canonical_digest
from .errors import ConfigurationError

SOURCE_MANIFEST_PATH = Path(__file__).resolve().parents[1] / ".release" / "source-tree.json"


class BakedSourceTreeManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal["1"]
    inclusion_policy_version: Literal["1"]
    producer: Literal["research-assistant.git-source-tree"]
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    source_root: Literal["agents"]
    source_tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_count: int = Field(gt=0)
    source_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def load_baked_source_tree_manifest(path: Path | None = None) -> BakedSourceTreeManifest:
    manifest_path = path or SOURCE_MANIFEST_PATH
    try:
        payload = manifest_path.read_text(encoding="utf-8")
        manifest = BakedSourceTreeManifest.model_validate_json(payload)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise ConfigurationError(
            "Baked Hosted Agent source identity is missing or invalid; "
            "run scripts/build_agent_source_tree.py before startup",
            context={
                "path": str(manifest_path),
                "producer": "scripts/build_agent_source_tree.py",
            },
        ) from exc
    # Git-object provenance makes this an independently regenerable correctness control.
    expected_digest = canonical_digest(
        manifest.model_dump(mode="json", exclude={"source_manifest_digest"})
    )
    if not hmac.compare_digest(manifest.source_manifest_digest, expected_digest):
        raise ConfigurationError(
            "Baked Hosted Agent source identity failed its canonical digest check; "
            "regenerate it with scripts/build_agent_source_tree.py",
            context={
                "path": str(manifest_path),
                "producer": "scripts/build_agent_source_tree.py",
            },
        )
    return manifest
