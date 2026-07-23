from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, status

from research_assistant_api.config import Settings


@dataclass(frozen=True, slots=True)
class IdentityContext:
    user_id: str
    display_name: str
    tenant_id: str
    groups: tuple[str, ...]
    source: str
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
    encoded = request.headers.get("x-ms-client-principal")
    if settings.trust_platform_identity_headers and encoded and (payload := _decode_client_principal(encoded)):
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
            )

    if settings.allow_demo_identity:
        return IdentityContext(
            user_id="demo-researcher",
            display_name="Dr. Maya Chen",
            tenant_id=settings.workspace_tenant_id,
            groups=("researchers", "grant-reviewers", "research-admins"),
            source="demo-sandbox",
        )

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


def enforce_project_membership(identity: IdentityContext, project_id: str) -> None:
    """Fail-closed application-role check: does ``identity`` belong to ``project_id``?

    Group claims alone are not treated as a sufficient *or* safe boundary on
    their own:

    - The interactive local/dev "demo sandbox" identity (``source ==
      "demo-sandbox"``, only ever issued when ``Settings.allow_demo_identity``
      is explicitly enabled) is exempt, since it never carries real Entra
      group claims and exists purely to exercise the API without a real
      identity provider.
    - Every other identity must carry the ``project:{project_id}`` group.
    - If the identity provider reported group-claim truncation
      (``groups_overage``), the ``groups`` list is known-incomplete and this
      check fails closed (denies) rather than silently trusting an absence
      that might just be a token size limit -- callers see a distinct,
      actionable 403 rather than an inexplicable "not a member" error.
      Resolving overage via an explicit directory/Graph membership lookup is
      a known limitation, out of scope for this pass.
    """
    if identity.source == "demo-sandbox":
        return
    if identity.groups_overage:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Unable to verify project membership: the identity's group claim was "
                "truncated by the identity provider (group overage). Contact an administrator."
            ),
        )
    if project_group_name(project_id) not in identity.groups:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Identity is not a member of project '{project_id}'.",
        )
