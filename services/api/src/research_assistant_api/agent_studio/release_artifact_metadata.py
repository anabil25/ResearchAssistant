"""Real (non-fabricated) release package/build metadata.

``ReleaseService.cut_version`` used to derive ``package_version`` as
``f"{sequence}.0.0"`` — a fake semver invented from the version's display
``sequence`` number. This module supplies genuinely real data instead:
actual installed package versions (read via ``importlib.metadata``, i.e.
what is truly running), a content digest over that map, and an optional
real source-control revision. ``sequence`` remains a separate display/
ordering concept and is never used to derive any of these values.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from typing import Protocol

from research_assistant_api.agent_studio.models import ReleaseArtifactMetadata

#: Distributions whose real installed version is recorded on every cut
#: version. ``research-assistant-api`` is the hosting package itself;
#: ``fastapi``/``pydantic`` are the framework packages the manifest/gate
#: machinery depends on.
TRACKED_DISTRIBUTIONS: tuple[str, ...] = ("research-assistant-api", "fastapi", "pydantic")


class ReleaseArtifactSource(Protocol):
    """Supplies real release artifact/package metadata at cut-version time."""

    def current_metadata(self) -> ReleaseArtifactMetadata: ...


def _lock_digest(package_versions: dict[str, str]) -> str | None:
    if not package_versions:
        return None
    canonical = json.dumps(package_versions, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class InstalledPackageArtifactSource:
    """Reads genuinely installed package versions via ``importlib.metadata``.

    Every value here reflects the real installed distribution (or is
    honestly absent when not installed) — never a version number derived
    from a display sequence.
    """

    def __init__(
        self,
        *,
        source_revision: str | None = None,
        tracked: tuple[str, ...] = TRACKED_DISTRIBUTIONS,
    ) -> None:
        self._source_revision = source_revision
        self._tracked = tracked

    def current_metadata(self) -> ReleaseArtifactMetadata:
        versions: dict[str, str] = {}
        for name in self._tracked:
            try:
                versions[name] = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                continue
        return ReleaseArtifactMetadata(
            package_versions=versions,
            lock_digest=_lock_digest(versions),
            framework_version=versions.get("fastapi", "unknown"),
            hosting_package_version=versions.get("research-assistant-api", "unknown"),
            source_revision=self._source_revision,
        )


class StaticReleaseArtifactSource:
    """Wraps a fixed, externally-supplied snapshot.

    Test-only: exercises ``ReleaseService`` against deterministic,
    explicitly-authored metadata without depending on whatever happens to be
    installed in the current environment.
    """

    def __init__(self, metadata: ReleaseArtifactMetadata) -> None:
        self._metadata = metadata

    def current_metadata(self) -> ReleaseArtifactMetadata:
        return self._metadata
