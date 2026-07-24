"""Project-membership authorization for the Agent Studio platform.

Coupling authorization decisions directly to a raw ``IdentityContext.groups``
claim list is unsafe on its own: an identity provider's group claim can be
*known-incomplete* (Microsoft Entra ID "group overage" -- see
``research_assistant_api.identity._has_group_overage``), and a caller that
only inspects ``groups`` has no principled way to distinguish "definitely not
a member" from "we don't actually know". Treating overage as an unresolved
limitation and falling through to a plain membership check would let a
truncated claims list silently deny access to a legitimate member (or, in a
differently-shaped bug, silently grant it) -- neither is acceptable for a
durable authorization boundary.

``ProjectMembershipResolver`` is the domain seam every route goes through
instead: it returns a structured ``MembershipDecision`` with three possible
outcomes (``MEMBER`` / ``NOT_MEMBER`` / ``UNAVAILABLE``), and callers must
fail closed on ``UNAVAILABLE`` -- *before* reaching any resource existence
lookup, so a membership-unknown case can never be inferred from the shape of
a later 404 vs 403.

``ClaimsGroupMembershipResolver`` is the default, application-owned adapter:
it authorizes membership purely from the ``project:{project_id}`` group-claim
convention (see ``research_assistant_api.identity.project_group_name``), but
only when the caller asserts the claim set is known-complete
(``groups_known_complete=True``). Group overage, or any other source that
cannot assert completeness, resolves to ``UNAVAILABLE`` rather than silently
running the (potentially wrong) claims check anyway.

This module intentionally defines only a *protocol* plus the claims-based
adapter. A future Microsoft Graph or application-role-membership adapter can
implement ``ProjectMembershipResolver`` to authoritatively resolve membership
(including overage cases, by calling out to a directory instead of trusting
the token) without any router or service code changing -- callers depend on
the protocol, never on this module's concrete adapter.

Application role/ownership grants (``AgentRole`` derived from
``OwnershipGrant`` records in the store) are a *separate*, additionally
required check layered on top of project membership where both apply; this
module resolves project membership only and never substitutes for an
ownership/role check.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from research_assistant_api.identity import project_group_name

__all__ = [
    "ClaimsGroupMembershipResolver",
    "DemoSandboxMembershipPolicy",
    "MembershipCheckRequest",
    "MembershipDecision",
    "MembershipOutcome",
    "ProjectMembershipError",
    "ProjectMembershipResolver",
    "enforce_project_membership",
]


class MembershipOutcome(StrEnum):
    """Structured result of a project-membership resolution."""

    #: The principal is confirmed to belong to the project.
    MEMBER = "member"
    #: The principal is confirmed *not* to belong to the project, from a
    #: known-complete membership source.
    NOT_MEMBER = "not_member"
    #: Membership could not be determined from the available source (e.g.
    #: group-claims overage, or no membership source configured at all).
    #: Callers must treat this identically to a denial -- fail closed -- but
    #: may surface a distinct, actionable message to the caller.
    UNAVAILABLE = "unavailable"


def _normalize_group(value: str) -> str:
    """Normalize a single group-claim value for comparison.

    Applies Unicode NFC normalization so that visually- and
    semantically-identical group names that differ only in composed vs.
    decomposed Unicode form (a common spoofing/confusion vector for
    claim-based string comparison) are treated as equal. This is a pure
    normalization step -- it never widens what counts as a match, it only
    ensures two encodings of the same text compare equal.
    """
    return unicodedata.normalize("NFC", value)


@dataclass(frozen=True, slots=True)
class MembershipCheckRequest:
    """Everything a ``ProjectMembershipResolver`` needs to make a decision.

    Deliberately does not carry the full ``IdentityContext`` -- only the
    specific, already-normalized fields a resolver is allowed to reason
    about, so a resolver implementation can never reach for some other
    identity field (e.g. ``display_name``) as an authorization signal.
    """

    tenant_id: str
    project_id: str
    principal_id: str
    #: Raw group-claim values asserted for the principal. Adapters must not
    #: assume these are complete unless ``groups_known_complete`` is True.
    claimed_groups: tuple[str, ...]
    #: True only when the identity source positively asserts the group
    #: claim list is exhaustive (no provider-side truncation / overage, and
    #: no separate out-of-band membership source is required). False by
    #: default -- an unset/unknown completeness signal must never be
    #: treated as "complete".
    groups_known_complete: bool = False


@dataclass(frozen=True, slots=True)
class MembershipDecision:
    """The result of resolving a ``MembershipCheckRequest``."""

    outcome: MembershipOutcome
    reason: str | None = None


class ProjectMembershipResolver(Protocol):
    """Domain port: resolve whether a principal belongs to a project.

    Implementations must never guess in the face of incomplete information;
    they must return ``MembershipOutcome.UNAVAILABLE`` instead of either
    ``MEMBER`` or ``NOT_MEMBER`` whenever they cannot positively assert
    completeness of their membership source for this request.
    """

    def resolve_membership(self, request: MembershipCheckRequest) -> MembershipDecision:
        """Return a structured membership decision for ``request``."""
        ...


class ClaimsGroupMembershipResolver:
    """Default adapter: resolves membership from the ``project:{id}`` group claim.

    Fails closed (``UNAVAILABLE``) whenever the caller has not asserted that
    ``claimed_groups`` is a complete list for this identity -- it never
    attempts to distinguish "the group claim is missing because the
    principal isn't a member" from "the group claim is missing because it
    was truncated by the identity provider". Only a request that explicitly
    asserts completeness is evaluated against the claim convention.
    """

    def resolve_membership(self, request: MembershipCheckRequest) -> MembershipDecision:
        if not request.groups_known_complete:
            return MembershipDecision(
                outcome=MembershipOutcome.UNAVAILABLE,
                reason=(
                    "Unable to verify project membership: the identity's group claim "
                    "was truncated by the identity provider (group overage), or no "
                    "complete membership source is available. Contact an administrator."
                ),
            )
        normalized_target = _normalize_group(project_group_name(request.project_id))
        normalized_claims = {_normalize_group(group) for group in request.claimed_groups}
        if normalized_target in normalized_claims:
            return MembershipDecision(outcome=MembershipOutcome.MEMBER)
        return MembershipDecision(
            outcome=MembershipOutcome.NOT_MEMBER,
            reason=f"Identity is not a member of project '{request.project_id}'.",
        )


class DemoSandboxMembershipPolicy:
    """Explicit, named local/test-only membership policy for the demo sandbox identity.

    ``research_assistant_api.identity``'s unauthenticated "demo sandbox"
    identity (``DEMO_SANDBOX_SOURCE``) is deliberately allowed to reach any
    project without a real membership claim -- it exists purely so an
    unauthenticated local/dev/test caller can exercise the API. That
    behavior must never be an ad hoc ``if identity.source == ...`` skip
    embedded in route-scoping logic: an implicit bypass that never even
    constructs a ``MembershipCheckRequest`` is invisible to anything that
    instruments/audits the ``ProjectMembershipResolver`` seam, and is easy to
    accidentally widen later. Routing it through this single, explicitly
    named, unit-testable policy object instead means:

    * the demo bypass is a single, greppable symbol, not scattered logic;
    * it always goes through :func:`enforce_project_membership`, so any
      future instrumentation on that seam also observes demo-sandbox calls;
    * it is independent of whatever ``ProjectMembershipResolver`` the
      application composes for real identities (e.g. a future Graph
      adapter) -- a hardened real-identity resolver can never accidentally
      loosen or tighten the demo sandbox's behavior, and vice versa.

    This policy performs no verification of its own and unconditionally
    grants ``MEMBER`` -- it is the caller's responsibility (see
    ``research_assistant_api.agent_studio.router._scope``) to route to this
    policy *only* for a principal whose ``IdentityContext.source`` is
    already confirmed to be ``DEMO_SANDBOX_SOURCE``, which is itself only
    ever constructible when ``Settings.allow_demo_identity`` is enabled --
    and that field defaults to ``False`` and is refused outside
    ``DEMO_IDENTITY_SAFE_ENVIRONMENTS`` (see ``config.py``). This class does
    not re-check either of those preconditions so it can be exercised as a
    pure, isolated decision in unit tests.
    """

    def resolve_membership(self, request: MembershipCheckRequest) -> MembershipDecision:
        return MembershipDecision(
            outcome=MembershipOutcome.MEMBER,
            reason="Demo sandbox identity: explicit local/test-only membership policy.",
        )


class ProjectMembershipError(Exception):
    """Raised by ``enforce_project_membership`` on any non-member outcome."""

    def __init__(self, decision: MembershipDecision) -> None:
        self.decision = decision
        super().__init__(decision.reason or "Project membership could not be confirmed.")


def enforce_project_membership(
    resolver: ProjectMembershipResolver,
    request: MembershipCheckRequest,
) -> None:
    """Fail-closed enforcement: raise ``ProjectMembershipError`` unless ``MEMBER``.

    Both ``NOT_MEMBER`` and ``UNAVAILABLE`` raise -- callers must never treat
    "membership unknown" as equivalent to success, and must never perform a
    resource existence lookup before this check has resolved to ``MEMBER``.
    """
    decision = resolver.resolve_membership(request)
    if decision.outcome is MembershipOutcome.MEMBER:
        return
    raise ProjectMembershipError(decision)
