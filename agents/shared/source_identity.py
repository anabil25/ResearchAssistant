from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigurationError

SOURCE_MANIFEST_PATH = Path(__file__).resolve().parents[1] / ".release" / "source-bundle.json"


class BakedSourceBundleManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"]
    producer: Literal["research-assistant.git-source-bundle"]
    source_revision: str = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    source_root: Literal["agents"]
    source_bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entry_count: int = Field(gt=0)


def load_baked_source_bundle_manifest(path: Path | None = None) -> BakedSourceBundleManifest:
    manifest_path = path or SOURCE_MANIFEST_PATH
    try:
        payload = manifest_path.read_text(encoding="utf-8")
        return BakedSourceBundleManifest.model_validate_json(payload)
    except (OSError, UnicodeError, ValidationError) as exc:
        raise ConfigurationError(
            "Baked Hosted Agent source identity is missing or invalid",
            context={"path": str(manifest_path)},
        ) from exc
