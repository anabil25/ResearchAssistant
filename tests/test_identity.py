"""Tests for ``research_assistant_api.identity``'s group-overage detection.

Covers ``_has_group_overage``'s two signal paths -- the Entra ID
``_claim_names``/``groups`` indirection, and the Container Apps/App Service
EasyAuth ``hasgroups`` claim (including its "skip non-dict claim entries"
defensive guard) -- since these feed the fail-closed group-overage flag that
``research_assistant_api.agent_studio.authz`` relies on to avoid treating a
truncated ``groups`` claim as authoritative for a membership denial.
"""

from __future__ import annotations

from research_assistant_api.identity import _has_group_overage


def test_has_group_overage_detects_claim_names_indirection() -> None:
    payload = {"_claim_names": {"groups": "src1"}, "claims": []}
    assert _has_group_overage(payload) is True


def test_has_group_overage_false_when_claim_names_omits_groups() -> None:
    payload = {"_claim_names": {"other": "src1"}, "claims": []}
    assert _has_group_overage(payload) is False


def test_has_group_overage_detects_hasgroups_claim() -> None:
    payload = {"claims": [{"typ": "hasgroups", "val": "true"}]}
    assert _has_group_overage(payload) is True


def test_has_group_overage_skips_non_dict_claim_entries() -> None:
    # A malformed/non-dict claim entry must be skipped rather than raising,
    # and a well-formed "hasgroups" entry appearing after it must still be
    # detected.
    payload = {"claims": ["not-a-claim", {"typ": "hasgroups", "val": "true"}]}
    assert _has_group_overage(payload) is True


def test_has_group_overage_false_when_no_signal_present() -> None:
    payload = {"claims": [{"typ": "groups", "val": "researchers"}]}
    assert _has_group_overage(payload) is False
