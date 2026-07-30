from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status

from research_assistant_api.config import Settings


PLATFORM_OWNER_GROUPS = frozenset({"research-admins", "agent-studio-admins"})
LOCAL_DEVELOPMENT_SOURCE = "demo-sandbox"


@dataclass(frozen=True, slots=True)
class IdentityContext:
    user_id: str
    display_name: str
    tenant_id: str
    groups: tuple[str, ...]
    source: str
    #: Application-role claims (Entra "roles" claim, e.g. app roles assigned
    #: to a service principal/managed identity) carried by this identity.
    #: Distinct from ``groups`` (Entra ID group membership, used for
    #: per-project human membership) -- ``roles`` is how a *non-human*
    #: caller (a hosted-agent runtime's own managed identity) proves it is
    #: acting as that specific application role rather than as a project
    #: member. See ``research_assistant_api.agent_studio.router
    #: .HOSTED_RUNTIME_SERVICE_ROLE`` for the one consumer of this today:
    #: the runtime-internal idempotency control-plane routes require it and
    #: are unreachable by any human identity, which never carries it.
    roles: tuple[str, ...] = ()
    #: ``True`` when the identity provider indicated that the ``groups``
    #: claim was truncated ("group overage") -- e.g. Microsoft Entra ID
    #: replaces an over-large ``groups`` claim with a ``_claim_names``/
    #: ``_claim_sources`` indirection instead of embedding every group.
    #: When this is set, ``groups`` is known-incomplete and must never be
    #: treated as authoritative for a *denial* of membership: callers that
    #: need a durable authorization boundary (e.g. Agent Studio project
    #: membership) must fail closed rather than silently trust the
    #: possibly-truncated list. See ``research_assistant_api.agent_studio
    #: .authz`` for the consumer of this flag.
    groups_overage: bool = False


def is_platform_owner(identity: IdentityContext) -> bool:
    return bool(PLATFORM_OWNER_GROUPS.intersection(identity.groups))


def local_developer_identity(settings: Settings) -> IdentityContext:
    return IdentityContext(
        user_id="demo-researcher",
        display_name="Anonymous researcher",
        tenant_id=settings.workspace_tenant_id,
        groups=("researchers", "research-admins", "grant-reviewers"),
        source=LOCAL_DEVELOPMENT_SOURCE,
    )


def _decode_client_principal(value: str) -> dict[str, Any] | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _claim_values(payload: dict[str, Any]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for claim in payload.get("claims", []):
        if not isinstance(claim, dict):
            continue
        claim_type = claim.get("typ")
        claim_value = claim.get("val")
        if isinstance(claim_type, str) and isinstance(claim_value, str):
            values.setdefault(claim_type, []).append(claim_value)
    return values


def _has_group_overage(payload: dict[str, Any]) -> bool:
    """Detect Microsoft Entra ID "group overage": when a principal belongs to
    more groups than fit in a token, Entra ID omits ``groups`` claims and
    instead emits a ``_claim_names``/``_claim_sources`` indirection (or, for
    the Container Apps/App Service EasyAuth client-principal encoding used
    here, a ``hasgroups`` claim of ``"true"``) telling the caller to fetch
    the full list out-of-band. Either signal means ``claims.get("groups")``
    is not a complete list and must never be treated as authoritative for a
    membership *denial*.
    """
    if "_claim_names" in payload and "groups" in payload.get("_claim_names", {}):
        return True
    for claim in payload.get("claims", []):
        if not isinstance(claim, dict):
            continue
        if claim.get("typ") == "hasgroups" and str(claim.get("val")).lower() == "true":
            return True
    return False


def resolve_identity(request: Request, settings: Settings) -> IdentityContext:
    """Resolve the caller's identity from platform-injected or demo signals.

    ``entra_auth_enforced`` gates trusting the
    ``x-ms-client-principal`` header that Azure Container Apps' built-in
    authentication (EasyAuth / ``Microsoft.App/containerApps/authConfigs``)
    injects *after* it has independently validated the incoming
    ``Authorization`` bearer token (audience/issuer/signature) and rejected
    unauthenticated requests. This function deliberately does not re-parse
    or validate the ``Authorization`` header itself -- that is the whole
    point of relying on the platform's built-in authentication rather than
    duplicating JWT/JWKS validation here.

    That trust is only sound once Container Apps ``authConfigs`` is
    actually deployed and enforcing (see
    ``infra/modules/container-apps.bicep``'s ``enableEntraAuth`` parameter
    and the ``authConfigs`` child resource it emits). ``Settings`` fails
    closed at startup (``config._forbid_unenforced_platform_identity_trust_outside_safe_environments``)
    when ``trust_platform_identity_headers`` is enabled without a
    self-reported ``entra_auth_enforced=True`` outside known-safe
    local/dev/test environments, so a deployment cannot silently trust a
    forged ``x-ms-client-principal`` header in production because the infra
    wiring was forgotten.
    """
    encoded = request.headers.get("x-ms-client-principal")
    if settings.entra_auth_enforced and encoded and (payload := _decode_client_principal(encoded)):
        claims = _claim_values(payload)
        tenant = next(
            iter(claims.get("tid", []) or claims.get("http://schemas.microsoft.com/identity/claims/tenantid", [])),
            None,
        )
        user_id = str(payload.get("userId") or "")
        if tenant and user_id:
            return IdentityContext(
                user_id=user_id,
                display_name=str(payload.get("userDetails") or user_id),
                tenant_id=tenant,
                groups=tuple(claims.get("groups", [])),
                source="container-apps-auth",
                groups_overage=_has_group_overage(payload),
                # Entra App Role assignments (assignable to a service
                # principal/managed identity, not just human users) flow
                # through Container Apps/App Service EasyAuth as a "roles"
                # claim exactly like "groups" does for human group
                # membership -- reuse the same generic claims decoding
                # rather than adding a separate JWT/JWKS validation path.
                roles=tuple(claims.get("roles", [])),
            )

    if not settings.entra_auth_enforced:
        return local_developer_identity(settings)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="An authenticated platform identity is required.",
    )


def enforce_tenant_claim(identity: IdentityContext, supplied_tenant: str) -> None:
    if supplied_tenant != identity.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request tenant does not match the authenticated identity.",
        )


#: Group-name convention for per-project application-role membership: an
#: identity is a member of project ``P`` iff its ``groups`` claim contains
#: ``f"{PROJECT_GROUP_PREFIX}{P}"``. This is deliberately a distinct,
#: explicit boundary from tenant membership (``IdentityContext.tenant_id``)
#: per the Phase 2 partitioning mandate: identity *groups* alone are not a
#: durable partition boundary, but they are the input signal this
#: durable-partition check is derived from, one project at a time.
PROJECT_GROUP_PREFIX = "project:"


def project_group_name(project_id: str) -> str:
    """Canonical group-claim name granting membership in ``project_id``."""
    return f"{PROJECT_GROUP_PREFIX}{project_id}"


#: The interactive local/dev "demo sandbox" identity source (only ever issued
#: when ``Settings.entra_auth_enforced`` is false). It never
#: carries real Entra group claims and exists purely to exercise the API
#: without a real identity provider, so it is exempt from project-membership
#: group-claim checks. See ``research_assistant_api.agent_studio.authz`` for
#: the actual membership-resolution policy this identity is exempted from.
DEMO_SANDBOX_SOURCE = LOCAL_DEVELOPMENT_SOURCE
