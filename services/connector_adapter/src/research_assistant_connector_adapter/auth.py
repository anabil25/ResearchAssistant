from __future__ import annotations

import os

import jwt
from jwt import PyJWKClient


class GatewayAuthorizationError(RuntimeError):
    pass


class GatewayTokenValidator:
    def __init__(
        self,
        *,
        tenant_id: str,
        principal_id: str,
        jwks: PyJWKClient | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._principal_id = principal_id
        self._jwks = jwks or PyJWKClient(
            f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
            cache_keys=True,
        )

    def validate(self, authorization: str | None) -> None:
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
        if claims.get("oid") != self._principal_id:
            raise GatewayAuthorizationError("The caller is not the configured API gateway.")


def build_gateway_validator() -> GatewayTokenValidator | None:
    principal_id = os.getenv("RESEARCH_APIM_PRINCIPAL_ID")
    tenant_id = os.getenv("RESEARCH_WORKSPACE_TENANT_ID")
    if not principal_id and not tenant_id:
        return None
    if not principal_id or not tenant_id:
        raise RuntimeError(
            "Both RESEARCH_APIM_PRINCIPAL_ID and RESEARCH_WORKSPACE_TENANT_ID "
            "are required when gateway authentication is enabled."
        )
    return GatewayTokenValidator(
        tenant_id=tenant_id,
        principal_id=principal_id,
    )
