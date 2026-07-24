from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from research_assistant_connector_adapter.auth import (
    GatewayAuthorizationError,
    GatewayTokenValidator,
    build_gateway_validator,
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

    assert validator.validate("Bearer signed-token") == "apim-principal"
    with pytest.raises(ValueError, match="unique"):
        GatewayTokenValidator(
            tenant_id="tenant-1",
            principal_ids=("duplicate", "duplicate"),
            jwks=FakeJwks(),
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


def test_gateway_validator_configuration_requires_trusted_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "RESEARCH_PROVIDER_CALLER_PRINCIPAL_IDS",
        "RESEARCH_APIM_PRINCIPAL_ID",
        "RESEARCH_WORKSPACE_TENANT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    assert build_gateway_validator() is None

    monkeypatch.setenv("RESEARCH_WORKSPACE_TENANT_ID", "tenant")
    with pytest.raises(RuntimeError, match="caller principals"):
        build_gateway_validator()
    monkeypatch.setenv(
        "RESEARCH_PROVIDER_CALLER_PRINCIPAL_IDS",
        "api-principal, foundry-principal",
    )
    assert build_gateway_validator() is not None
