from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

SOURCE_MANIFEST_SCHEMA_VERSION = "1"
SOURCE_INCLUSION_POLICY_VERSION = "1"
SOURCE_MANIFEST_PRODUCER = "research-assistant.git-source-tree"
DEFAULT_SOURCE_ROOT = PurePosixPath("agents")
DEFAULT_OUTPUT = Path("agents/.release/source-tree.json")
IGNORED_PACKAGE_DIRECTORIES = frozenset(
    {".foundry", ".release", ".venv", "__pycache__", "evals", "tests"}
)


class SourceIdentityBuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class SourceTreeManifest:
    schema_version: str
    inclusion_policy_version: str
    producer: str
    source_commit: str
    source_tree: str
    source_root: str
    source_tree_digest: str
    entry_count: int
    source_manifest_digest: str

    def identity_payload(self) -> dict[str, str | int]:
        return {
            "entry_count": self.entry_count,
            "inclusion_policy_version": self.inclusion_policy_version,
            "producer": self.producer,
            "schema_version": self.schema_version,
            "source_commit": self.source_commit,
            "source_root": self.source_root,
            "source_tree": self.source_tree,
            "source_tree_digest": self.source_tree_digest,
        }

    def payload(self) -> dict[str, str | int]:
        return {
            **self.identity_payload(),
            "source_manifest_digest": self.source_manifest_digest,
        }


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_path(path: str) -> str:
    if not path or path.startswith("/"):
        raise SourceIdentityBuildError("Source paths must be non-empty and relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SourceIdentityBuildError(f"Source path is not canonical: {path!r}")
    return unicodedata.normalize("NFC", PurePosixPath(*parts).as_posix())


def _normalized_source(content: bytes, *, path: str) -> bytes:
    try:
        with io.TextIOWrapper(
            io.BytesIO(content),
            encoding="utf-8",
            errors="strict",
            newline=None,
        ) as stream:
            return stream.read().encode("utf-8")
    except UnicodeError as exc:
        raise SourceIdentityBuildError(f"Source file is not valid UTF-8: {path}") from exc


def canonical_source_entries(entries: Iterable[tuple[str, bytes]]) -> tuple[tuple[str, str], ...]:
    canonical: dict[str, str] = {}
    casefolded: dict[str, str] = {}
    result: list[tuple[str, str]] = []
    for original_path, content in entries:
        path = _canonical_path(original_path)
        previous = canonical.get(path)
        if previous is not None:
            raise SourceIdentityBuildError(
                f"Source paths collide after NFC normalization: {previous!r} and {original_path!r}"
            )
        folded = path.casefold()
        previous_folded = casefolded.get(folded)
        if previous_folded is not None:
            raise SourceIdentityBuildError(
                f"Source paths collide after case folding: {previous_folded!r} and {original_path!r}"
            )
        canonical[path] = original_path
        casefolded[folded] = original_path
        result.append((path, _normalized_source(content, path=path).hex()))
    result.sort(key=lambda item: item[0])
    return tuple(result)


def source_tree_digest(entries: Iterable[tuple[str, bytes]]) -> str:
    canonical_entries = canonical_source_entries(entries)
    if not canonical_entries:
        raise SourceIdentityBuildError("The committed agent source tree is empty")
    return _canonical_digest(canonical_entries)


def _git(repo_root: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(repo_root), *arguments),
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SourceIdentityBuildError(f"Git command failed: {message or arguments[0]}")
    return completed.stdout


def _decode_git_path(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceIdentityBuildError("Tracked source path is not valid UTF-8") from exc


def committed_source_entries(
    repo_root: Path,
    *,
    revision: str = "HEAD",
    source_root: PurePosixPath = DEFAULT_SOURCE_ROOT,
) -> tuple[str, tuple[tuple[str, bytes], ...]]:
    root = repo_root.resolve()
    commit = _git(root, "rev-parse", "--verify", f"{revision}^{{commit}}").decode("ascii").strip()
    prefix = f"{source_root.as_posix()}/"
    names = _git(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--name-only",
        commit,
        "--",
        source_root.as_posix(),
    )
    entries: list[tuple[str, bytes]] = []
    for raw_name in names.split(b"\0"):
        if not raw_name:
            continue
        tracked_path = _decode_git_path(raw_name)
        if not tracked_path.startswith(prefix):
            raise SourceIdentityBuildError(f"Tracked source escaped the configured root: {tracked_path!r}")
        relative_path = tracked_path.removeprefix(prefix)
        parsed = PurePosixPath(relative_path)
        if any(part in IGNORED_PACKAGE_DIRECTORIES for part in parsed.parts):
            continue
        if parsed.suffix != ".py" and parsed.name != "requirements.txt":
            continue
        content = _git(root, "cat-file", "blob", f"{commit}:{tracked_path}")
        entries.append((relative_path, content))
    return commit, tuple(entries)


def worktree_source_entries(
    repo_root: Path,
    *,
    source_root: PurePosixPath = DEFAULT_SOURCE_ROOT,
) -> tuple[tuple[str, bytes], ...]:
    root = repo_root.resolve() / Path(*source_root.parts)
    entries: list[tuple[str, bytes]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in IGNORED_PACKAGE_DIRECTORIES for part in relative.parts):
            continue
        if not path.is_file() or (path.suffix != ".py" and path.name != "requirements.txt"):
            continue
        entries.append((relative.as_posix(), path.read_bytes()))
    return tuple(entries)


def validate_worktree_matches_commit(
    repo_root: Path,
    *,
    revision: str = "HEAD",
    source_root: PurePosixPath = DEFAULT_SOURCE_ROOT,
) -> None:
    _, committed = committed_source_entries(
        repo_root,
        revision=revision,
        source_root=source_root,
    )
    worktree = worktree_source_entries(repo_root, source_root=source_root)
    if canonical_source_entries(worktree) != canonical_source_entries(committed):
        raise SourceIdentityBuildError(
            "Identity-eligible agent source (.py + requirements.txt) differs from "
            "committed content"
        )


def build_source_tree_manifest(
    repo_root: Path,
    *,
    revision: str = "HEAD",
    source_root: PurePosixPath = DEFAULT_SOURCE_ROOT,
) -> SourceTreeManifest:
    commit, entries = committed_source_entries(
        repo_root,
        revision=revision,
        source_root=source_root,
    )
    source_tree = _git(
        repo_root.resolve(),
        "rev-parse",
        "--verify",
        f"{commit}:{source_root.as_posix()}",
    ).decode("ascii").strip()
    tree_digest = source_tree_digest(entries)
    values: dict[str, str | int] = {
        "schema_version": SOURCE_MANIFEST_SCHEMA_VERSION,
        "inclusion_policy_version": SOURCE_INCLUSION_POLICY_VERSION,
        "producer": SOURCE_MANIFEST_PRODUCER,
        "source_commit": commit,
        "source_tree": source_tree,
        "source_root": source_root.as_posix(),
        "source_tree_digest": tree_digest,
        "entry_count": len(entries),
    }
    return SourceTreeManifest(
        schema_version=SOURCE_MANIFEST_SCHEMA_VERSION,
        inclusion_policy_version=SOURCE_INCLUSION_POLICY_VERSION,
        producer=SOURCE_MANIFEST_PRODUCER,
        source_commit=commit,
        source_tree=source_tree,
        source_root=source_root.as_posix(),
        source_tree_digest=tree_digest,
        entry_count=len(entries),
        source_manifest_digest=_canonical_digest(values),
    )


def write_source_tree_manifest(manifest: SourceTreeManifest, output: Path) -> None:
    destination = output.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            manifest.payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the immutable Hosted Agent source identity from committed Git blobs."
    )
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--source-root", type=PurePosixPath, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    repo_root = arguments.repo_root.resolve()
    output = arguments.output
    if not output.is_absolute():
        output = repo_root / output
    manifest = build_source_tree_manifest(
        repo_root,
        revision=arguments.revision,
        source_root=arguments.source_root,
    )
    validate_worktree_matches_commit(
        repo_root,
        revision=arguments.revision,
        source_root=arguments.source_root,
    )
    write_source_tree_manifest(manifest, output)
    print(manifest.source_tree_digest)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the predeploy process
    raise SystemExit(main())
