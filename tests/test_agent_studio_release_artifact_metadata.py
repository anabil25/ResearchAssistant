# mypy: disable-error-code=import-untyped
"""Direct unit tests for ``release_artifact_metadata.py``.

These exercise both ``ReleaseArtifactSource`` implementations directly:
real installed-package discovery (including the honest "not installed"
skip branch) and the fixed, externally-supplied test snapshot.
"""

from __future__ import annotations

from research_assistant_api.agent_studio.models import ReleaseArtifactMetadata
from research_assistant_api.agent_studio.release_artifact_metadata import (
    InstalledPackageArtifactSource,
    StaticReleaseArtifactSource,
)


def test_installed_package_source_reads_real_versions_for_known_distributions() -> None:
    source = InstalledPackageArtifactSource(tracked=("fastapi", "pydantic"))

    metadata = source.current_metadata()

    assert set(metadata.package_versions) == {"fastapi", "pydantic"}
    assert metadata.framework_version == metadata.package_versions["fastapi"]
    # ``research-assistant-api`` was not tracked in this call, so the hosting
    # package version genuinely is not known here.
    assert metadata.hosting_package_version == "unknown"
    assert metadata.lock_digest is not None
    assert metadata.lock_digest.startswith("sha256:")
    assert metadata.source_revision is None


def test_installed_package_source_skips_distributions_that_are_not_installed() -> None:
    source = InstalledPackageArtifactSource(tracked=("definitely-not-a-real-package-xyz123",))

    metadata = source.current_metadata()

    assert metadata.package_versions == {}
    assert metadata.lock_digest is None
    assert metadata.framework_version == "unknown"
    assert metadata.hosting_package_version == "unknown"


def test_installed_package_source_records_explicit_source_revision() -> None:
    source = InstalledPackageArtifactSource(tracked=(), source_revision="abc123")

    metadata = source.current_metadata()

    assert metadata.source_revision == "abc123"
    assert metadata.package_versions == {}
    assert metadata.lock_digest is None


def test_static_release_artifact_source_returns_the_supplied_snapshot() -> None:
    fixed = ReleaseArtifactMetadata(
        package_versions={"fastapi": "1.2.3"},
        lock_digest="sha256:deadbeef",
        framework_version="1.2.3",
        hosting_package_version="9.9.9",
        source_revision="deadbeef",
    )
    source = StaticReleaseArtifactSource(fixed)

    assert source.current_metadata() is fixed
    # Calling repeatedly must return the exact same static snapshot, never
    # re-deriving or mutating it.
    assert source.current_metadata() == fixed
