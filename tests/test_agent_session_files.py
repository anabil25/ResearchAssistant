"""The agent-side half of chat attachments.

Chat uploads land in the hosted session's persistent ``$HOME``. These tools are
how an agent actually reads them, and the boundary that matters is that a
model-supplied path cannot walk out of that directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from shared import tools as agent_tools
from shared.profiles import get_manifest
from shared.tools import tools_for_profile


@pytest.fixture
def session_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(agent_tools, "SESSION_FILES_ROOT", home)
    return home


def invoke(tool: object, **kwargs: object) -> str:
    """Call through the Agent Framework ``@tool`` wrapper to the function."""
    target = getattr(tool, "func", tool)
    return str(target(**kwargs))


class TestToolWiring:
    @pytest.mark.parametrize("profile_id", ["literature", "grant", "matching"])
    def test_the_chat_studios_can_read_their_session_files(self, profile_id: str) -> None:
        names = {getattr(tool, "name", None) for tool in tools_for_profile(get_manifest(profile_id))}
        assert names == {"list_session_files", "read_session_file"}

    @pytest.mark.parametrize("profile_id", ["institution", "coordinator"])
    def test_agents_without_a_chat_surface_get_no_file_tools(self, profile_id: str) -> None:
        assert tools_for_profile(get_manifest(profile_id)) == []

    def test_dataset_still_resolves_its_governed_toolbox_not_local_tools(self) -> None:
        # Dataset compute stays on the approval-gated `dataset.compute` binding.
        from shared.errors import ConfigurationError

        with pytest.raises(ConfigurationError, match="Toolbox"):
            tools_for_profile(get_manifest("dataset"))


class TestListing:
    def test_an_empty_session_says_so_rather_than_returning_nothing(
        self, session_home: Path
    ) -> None:
        assert "No files" in invoke(agent_tools.list_session_files)

    def test_attached_files_are_listed_with_their_sizes(self, session_home: Path) -> None:
        csv = session_home / "outcomes.csv"
        csv.write_text("a,b\n1,2\n", encoding="utf-8")
        (session_home / "nested").mkdir()
        (session_home / "nested" / "notes.md").write_text("# notes", encoding="utf-8")

        listing = invoke(agent_tools.list_session_files)

        assert f"outcomes.csv ({csv.stat().st_size} bytes)" in listing
        assert "nested/notes.md (7 bytes)" in listing


class TestReading:
    def test_an_attached_file_is_returned_verbatim(self, session_home: Path) -> None:
        (session_home / "outcomes.csv").write_text("a,b\n1,2\n", encoding="utf-8", newline="")
        assert invoke(agent_tools.read_session_file, path="outcomes.csv") == "a,b\n1,2\n"

    def test_a_leading_slash_is_treated_as_session_relative(self, session_home: Path) -> None:
        (session_home / "notes.txt").write_text("hello", encoding="utf-8")
        assert invoke(agent_tools.read_session_file, path="/notes.txt") == "hello"

    @pytest.mark.parametrize(
        "path",
        ["../secret.txt", "../../secret.txt", "nested/../../secret.txt"],
    )
    def test_a_path_cannot_escape_the_session_home_directory(
        self, session_home: Path, path: str
    ) -> None:
        (session_home.parent / "secret.txt").write_text("classified", encoding="utf-8")
        (session_home / "nested").mkdir()

        result = invoke(agent_tools.read_session_file, path=path)

        assert "classified" not in result
        assert "confined to the session home directory" in result

    def test_a_missing_file_points_the_agent_at_the_listing_tool(
        self, session_home: Path
    ) -> None:
        result = invoke(agent_tools.read_session_file, path="absent.csv")
        assert "list_session_files" in result

    def test_a_file_over_the_read_limit_is_refused_rather_than_truncated(
        self, session_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(agent_tools, "MAX_SESSION_FILE_BYTES", 4)
        (session_home / "big.csv").write_text("far too long", encoding="utf-8")

        result = invoke(agent_tools.read_session_file, path="big.csv")

        assert "far too long" not in result
        assert "read limit" in result

    def test_undecodable_bytes_do_not_crash_the_turn(self, session_home: Path) -> None:
        (session_home / "binary.txt").write_bytes(b"\xff\xfe ok")
        assert "ok" in invoke(agent_tools.read_session_file, path="binary.txt")


def test_every_agent_is_told_attachments_are_untrusted() -> None:
    for profile_id in ("literature", "grant", "matching", "dataset"):
        instructions = get_manifest(profile_id).instructions
        assert "session home directory" in instructions
        assert "untrusted data" in instructions
