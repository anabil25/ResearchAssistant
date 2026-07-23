from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from research_assistant_api.config import Settings
from research_assistant_api.identity import (
    _claim_values,
    _decode_client_principal,
    enforce_tenant_claim,
    resolve_identity,
)


def _request(headers: dict[str, str]) -> Any:
    return SimpleNamespace(headers=headers)


def _principal(payload: Any) -> str:
    encoded = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return encoded.rstrip("=")


def test_decode_client_principal_and_claim_values_ignore_invalid_entries() -> None:
    payload = {
        "userId": "user-1",
        "claims": [
            {"typ": "groups", "val": "researchers"},
            {"typ": "groups", "val": "grant-reviewers"},
            {"typ": "tid", "val": "tenant-1"},
            {"typ": "groups", "val": 7},
            {"typ": 9, "val": "ignored"},
            "not-a-dict",
        ],
    }

    decoded = _decode_client_principal(_principal(payload))

    assert decoded == payload
    assert _decode_client_principal("not-base64") is None
    assert _decode_client_principal(_principal(["not", "a", "dict"])) is None
    assert _claim_values(payload) == {
        "groups": ["researchers", "grant-reviewers"],
        "tid": ["tenant-1"],
    }


@pytest.mark.parametrize(
    ("claims", "expected_tenant"),
    [
        ([{"typ": "tid", "val": "tenant-1"}], "tenant-1"),
        (
            [{"typ": "http://schemas.microsoft.com/identity/claims/tenantid", "val": "tenant-2"}],
            "tenant-2",
        ),
    ],
)
def test_resolve_identity_prefers_platform_headers_when_enabled(
    claims: list[dict[str, str]],
    expected_tenant: str,
) -> None:
    settings = Settings(trust_platform_identity_headers=True, allow_demo_identity=False)
    request = _request(
        {
            "x-ms-client-principal": _principal(
                {
                    "userId": "user-123",
                    "userDetails": "Ada Lovelace",
                    "claims": [*claims, {"typ": "groups", "val": "researchers"}],
                }
            )
        }
    )

    identity = resolve_identity(request, settings)

    assert identity.user_id == "user-123"
    assert identity.display_name == "Ada Lovelace"
    assert identity.tenant_id == expected_tenant
    assert identity.groups == ("researchers",)
    assert identity.source == "container-apps-auth"


def test_resolve_identity_falls_back_to_demo_identity_for_invalid_or_incomplete_headers() -> None:
    settings = Settings(
        trust_platform_identity_headers=True,
        allow_demo_identity=True,
        workspace_tenant_id="demo-tenant",
    )

    malformed = resolve_identity(
        _request({"x-ms-client-principal": "broken"}),
        settings,
    )
    missing_claims = resolve_identity(
        _request(
            {
                "x-ms-client-principal": _principal(
                    {
                        "userId": "",
                        "claims": [{"typ": "tid", "val": "tenant-1"}],
                    }
                )
            }
        ),
        settings,
    )

    assert malformed.source == "demo-sandbox"
    assert malformed.tenant_id == "demo-tenant"
    assert malformed.groups == ("researchers", "grant-reviewers", "research-admins")
    assert missing_claims.user_id == "demo-researcher"


def test_resolve_identity_requires_authenticated_identity_when_demo_is_disabled() -> None:
    settings = Settings(trust_platform_identity_headers=True, allow_demo_identity=False)

    with pytest.raises(HTTPException, match="platform identity is required") as excinfo:
        resolve_identity(_request({}), settings)

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == "An authenticated platform identity is required."


def test_enforce_tenant_claim_rejects_mismatched_tenant() -> None:
    identity = resolve_identity(
        _request({}),
        Settings(allow_demo_identity=True, workspace_tenant_id="tenant-1"),
    )

    enforce_tenant_claim(identity, "tenant-1")
    with pytest.raises(HTTPException, match="does not match") as excinfo:
        enforce_tenant_claim(identity, "tenant-2")

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Request tenant does not match the authenticated identity."
