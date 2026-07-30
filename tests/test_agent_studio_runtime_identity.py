from __future__ import annotations

import base64
import json
from typing import Any

from research_assistant_api.agent_studio.runtime_identity import (
    extract_runtime_principal,
    resolve_runtime_principal,
)
from research_assistant_api.config import Settings

ISSUER = "https://login.microsoftonline.com/tenant-1/v2.0"
AUDIENCE = "api://research-assistant-runtime"


def _claims(**overrides: list[str]) -> dict[str, list[str]]:
    base = {
        "iss": [ISSUER],
        "aud": [AUDIENCE],
        "roles": ["research-assistant.runtime"],
        "appid": ["client-app-1"],
    }
    base.update(overrides)
    return base


class _FakeRequest:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _principal_header(claims: dict[str, list[str]], *, user_id: str = "sp-1") -> str:
    payload: dict[str, Any] = {
        "userId": user_id,
        "claims": [{"typ": typ, "val": val} for typ, values in claims.items() for val in values],
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


# --- extract_runtime_principal (pure) --------------------------------------


def test_extract_returns_principal_from_complete_claims() -> None:
    principal = extract_runtime_principal(_claims())
    assert principal is not None
    assert principal.issuer == ISSUER
    assert principal.audiences == (AUDIENCE,)
    assert principal.app_roles == ("research-assistant.runtime",)
    assert principal.client_app_id == "client-app-1"


def test_extract_uses_azp_when_appid_absent() -> None:
    claims = _claims()
    del claims["appid"]
    claims["azp"] = ["client-app-2"]
    principal = extract_runtime_principal(claims)
    assert principal is not None
    assert principal.client_app_id == "client-app-2"


def test_extract_allows_empty_roles() -> None:
    claims = _claims()
    del claims["roles"]
    principal = extract_runtime_principal(claims)
    assert principal is not None
    assert principal.app_roles == ()


def test_extract_returns_none_without_issuer() -> None:
    claims = _claims()
    del claims["iss"]
    assert extract_runtime_principal(claims) is None


def test_extract_returns_none_without_audience() -> None:
    claims = _claims()
    del claims["aud"]
    assert extract_runtime_principal(claims) is None


def test_extract_returns_none_without_client_app_id() -> None:
    claims = _claims()
    del claims["appid"]
    assert extract_runtime_principal(claims) is None


def test_extract_preserves_multiple_audiences() -> None:
    principal = extract_runtime_principal(_claims(aud=[AUDIENCE, "api://other"]))
    assert principal is not None
    assert principal.audiences == (AUDIENCE, "api://other")


def test_extract_accepts_matching_appid_and_azp() -> None:
    claims = _claims()
    claims["azp"] = ["client-app-1"]  # equal to appid
    principal = extract_runtime_principal(claims)
    assert principal is not None
    assert principal.client_app_id == "client-app-1"


def test_extract_fails_closed_when_appid_and_azp_disagree() -> None:
    claims = _claims()
    claims["azp"] = ["different-app"]  # conflicts with appid=client-app-1
    assert extract_runtime_principal(claims) is None


def test_extract_fails_closed_on_multiple_appid_values() -> None:
    assert extract_runtime_principal(_claims(appid=["a", "b"])) is None


def test_extract_fails_closed_on_ambiguous_issuer() -> None:
    assert extract_runtime_principal(_claims(iss=[ISSUER, "https://evil.example/v2.0"])) is None


def test_extract_accepts_duplicate_identical_issuer() -> None:
    principal = extract_runtime_principal(_claims(iss=[ISSUER, ISSUER]))
    assert principal is not None
    assert principal.issuer == ISSUER


# --- resolve_runtime_principal (request-level) -----------------------------


def test_resolve_returns_none_when_gateway_auth_not_enforced() -> None:
    # No gateway validating tokens -> the injected header is not trustworthy,
    # so no runtime principal is ever extracted from it.
    settings = Settings(environment="test")
    request = _FakeRequest({"x-ms-client-principal": _principal_header(_claims())})
    assert resolve_runtime_principal(request, settings) is None  # type: ignore[arg-type]


def test_resolve_returns_none_without_header() -> None:
    settings = Settings(entra_auth_enforced=True)
    assert resolve_runtime_principal(_FakeRequest({}), settings) is None  # type: ignore[arg-type]


def test_resolve_returns_none_for_undecodable_header() -> None:
    settings = Settings(entra_auth_enforced=True)
    request = _FakeRequest({"x-ms-client-principal": "!!!not-base64!!!"})
    assert resolve_runtime_principal(request, settings) is None  # type: ignore[arg-type]


def test_resolve_extracts_principal_from_valid_header() -> None:
    settings = Settings(entra_auth_enforced=True)
    request = _FakeRequest({"x-ms-client-principal": _principal_header(_claims())})
    principal = resolve_runtime_principal(request, settings)  # type: ignore[arg-type]
    assert principal is not None
    assert principal.client_app_id == "client-app-1"
    assert principal.issuer == ISSUER


def test_resolve_returns_none_when_header_lacks_workload_claims() -> None:
    settings = Settings(entra_auth_enforced=True)
    claims = _claims()
    del claims["appid"]
    request = _FakeRequest({"x-ms-client-principal": _principal_header(claims)})
    assert resolve_runtime_principal(request, settings) is None  # type: ignore[arg-type]
