from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from jwt import PyJWKClient
from research_assistant_connector_adapter.auth import (
    GatewayAuthorizationError,
    GatewayTokenValidator,
    build_gateway_validator,
)


class FakeJwks(PyJWKClient):
    def __init__(self) -> None:
        pass

    def get_signing_key_from_jwt(self, token: str | bytes) -> Any:
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


def test_gateway_validator_configuration_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARCH_APIM_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("RESEARCH_WORKSPACE_TENANT_ID", raising=False)
    assert build_gateway_validator() is None

    monkeypatch.setenv("RESEARCH_APIM_PRINCIPAL_ID", "apim-principal")
    with pytest.raises(RuntimeError, match="Both RESEARCH_APIM_PRINCIPAL_ID"):
        build_gateway_validator()

    monkeypatch.setenv("RESEARCH_WORKSPACE_TENANT_ID", "tenant-1")
    validator = build_gateway_validator()
    assert validator is not None
    assert validator._principal_id == "apim-principal"
    assert validator._tenant_id == "tenant-1"


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
