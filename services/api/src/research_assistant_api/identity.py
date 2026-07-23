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
