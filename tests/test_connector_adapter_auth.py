from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from research_assistant_connector_adapter.auth import (
    GatewayAuthorizationError,
    GatewayTokenValidator,
)


class FakeJwks:
    def get_signing_key_from_jwt(self, token: str) -> Any:
        assert token == "signed-token"
        return SimpleNamespace(key="public-key")


def test_gateway_token_requires_exact_apim_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = GatewayTokenValidator(
        tenant_id="tenant-1",
        principal_id="apim-principal",
        jwks=FakeJwks(),
    )
    monkeypatch.setattr(
        jwt,
        "decode",
        lambda *_args, **_kwargs: {"oid": "apim-principal"},
    )

    validator.validate("Bearer signed-token")

    monkeypatch.setattr(
        jwt,
        "decode",
        lambda *_args, **_kwargs: {"oid": "another-principal"},
    )
    with pytest.raises(GatewayAuthorizationError, match="configured API gateway"):
        validator.validate("Bearer signed-token")


def test_gateway_token_rejects_missing_or_invalid_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator = GatewayTokenValidator(
        tenant_id="tenant-1",
        principal_id="apim-principal",
        jwks=FakeJwks(),
    )
    with pytest.raises(GatewayAuthorizationError, match="required"):
        validator.validate(None)

    def invalid(*_args: object, **_kwargs: object) -> dict[str, str]:
        raise jwt.InvalidTokenError("invalid")

    monkeypatch.setattr(jwt, "decode", invalid)
    with pytest.raises(GatewayAuthorizationError, match="invalid"):
        validator.validate("Bearer signed-token")
