"""Extract a runtime ``RuntimePrincipal`` from the platform-validated identity.

Runtime (workload) callers authenticate exactly like every other request to
this API: Azure Container Apps' built-in authentication (EasyAuth) validates the
bearer token (issuer/audience/signature) and injects an ``x-ms-client-principal``
header. This module projects that already-validated principal into the narrow
``RuntimePrincipal`` shape ``runtime_authz`` consumes -- issuer, audiences, app
roles, and the client/app id -- and nothing else.

Crucially, this never parses or validates the ``Authorization`` header itself
(that is the gateway's job) and never trusts an independent request-body field for
any of these values: a runtime cannot self-assert its issuer, audience, roles,
or client id. When ``entra_auth_enforced`` is unset, or the
header is absent/malformed, or the principal lacks the minimum workload-identity
claims (issuer, at least one audience, a client/app id), extraction returns
``None`` and the caller must fail closed -- there is deliberately no local
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

    Fails closed (returns ``None``) unless the minimum workload-identity claims
    are present and unambiguous: exactly one distinct issuer, at least one
    audience, and exactly one client/app id. The client id is taken from
    ``appid`` (v1) and/or ``azp`` (v2); if both are present and **disagree**, or
    either carries more than one distinct value, that is a conflicting/ambiguous
    identity and is refused outright -- never resolved by silent precedence.
    ``app_roles`` may be empty -- a token with no roles is still a valid
    principal that ``runtime_authz`` will then deny for lacking the required
    internal role.
    """

    issuer = _exactly_one(claims, _ISSUER_CLAIM)
    audiences = tuple(claims.get(_AUDIENCE_CLAIM, []))
    client_app_id = _single_client_app_id(claims)
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

    Returns ``None`` (caller must fail closed) unless ``entra_auth_enforced``
    is set -- the header is only trustworthy when an authenticating gateway is
    actually validating tokens in front of this process. Also returns ``None``
    when the header is absent/undecodable or the decoded principal lacks the
    minimum workload claims. There is intentionally no dev fallback -- a
    runtime principal is only ever a real validated workload token, built
    solely from gateway-validated issuer/aud/roles/(appid|azp), never from a
    request body field.
    """

    if not settings.entra_auth_enforced:
        return None
    encoded = request.headers.get("x-ms-client-principal")
    if not encoded:
        return None
    payload = _decode_client_principal(encoded)
    if payload is None:
        return None
    return extract_runtime_principal(_claim_values(payload))


def _exactly_one(claims: Mapping[str, list[str]], claim_type: str) -> str | None:
    """The single distinct value of ``claim_type``, or ``None`` if absent or
    ambiguous (more than one distinct value)."""
    values = set(claims.get(claim_type, []))
    return next(iter(values)) if len(values) == 1 else None


def _single_client_app_id(claims: Mapping[str, list[str]]) -> str | None:
    """The single client/app id across ``appid`` and ``azp``.

    Returns ``None`` when no candidate exists, or when ``appid``/``azp`` carry
    more than one distinct value between them (both present and disagreeing, or
    either repeated) -- a conflicting identity is refused, never resolved by
    silent precedence.
    """
    candidates = set()
    for claim_type in _CLIENT_APP_ID_CLAIMS:
        candidates.update(claims.get(claim_type, []))
    return next(iter(candidates)) if len(candidates) == 1 else None
