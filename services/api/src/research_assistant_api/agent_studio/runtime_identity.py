"""Extract a runtime ``RuntimePrincipal`` from the platform-validated identity.

Runtime (workload) callers authenticate exactly like every other request to
this API: Azure Container Apps' built-in authentication (EasyAuth) validates the
bearer token (issuer/audience/signature) and injects an ``x-ms-client-principal``
header. This module projects that already-validated principal into the narrow
``RuntimePrincipal`` shape ``runtime_authz`` consumes -- issuer, audiences, app
roles, and the client/app id -- and nothing else.

Crucially, this never parses or validates the ``Authorization`` header itself
(that is EasyAuth's job) and never trusts an independent request-body field for
any of these values: a runtime cannot self-assert its issuer, audience, roles,
or client id. When ``trust_platform_identity_headers`` is disabled, or the
header is absent/malformed, or the principal lacks the minimum workload-identity
claims (issuer, at least one audience, a client/app id), extraction returns
``None`` and the caller must fail closed -- there is deliberately no demo/dev
runtime identity, because a runtime principal is only ever a real validated
workload token.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request

from research_assistant_api.agent_studio.runtime_authz import RuntimePrincipal
from research_assistant_api.config import Settings
from research_assistant_api.identity import _claim_values, _decode_client_principal

#: Claim type carrying the token issuer (``iss``).
_ISSUER_CLAIM = "iss"
#: Claim type carrying the token audience (``aud``); a token may present more
#: than one audience value.
_AUDIENCE_CLAIM = "aud"
#: Claim type carrying Entra application-role assignments (``roles``), the same
#: claim ``identity.resolve_identity`` reads for the human-facing surface.
_ROLES_CLAIM = "roles"
#: Claim types that carry the calling application/client id, in preference
#: order: ``appid`` (Entra v1 tokens) then ``azp`` (v2 authorized party).
_CLIENT_APP_ID_CLAIMS = ("appid", "azp")


def extract_runtime_principal(claims: Mapping[str, list[str]]) -> RuntimePrincipal | None:
    """Project already-decoded platform claims into a ``RuntimePrincipal``.

    Returns ``None`` unless the minimum workload-identity claims are present:
    an issuer, at least one audience, and a client/app id. ``app_roles`` may be
    empty -- a token with no roles is still a valid principal that
    ``runtime_authz`` will then deny for lacking the required internal role, so
    "no roles" is an authorization decision, not an extraction failure.
    """

    issuer = _first(claims, _ISSUER_CLAIM)
    audiences = tuple(claims.get(_AUDIENCE_CLAIM, []))
    client_app_id = _first_of(claims, _CLIENT_APP_ID_CLAIMS)
    if issuer is None or not audiences or client_app_id is None:
        return None
    return RuntimePrincipal(
        issuer=issuer,
        audiences=audiences,
        app_roles=tuple(claims.get(_ROLES_CLAIM, [])),
        client_app_id=client_app_id,
    )


def resolve_runtime_principal(request: Request, settings: Settings) -> RuntimePrincipal | None:
    """Resolve a ``RuntimePrincipal`` from the request's platform-injected header.

    Returns ``None`` (caller must fail closed) unless BOTH
    ``trust_platform_identity_headers`` and ``entra_auth_enforced`` are set --
    the header is only trustworthy when Container Apps EasyAuth is actually
    validating tokens (enforced) and the app is configured to trust its output
    (trust). Also returns ``None`` when the header is absent/undecodable or the
    decoded principal lacks the minimum workload claims. There is intentionally
    no demo/dev fallback -- a runtime principal is only ever a real validated
    workload token, built solely from EasyAuth-validated issuer/aud/roles/
    (appid|azp), never from a request body field.
    """

    if not (settings.trust_platform_identity_headers and settings.entra_auth_enforced):
        return None
    encoded = request.headers.get("x-ms-client-principal")
    if not encoded:
        return None
    payload = _decode_client_principal(encoded)
    if payload is None:
        return None
    return extract_runtime_principal(_claim_values(payload))


def _first(claims: Mapping[str, list[str]], claim_type: str) -> str | None:
    values = claims.get(claim_type)
    return values[0] if values else None


def _first_of(claims: Mapping[str, list[str]], claim_types: tuple[str, ...]) -> str | None:
    for claim_type in claim_types:
        value = _first(claims, claim_type)
        if value is not None:
            return value
    return None
