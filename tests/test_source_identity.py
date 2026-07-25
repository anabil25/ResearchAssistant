from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PurePosixPath

import pytest
from shared.errors import ConfigurationError
from shared.source_identity import (
    BakedSourceTreeManifest,
    load_baked_source_tree_manifest,
)

from scripts import build_agent_source_tree as source_build
from scripts.build_agent_source_tree import (
    SourceIdentityBuildError,
    SourceTreeManifest,
    build_source_tree_manifest,
    committed_source_entries,
    main,
    source_tree_digest,
    validate_worktree_matches_commit,
    worktree_source_entries,
    write_source_tree_manifest,
)


def _run_git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ("git", "-C", str(repo), *arguments),
        check=True,
        capture_output=True,
    )


def _initialize_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _run_git(repo, "init", "--quiet")
    _run_git(repo, "config", "user.email", "source-identity@example.test")
    _run_git(repo, "config", "user.name", "Source Identity Test")
    return repo


def _commit(repo: Path, message: str) -> None:
    _run_git(repo, "-c", "core.autocrlf=false", "add", "--all")
    _run_git(repo, "commit", "--quiet", "-m", message)


def _write_agent(repo: Path, content: bytes, name: str = "main.py") -> Path:
    destination = repo / "agents" / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    return destination


def _manifest_payload(**overrides: object) -> dict[str, object]:
    identity: dict[str, object] = {
        "entry_count": 1,
        "inclusion_policy_version": "1",
        "producer": "research-assistant.git-source-tree",
        "schema_version": "1",
        "source_commit": "a" * 40,
        "source_root": "agents",
        "source_tree": "b" * 40,
        "source_tree_digest": source_tree_digest((("main.py", b"VALUE = 1\n"),)),
    }
    requested_manifest_digest = overrides.pop("source_manifest_digest", None)
    identity.update(overrides)
    return {
        **identity,
        "source_manifest_digest": (
            requested_manifest_digest
            if requested_manifest_digest is not None
            else source_build._canonical_digest(identity)
        ),
    }


def test_source_hash_normalizes_newlines_and_preserves_final_newline() -> None:
    lf = source_tree_digest((("main.py", b"first\nsecond\n"),))
    crlf = source_tree_digest((("main.py", b"first\r\nsecond\r\n"),))
    bare_cr = source_tree_digest((("main.py", b"first\rsecond\r"),))
    assert lf == crlf == bare_cr
    assert source_tree_digest((("main.py", b"first\nsecond"),)) != lf
    assert source_tree_digest((("main.py", b"first\nchanged\n"),)) != lf


def test_committed_source_tracks_content_final_newline_add_remove_and_rename(tmp_path: Path) -> None:
    repo = _initialize_repo(tmp_path)
    source = _write_agent(repo, b"VALUE = 1\n")
    _commit(repo, "baseline")
    baseline = build_source_tree_manifest(repo).source_tree_digest

    source.write_bytes(b"VALUE = 2\n")
    _commit(repo, "content")
    content_changed = build_source_tree_manifest(repo).source_tree_digest

    source.write_bytes(b"VALUE = 2")
    _commit(repo, "final newline")
    final_newline_changed = build_source_tree_manifest(repo).source_tree_digest

    support = _write_agent(repo, b"SUPPORT = True\n", "support.py")
    _commit(repo, "add")
    added = build_source_tree_manifest(repo).source_tree_digest

    support.unlink()
    _commit(repo, "remove")
    removed = build_source_tree_manifest(repo).source_tree_digest

    source.rename(source.with_name("renamed.py"))
    _commit(repo, "rename")
    renamed = build_source_tree_manifest(repo).source_tree_digest

    assert baseline != content_changed
    assert content_changed != final_newline_changed
    assert final_newline_changed != added
    assert added != removed
    assert removed != renamed


def test_source_hash_rejects_invalid_content_paths_and_collisions() -> None:
    with pytest.raises(SourceIdentityBuildError, match="valid UTF-8"):
        source_tree_digest((("main.py", b"\xff"),))
    with pytest.raises(SourceIdentityBuildError, match="non-empty and relative"):
        source_tree_digest((("", b"value"),))
    with pytest.raises(SourceIdentityBuildError, match="non-empty and relative"):
        source_tree_digest((("/main.py", b"value"),))
    with pytest.raises(SourceIdentityBuildError, match="not canonical"):
        source_tree_digest((("folder//main.py", b"value"),))
    with pytest.raises(SourceIdentityBuildError, match="NFC normalization"):
        source_tree_digest(
            (
                ("caf\u00e9.py", b"one"),
                ("cafe\u0301.py", b"two"),
            )
        )
    with pytest.raises(SourceIdentityBuildError, match="case folding"):
        source_tree_digest(
            (
                ("Agent.py", b"one"),
                ("agent.py", b"two"),
            )
        )
    with pytest.raises(SourceIdentityBuildError, match="empty"):
        source_tree_digest(())


def test_committed_source_ignores_worktree_and_non_source_files(tmp_path: Path) -> None:
    repo = _initialize_repo(tmp_path)
    _write_agent(repo, b"VALUE = 1\r\n")
    _write_agent(repo, b"IGNORED = True\n", "tests/ignored.py")
    (repo / "agents" / "notes.md").write_text("ignored", encoding="utf-8")
    _commit(repo, "baseline")

    first = build_source_tree_manifest(repo)
    _write_agent(repo, b"UNTRACKED = True\n", "scratch.py")
    (repo / "agents" / "notes.md").write_text("changed but irrelevant", encoding="utf-8")
    second = build_source_tree_manifest(repo)

    assert second.source_tree_digest == first.source_tree_digest
    assert second.source_commit == first.source_commit
    assert second.source_tree == first.source_tree
    assert second.entry_count == 1
    assert second.payload()["producer"] == "research-assistant.git-source-tree"
    assert second.inclusion_policy_version == "1"
    assert second.source_manifest_digest == source_build._canonical_digest(
        second.identity_payload()
    )


def test_predeploy_rejects_identity_eligible_worktree_drift(tmp_path: Path) -> None:
    repo = _initialize_repo(tmp_path)
    source = _write_agent(repo, b"VALUE = 1\n")
    _commit(repo, "baseline")

    source.write_bytes(b"VALUE = 1\r\n")
    validate_worktree_matches_commit(repo)
    ignored = _write_agent(repo, b"IGNORED = True\n", ".venv/ignored.py")
    (repo / "agents" / "notes.md").write_text("not packaged", encoding="utf-8")
    validate_worktree_matches_commit(repo)
    assert worktree_source_entries(repo) == (("main.py", b"VALUE = 1\r\n"),)

    scratch = _write_agent(repo, b"UNTRACKED = True\n", "scratch.py")
    with pytest.raises(SourceIdentityBuildError, match="differs"):
        validate_worktree_matches_commit(repo)
    scratch.unlink()

    source.write_bytes(b"VALUE = 2\n")
    with pytest.raises(SourceIdentityBuildError, match="differs"):
        validate_worktree_matches_commit(repo)
    ignored.unlink()


def test_main_invokes_the_drift_check_so_drifted_bytes_cannot_ship(
    tmp_path: Path,
) -> None:
    """N1: assert the WIRING, not just the function.

    ``validate_worktree_matches_commit`` is well covered above, but every one of
    those cases calls it directly. Deleting the call from ``main()`` leaves the
    whole suite green while producing this chain: predeploy exits 0, a manifest is
    written, the runtime accepts it, and drifted bytes ship under a committed
    source identity -- silent at every layer expected to catch it, with the
    documentation still asserting the control.

    This is the assertion on the sole enforcement point, so it drifts a real
    identity-eligible file and requires ``main()`` itself to fail.
    """

    repo = _initialize_repo(tmp_path)
    source = _write_agent(repo, b"VALUE = 1\n")
    _commit(repo, "baseline")
    output = repo / ".release" / "source-tree.json"

    # Clean worktree: main() succeeds and writes the manifest.
    assert main(["--repo-root", str(repo), "--output", str(output)]) == 0
    assert output.exists()
    output.unlink()

    # Drifted identity-eligible byte: main() must refuse, and write nothing.
    # It propagates rather than returning a code -- an uncaught raise is what
    # gives `python -m scripts.build_agent_source_tree` its non-zero exit, so the
    # raise IS the predeploy failure. Deleting the call from main() makes this
    # pass silently, which is the whole point of the assertion.
    source.write_bytes(b"VALUE = 2\n")
    with pytest.raises(SourceIdentityBuildError, match="differs"):
        main(["--repo-root", str(repo), "--output", str(output)])
    assert not output.exists()


def test_committed_source_digest_is_checkout_newline_independent(tmp_path: Path) -> None:
    repo = _initialize_repo(tmp_path)
    source = _write_agent(repo, b"first\nsecond\n")
    _commit(repo, "lf")
    lf = build_source_tree_manifest(repo).source_tree_digest

    source.write_bytes(b"first\r\nsecond\r\n")
    _commit(repo, "crlf")
    crlf = build_source_tree_manifest(repo).source_tree_digest

    source.write_bytes(b"first\rsecond\r")
    _commit(repo, "cr")
    bare_cr = build_source_tree_manifest(repo).source_tree_digest

    assert lf == crlf == bare_cr


def test_committed_source_reports_git_and_path_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SourceIdentityBuildError, match="Git command failed"):
        build_source_tree_manifest(tmp_path)
    with pytest.raises(SourceIdentityBuildError, match="not valid UTF-8"):
        source_build._decode_git_path(b"\xff")

    responses = iter((b"a" * 40 + b"\n", b"outside.py\0"))
    monkeypatch.setattr(source_build, "_git", lambda *_args: next(responses))
    with pytest.raises(SourceIdentityBuildError, match="escaped"):
        committed_source_entries(tmp_path, source_root=PurePosixPath("agents"))


def test_manifest_write_load_and_cli_are_deterministic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _initialize_repo(tmp_path)
    _write_agent(repo, b"VALUE = 1\n")
    _commit(repo, "source")
    manifest = build_source_tree_manifest(repo)
    output = tmp_path / "nested" / "source-tree.json"
    write_source_tree_manifest(manifest, output)

    loaded = load_baked_source_tree_manifest(output)
    assert loaded.source_tree_digest == manifest.source_tree_digest
    assert loaded.source_commit == manifest.source_commit
    assert loaded.source_tree == manifest.source_tree
    assert output.read_bytes().endswith(b"\n")

    relative_output = Path("agents/.release/test-source-tree.json")
    assert main(
        (
            "--repo-root",
            str(repo),
            "--output",
            str(relative_output),
        )
    ) == 0
    assert (repo / relative_output).is_file()
    assert manifest.source_tree_digest in capsys.readouterr().out

    absolute_output = tmp_path / "absolute.json"
    assert main(
        (
            "--repo-root",
            str(repo),
            "--output",
            str(absolute_output),
        )
    ) == 0
    assert absolute_output.is_file()


def test_manifest_write_cleans_up_failed_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = {
        "entry_count": 1,
        "inclusion_policy_version": "1",
        "producer": "research-assistant.git-source-tree",
        "schema_version": "1",
        "source_commit": "a" * 40,
        "source_root": "agents",
        "source_tree": "b" * 40,
        "source_tree_digest": "c" * 64,
    }
    manifest = SourceTreeManifest(
        schema_version="1",
        inclusion_policy_version="1",
        producer="research-assistant.git-source-tree",
        source_commit="a" * 40,
        source_tree="b" * 40,
        source_root="agents",
        source_tree_digest="c" * 64,
        entry_count=1,
        source_manifest_digest=source_build._canonical_digest(identity),
    )
    output = tmp_path / "source-tree.json"

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        write_source_tree_manifest(manifest, output)
    assert list(tmp_path.iterdir()) == []


def test_baked_manifest_loader_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps(_manifest_payload()), encoding="utf-8")
    assert load_baked_source_tree_manifest(valid).entry_count == 1

    monkeypatch.setattr("shared.source_identity.SOURCE_MANIFEST_PATH", valid)
    assert load_baked_source_tree_manifest().source_root == "agents"

    missing = tmp_path / "missing.json"
    with pytest.raises(
        ConfigurationError,
        match=r"scripts/build_agent_source_tree\.py",
    ) as exc:
        load_baked_source_tree_manifest(missing)
    assert exc.value.context["producer"] == "scripts/build_agent_source_tree.py"

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(_manifest_payload(entry_count=0)), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="missing or invalid"):
        load_baked_source_tree_manifest(invalid)

    coerced = tmp_path / "coerced.json"
    coerced.write_text(json.dumps(_manifest_payload(entry_count="1")), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="missing or invalid"):
        load_baked_source_tree_manifest(coerced)

    tampered = tmp_path / "tampered.json"
    tampered.write_text(
        json.dumps(
            _manifest_payload(
                source_manifest_digest="0" * 64,
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="canonical digest"):
        load_baked_source_tree_manifest(tampered)

    invalid_utf8 = tmp_path / "invalid-utf8.json"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError, match="missing or invalid"):
        load_baked_source_tree_manifest(invalid_utf8)

    with pytest.raises(ValueError):
        BakedSourceTreeManifest.model_validate(_manifest_payload(source_commit="not-a-commit"))
