"""Unit tests for the Agent Studio project-membership authorization port.

Covers the domain ``ProjectMembershipResolver`` protocol, the default
``ClaimsGroupMembershipResolver`` adapter, and the fail-closed
``enforce_project_membership`` wrapper -- independent of any FastAPI/router
wiring (see ``test_agent_studio_router.py`` for the integration-level
coverage of how routes consume this port).
"""

from __future__ import annotations

import unicodedata

import pytest
from research_assistant_api.agent_studio.authz import (
    ClaimsGroupMembershipResolver,
    DemoSandboxMembershipPolicy,
    MembershipCheckRequest,
    MembershipDecision,
    MembershipOutcome,
    ProjectMembershipError,
    ProjectMembershipResolver,
    enforce_project_membership,
)


def _request(
    *,
    project_id: str = "proj-1",
    claimed_groups: tuple[str, ...] = (),
    groups_known_complete: bool = True,
    tenant_id: str = "tenant-1",
    principal_id: str = "user-1",
) -> MembershipCheckRequest:
    return MembershipCheckRequest(
        tenant_id=tenant_id,
        project_id=project_id,
        principal_id=principal_id,
        claimed_groups=claimed_groups,
        groups_known_complete=groups_known_complete,
    )


def test_claims_resolver_grants_membership_when_group_present_and_complete() -> None:
    resolver = ClaimsGroupMembershipResolver()
    decision = resolver.resolve_membership(
        _request(project_id="proj-1", claimed_groups=("project:proj-1",), groups_known_complete=True)
    )
    assert decision.outcome is MembershipOutcome.MEMBER


def test_claims_resolver_denies_membership_when_group_absent_and_complete() -> None:
    resolver = ClaimsGroupMembershipResolver()
    decision = resolver.resolve_membership(
        _request(project_id="proj-1", claimed_groups=("project:other-project",), groups_known_complete=True)
    )
    assert decision.outcome is MembershipOutcome.NOT_MEMBER
    assert decision.reason is not None
    assert "proj-1" in decision.reason


def test_claims_resolver_does_not_leak_membership_across_projects() -> None:
    """A group granting membership in one project must never authorize a
    different project -- the convention is per-project, not tenant-wide."""
    resolver = ClaimsGroupMembershipResolver()
    decision = resolver.resolve_membership(
        _request(
            project_id="proj-target",
            claimed_groups=("project:proj-other",),
            groups_known_complete=True,
        )
    )
    assert decision.outcome is MembershipOutcome.NOT_MEMBER


def test_claims_resolver_fails_closed_on_group_overage_even_absent_target_group() -> None:
    resolver = ClaimsGroupMembershipResolver()
    decision = resolver.resolve_membership(
        _request(project_id="proj-1", claimed_groups=(), groups_known_complete=False)
    )
    assert decision.outcome is MembershipOutcome.UNAVAILABLE
    assert decision.reason is not None


def test_claims_resolver_fails_closed_on_group_overage_even_when_target_group_present() -> None:
    """Regression: overage must be a blanket "cannot verify" signal, not one
    that's only consulted on absence. A resolver that trusted a present
    entry while distrusting an absent one would still be coupling a
    security decision to a claims list it has already declared incomplete
    for exactly this kind of case."""
    resolver = ClaimsGroupMembershipResolver()
    decision = resolver.resolve_membership(
        _request(project_id="proj-1", claimed_groups=("project:proj-1",), groups_known_complete=False)
    )
    assert decision.outcome is MembershipOutcome.UNAVAILABLE


def test_claims_resolver_fails_closed_when_no_membership_source_asserted() -> None:
    """A missing/absent claim source (not merely truncation) must resolve
    the same way as overage: UNAVAILABLE, never a silent NOT_MEMBER."""
    resolver = ClaimsGroupMembershipResolver()
    decision = resolver.resolve_membership(
        _request(project_id="proj-1", claimed_groups=(), groups_known_complete=False)
    )
    assert decision.outcome is MembershipOutcome.UNAVAILABLE


def test_claims_resolver_normalizes_unicode_group_names() -> None:
    """A group claim encoded in NFD (decomposed) Unicode must still match a
    differently-encoded (NFC/precomposed) project id -- otherwise an
    identity provider or client library that re-encodes strings could
    spoof/evade the membership convention purely through Unicode form."""
    project_id = unicodedata.normalize("NFC", "cafe\u0301-project")  # precomposed "café-project"
    decomposed_group = "project:" + unicodedata.normalize("NFD", "cafe\u0301-project")
    resolver = ClaimsGroupMembershipResolver()
    decision = resolver.resolve_membership(
        _request(project_id=project_id, claimed_groups=(decomposed_group,), groups_known_complete=True)
    )
    assert decision.outcome is MembershipOutcome.MEMBER


def test_claims_resolver_normalization_does_not_broaden_case_sensitivity() -> None:
    """Unicode normalization must not become an accidental case-fold: a
    differently-cased group name is a genuinely different string and must
    not match."""
    resolver = ClaimsGroupMembershipResolver()
    decision = resolver.resolve_membership(
        _request(project_id="proj-1", claimed_groups=("Project:PROJ-1",), groups_known_complete=True)
    )
    assert decision.outcome is MembershipOutcome.NOT_MEMBER


def test_enforce_project_membership_passes_through_on_member() -> None:
    resolver = ClaimsGroupMembershipResolver()
    request = _request(project_id="proj-1", claimed_groups=("project:proj-1",), groups_known_complete=True)
    enforce_project_membership(resolver, request)  # must not raise


def test_enforce_project_membership_raises_on_not_member() -> None:
    resolver = ClaimsGroupMembershipResolver()
    request = _request(project_id="proj-1", claimed_groups=(), groups_known_complete=True)
    with pytest.raises(ProjectMembershipError) as exc_info:
        enforce_project_membership(resolver, request)
    assert exc_info.value.decision.outcome is MembershipOutcome.NOT_MEMBER


def test_enforce_project_membership_raises_on_unavailable() -> None:
    resolver = ClaimsGroupMembershipResolver()
    request = _request(project_id="proj-1", claimed_groups=(), groups_known_complete=False)
    with pytest.raises(ProjectMembershipError) as exc_info:
        enforce_project_membership(resolver, request)
    assert exc_info.value.decision.outcome is MembershipOutcome.UNAVAILABLE


def test_project_membership_error_message_falls_back_when_no_reason() -> None:
    error = ProjectMembershipError(MembershipDecision(outcome=MembershipOutcome.UNAVAILABLE, reason=None))
    assert str(error) == "Project membership could not be confirmed."


def test_claims_group_membership_resolver_satisfies_protocol() -> None:
    """Confirms the concrete adapter structurally implements the domain
    Protocol -- any future adapter (e.g. Graph-backed) can be substituted
    wherever a ``ProjectMembershipResolver`` is expected."""
    resolver: ProjectMembershipResolver = ClaimsGroupMembershipResolver()
    assert isinstance(
        resolver.resolve_membership(_request(claimed_groups=("project:proj-1",))),
        MembershipDecision,
    )


class _AlwaysUnavailableResolver:
    """Minimal stand-in for a future adapter, e.g. one backed by a directory
    lookup service that is temporarily unreachable."""

    def resolve_membership(self, request: MembershipCheckRequest) -> MembershipDecision:
        return MembershipDecision(outcome=MembershipOutcome.UNAVAILABLE, reason="directory unreachable")


def test_enforce_project_membership_works_with_alternate_resolver_implementation() -> None:
    """Any object satisfying the Protocol (not just ``ClaimsGroupMembershipResolver``)
    can be passed to ``enforce_project_membership`` -- this is the point of the
    port: a future Graph/app-role-membership adapter needs no other code change."""
    resolver: ProjectMembershipResolver = _AlwaysUnavailableResolver()
    with pytest.raises(ProjectMembershipError):
        enforce_project_membership(resolver, _request())


def test_demo_sandbox_membership_policy_grants_member_unconditionally() -> None:
    """The explicit demo sandbox policy always grants MEMBER -- it performs
    no verification of its own (see its docstring for why: the caller is
    responsible for only routing to it once ``IdentityContext.source`` is
    already confirmed to be the demo sandbox source)."""
    policy = DemoSandboxMembershipPolicy()
    decision = policy.resolve_membership(
        _request(project_id="any-project-at-all", claimed_groups=(), groups_known_complete=False)
    )
    assert decision.outcome is MembershipOutcome.MEMBER
    assert decision.reason is not None


def test_demo_sandbox_membership_policy_grants_member_regardless_of_project() -> None:
    """Distinct requests for different projects/tenants/principals must all
    still resolve MEMBER -- the policy is intentionally project-agnostic."""
    policy = DemoSandboxMembershipPolicy()
    for project_id in ("proj-a", "proj-b", "platform-reserved-lookalike"):
        decision = policy.resolve_membership(_request(project_id=project_id))
        assert decision.outcome is MembershipOutcome.MEMBER


def test_demo_sandbox_membership_policy_satisfies_protocol() -> None:
    """Confirms the demo policy structurally implements the same
    ``ProjectMembershipResolver`` Protocol as any real adapter, so it can be
    passed to ``enforce_project_membership``/substituted in router wiring
    without any special-casing at the call site."""
    resolver: ProjectMembershipResolver = DemoSandboxMembershipPolicy()
    enforce_project_membership(resolver, _request())  # must not raise
