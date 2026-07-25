from __future__ import annotations

import os
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient


class GatewayAuthorizationError(RuntimeError):
    pass


class JwkClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class GatewayTokenValidator:
    def __init__(
        self,
        *,
        tenant_id: str,
        principal_id: str | None = None,
        principal_ids: tuple[str, ...] = (),
        jwks: JwkClient | None = None,
    ) -> None:
        allowed_principals = (*principal_ids, *((principal_id,) if principal_id else ()))
        if not tenant_id or not allowed_principals or len(set(allowed_principals)) != len(
            allowed_principals
        ):
            raise ValueError("Gateway tenant and unique allowed principal identities are required")
        self._tenant_id = tenant_id
        self._principal_ids = frozenset(allowed_principals)
        self._jwks = jwks or PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
        )

    def validate(self, authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise GatewayAuthorizationError("A bearer token is required.")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=[
                    "https://management.azure.com",
                    "https://management.azure.com/",
                ],
                issuer=f"https://sts.windows.net/{self._tenant_id}/",
            )
        except jwt.PyJWTError as exc:
            raise GatewayAuthorizationError("The gateway token is invalid.") from exc
        principal_id = claims.get("oid")
        if not isinstance(principal_id, str) or principal_id not in self._principal_ids:
            raise GatewayAuthorizationError("The caller is not the configured API gateway.")
        return principal_id


def build_gateway_validator() -> GatewayTokenValidator | None:
    configured_principals = os.getenv("RESEARCH_PROVIDER_CALLER_PRINCIPAL_IDS")
    principal_ids = tuple(
        item.strip()
        for item in (configured_principals or "").split(",")
        if item.strip()
    )
    legacy_principal_id = os.getenv("RESEARCH_APIM_PRINCIPAL_ID")
    if legacy_principal_id:
        principal_ids = (*principal_ids, legacy_principal_id)
    tenant_id = os.getenv("RESEARCH_WORKSPACE_TENANT_ID")
    if not principal_ids and not tenant_id:
        return None
    if not principal_ids or not tenant_id:
        raise RuntimeError(
            "Provider caller principals and RESEARCH_WORKSPACE_TENANT_ID "
            "are required when gateway authentication is enabled."
        )
    return GatewayTokenValidator(
        tenant_id=tenant_id,
        principal_ids=principal_ids,
    )
