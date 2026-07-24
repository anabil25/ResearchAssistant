"""Deterministic runtime-control authorization order.

Runtime (harness/hosted-agent) callers are authorized on a **completely
separate** path from human researchers. A human reaches Agent Studio through
``authz.ProjectMembershipResolver`` (project group membership); a runtime never
does. A runtime principal is a validated platform/workload identity, and its
right to load a deployment is decided *only* by:

1. **Platform validation / issuer / audience.** The token was already
   validated by Azure Container Apps' built-in authentication (EasyAuth); this
   resolver additionally pins the exact expected ``issuer`` and ``audience`` so
   a token minted for a different app/tenant is rejected even if EasyAuth let
   it through a broader gate.
2. **Exact internal app role.** The principal must carry the exact internal
   application role required of every runtime caller (a coarse "is a runtime at
   all" gate) -- never a human group/role.
3. **Authenticated client/app-id allowlist to one deployment.** Only after the
   above pass is the mapping loaded from *its* stored partition, and the
   principal's ``(client_app_id, app_role)`` must appear in that one mapping's
   server-authored ``allowed_client_app_role_bindings`` -- binding this exact
   client identity to this exact deployment.
4. **Exact request mapping ref + digest.** The ref and digest the runtime
   echoes back must equal the loaded mapping's own, byte for byte.

The mapping partition read is deferred behind an injected ``load_mapping``
callable that is invoked **only** once steps 1-2 pass, so an unauthenticated or
non-runtime caller can never trigger a partition read and use its
success/latency as a deployment-existence oracle.

Every denial -- bad issuer/audience, missing role, mapping absent, client not
allowlisted, ref/digest mismatch -- is reported to the caller **uniformly**
(see ``uniform_denial``) so a probe cannot distinguish "forbidden" from "no
such deployment". The internal ``reason`` is retained for server-side audit
only and never shaped into the client response.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from research_assistant_api.agent_studio.runtime_client_binding import AuthorizedMappingLoader
from research_assistant_api.agent_studio.runtime_deployment_mapping import RuntimeDeploymentMapping


class RuntimeAuthzReason(StrEnum):
    """Internal (audit-only) outcome of a runtime authorization decision.

    Only ``AUTHORIZED`` grants access; every other value is a denial that the
    caller must render as a single, uniform forbidden/not-found response.
    """

    AUTHORIZED = "authorized"
    ISSUER_MISMATCH = "issuer_mismatch"
    AUDIENCE_MISMATCH = "audience_mismatch"
    MISSING_APP_ROLE = "missing_app_role"
    MAPPING_NOT_FOUND = "mapping_not_found"
    DEPLOYMENT_ID_MISMATCH = "deployment_id_mismatch"
    MAPPING_NOT_YET_EFFECTIVE = "mapping_not_yet_effective"
    MAPPING_EXPIRED = "mapping_expired"
    CLIENT_NOT_ALLOWED = "client_not_allowed"
    MAPPING_REF_MISMATCH = "mapping_ref_mismatch"
    MAPPING_REVISION_STALE = "mapping_revision_stale"
    MAPPING_DIGEST_MISMATCH = "mapping_digest_mismatch"


#: Map a mapping ``lifecycle_fault`` string to its distinct audit reason. The
#: immutable mapping expresses ONLY its creation-time validity window, so the
#: only faults it produces are ``not_yet_effective`` and ``expired``; revocation
#: and supersession are enforced by the mutable binding ``status`` (a revoked/
#: superseded binding denies via the loader), not by the mapping.
_LIFECYCLE_FAULT_REASONS = {
    "not_yet_effective": RuntimeAuthzReason.MAPPING_NOT_YET_EFFECTIVE,
    "expired": RuntimeAuthzReason.MAPPING_EXPIRED,
}


@dataclass(frozen=True, slots=True)
class RuntimeAuthPolicy:
    """Server-configured expectations every runtime token/role must satisfy."""

    expected_issuer: str
    expected_audience: str
    required_app_role: str


@dataclass(frozen=True, slots=True)
class RuntimePrincipal:
    """A validated platform/workload identity presenting a runtime request.

    All fields originate from the platform-validated token (EasyAuth-injected
    ``x-ms-client-principal``), never from an independent request body field.
    ``client_app_id`` is the Entra application/client identifier (never a human
    user id); ``app_roles`` are the application roles the token carries.
    """

    issuer: str
    audiences: tuple[str, ...]
    app_roles: tuple[str, ...]
    client_app_id: str


@dataclass(frozen=True, slots=True)
class RuntimeAuthzDecision:
    """Outcome of :func:`authorize_runtime_request`.

    ``mapping`` is populated only on ``AUTHORIZED`` (the exact mapping the
    caller may now act on). ``reason`` is for server-side audit only.
    """

    reason: RuntimeAuthzReason
    mapping: RuntimeDeploymentMapping | None = None

    @property
    def authorized(self) -> bool:
        return self.reason is RuntimeAuthzReason.AUTHORIZED


class RuntimeAuthorizationError(Exception):
    """Raised by :func:`enforce_runtime_authorization` on any denial."""

    def __init__(self, decision: RuntimeAuthzDecision) -> None:
        self.decision = decision
        super().__init__(f"Runtime authorization denied: {decision.reason.value}.")


def uniform_denial() -> str:
    """The single client-facing message used for *every* runtime denial.

    Reused for forbidden and not-found alike so a caller cannot distinguish
    "you may not load this deployment" from "no such deployment" -- both are an
    identical opaque response, removing the existence oracle.
    """

    return "The requested runtime deployment is not available."


def authorize_runtime_request(
    *,
    policy: RuntimeAuthPolicy,
    principal: RuntimePrincipal,
    presented_deployment_id: str,
    presented_mapping_ref: str,
    presented_mapping_digest: str,
    load_authorized_mapping: AuthorizedMappingLoader,
    now: datetime,
) -> RuntimeAuthzDecision:
    """Resolve the runtime authorization order for a single request.

    ``now`` (required, aware UTC) is the instant the lifecycle/expiry decision
    is evaluated against; it is supplied by the composition root, never read
    from the wall clock inside this function, so expiry/revocation are
    deterministic and testable at a chosen instant.

    ``load_authorized_mapping`` is invoked only after issuer/audience/role pass,
    and it is responsible for authorizing the authenticated client's binding to
    the asserted deployment *before* any mapping point-read: it returns the
    mapping only when the trusted ``client_app_id`` is bound to exactly that
    ``deployment_id`` (server-owned authority), and ``None`` uniformly
    otherwise. The caller-asserted deployment id is therefore never itself the
    lookup authority, and an unbound/wrong client cannot enumerate or time-probe
    deployment ids. All ref/digest comparisons are constant-time.
    """

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware (UTC).")

    if principal.issuer != policy.expected_issuer:
        return RuntimeAuthzDecision(reason=RuntimeAuthzReason.ISSUER_MISMATCH)
    if policy.expected_audience not in principal.audiences:
        return RuntimeAuthzDecision(reason=RuntimeAuthzReason.AUDIENCE_MISMATCH)
    if policy.required_app_role not in principal.app_roles:
        return RuntimeAuthzDecision(reason=RuntimeAuthzReason.MISSING_APP_ROLE)

    mapping = load_authorized_mapping(principal.client_app_id, presented_deployment_id)
    if mapping is None:
        return RuntimeAuthzDecision(reason=RuntimeAuthzReason.MAPPING_NOT_FOUND)
    # Defense-in-depth invariant: a correct loader returns the mapping for the
    # asserted deployment, but authorize_runtime_request receives the loader as
    # a closure -- a future refactor/test double could return a mapping for a
    # different deployment. Re-verify (constant-time) rather than trusting the
    # loader's contract; never removed by "the loader guarantees it".
    if not hmac.compare_digest(mapping.deployment_id, presented_deployment_id):
        return RuntimeAuthzDecision(reason=RuntimeAuthzReason.DEPLOYMENT_ID_MISMATCH)
    fault = mapping.lifecycle_fault(now)
    if fault is not None:
        return RuntimeAuthzDecision(reason=_LIFECYCLE_FAULT_REASONS[fault])

    if not _client_is_allowlisted(principal, mapping):
        return RuntimeAuthzDecision(reason=RuntimeAuthzReason.CLIENT_NOT_ALLOWED)

    if not hmac.compare_digest(presented_mapping_ref, mapping.mapping_ref):
        # Flaw C: with the revision component in the ref, separate ROUTINE
        # post-supersession staleness (same schema + deployment, different
        # revision) from a genuine ref disagreement, so incident response never
        # confuses a stale runtime with a tampered one. The ref is issued
        # material (not a secret), so classifying it need not be constant-time;
        # the content digest below stays constant-time.
        revision_prefix = f"{mapping.schema_version}:{mapping.deployment_id}:"
        if presented_mapping_ref.startswith(revision_prefix):
            return RuntimeAuthzDecision(reason=RuntimeAuthzReason.MAPPING_REVISION_STALE)
        return RuntimeAuthzDecision(reason=RuntimeAuthzReason.MAPPING_REF_MISMATCH)
    if not hmac.compare_digest(presented_mapping_digest, mapping.mapping_digest):
        return RuntimeAuthzDecision(reason=RuntimeAuthzReason.MAPPING_DIGEST_MISMATCH)

    return RuntimeAuthzDecision(reason=RuntimeAuthzReason.AUTHORIZED, mapping=mapping)


def _client_is_allowlisted(principal: RuntimePrincipal, mapping: RuntimeDeploymentMapping) -> bool:
    """True iff some allowlist entry binds this client id to a role the
    principal actually holds.

    Requires both that the entry's ``client_app_id`` matches the authenticated
    client *and* that its ``app_role`` is one the token carries -- an allowlist
    entry alone is never sufficient without the principal also presenting that
    exact role.
    """

    held_roles = set(principal.app_roles)
    return any(
        entry.client_app_id == principal.client_app_id and entry.app_role in held_roles
        for entry in mapping.allowed_client_app_role_bindings
    )


def enforce_runtime_authorization(
    *,
    policy: RuntimeAuthPolicy,
    principal: RuntimePrincipal,
    presented_deployment_id: str,
    presented_mapping_ref: str,
    presented_mapping_digest: str,
    load_authorized_mapping: AuthorizedMappingLoader,
    now: datetime,
) -> RuntimeDeploymentMapping:
    """Fail-closed wrapper: return the authorized mapping or raise.

    Raises ``RuntimeAuthorizationError`` (which a router renders as the single
    uniform forbidden/not-found response) on any non-authorized outcome. ``now``
    (aware UTC) is passed through for the deterministic lifecycle/expiry check.
    """

    decision = authorize_runtime_request(
        policy=policy,
        principal=principal,
        presented_deployment_id=presented_deployment_id,
        presented_mapping_ref=presented_mapping_ref,
        presented_mapping_digest=presented_mapping_digest,
        load_authorized_mapping=load_authorized_mapping,
        now=now,
    )
    if decision.authorized and decision.mapping is not None:
        return decision.mapping
    raise RuntimeAuthorizationError(decision)
