"""Bounded HTTP and credential helpers shared by providers."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .config import AuthConfig
from .contracts import (
    AuthMode,
    InvocationContext,
    NeedsConsentError,
    ProviderTimeoutError,
    ProviderValidationError,
    RateLimitError,
    RequestSigningCredential,
    SecretCredential,
    SigningCredential,
    TokenCredential,
    UnauthorizedError,
    UpstreamError,
    canonical_json_hash,
    plain_json,
)

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
MAX_RETRY_DELAY_SECONDS = 30.0
RETRY_WAIT_SLICE_SECONDS = 0.1


def binding_safe_endpoint(value: str | None, *, invalid_label: str) -> tuple[str, str]:
    raw_value = value or invalid_label
    digest = canonical_json_hash(raw_value)
    try:
        parsed = urlsplit(raw_value)
        _ = parsed.port
    except ValueError:
        return invalid_label, digest
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        return invalid_label, digest
    sanitized = urlunsplit(
        (parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], "", "", "")
    ).rstrip("/")
    return sanitized, digest


def safe_url(base_url: str, path: str) -> str:
    base = urlsplit(base_url)
    try:
        _ = base.port
    except ValueError as exc:
        raise ValueError("A valid HTTP(S) endpoint port is required") from exc
    if base.scheme not in {"https", "http"} or not base.netloc or base.username or base.password:
        raise ValueError("A valid HTTP(S) endpoint without userinfo is required")
    if base.scheme == "http" and base.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Plain HTTP endpoints are allowed only on loopback hosts")
    if any(part == ".." for part in path.replace("\\", "/").split("/")):
        raise ValueError("Path traversal is not allowed")
    target = urlsplit(urljoin(base_url.rstrip("/") + "/", path.lstrip("/")))
    if target.scheme != base.scheme or target.netloc != base.netloc:
        raise ValueError("Provider paths cannot override the configured endpoint origin")
    return target.geturl()


def require_endpoint(value: str | None) -> str:
    if not value:
        raise ValueError("Provider endpoint is not configured")
    safe_url(value, "/")
    return value.rstrip("/")


def auth_headers(
    auth: AuthConfig,
    context: InvocationContext,
    *,
    provider_id: str,
    allow_signature: bool = False,
) -> dict[str, str]:
    credential = context.credential
    if auth.mode is AuthMode.NONE:
        return {}
    if auth.mode is AuthMode.SIGNATURE:
        if not allow_signature:
            raise UnauthorizedError(
                "Signature authentication is not supported by this provider",
                provider_id=provider_id,
            )
        return {}
    if auth.mode in {AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY, AuthMode.GITHUB_APP}:
        if not isinstance(credential, TokenCredential) or not auth.token_scope:
            raise UnauthorizedError("A compatible token credential is required", provider_id=provider_id)
        token = credential.get_token(auth.token_scope)
        if not token.token:
            raise UnauthorizedError("The token credential returned an empty token", provider_id=provider_id)
        return {"Authorization": f"Bearer {token.token}"}
    if auth.mode in {AuthMode.API_KEY, AuthMode.SHARED_KEY}:
        if not isinstance(credential, SecretCredential) or not auth.secret_name or not auth.header_name:
            raise UnauthorizedError("A compatible secret credential is required", provider_id=provider_id)
        secret = credential.get_secret(auth.secret_name)
        if not secret:
            raise UnauthorizedError("The secret credential returned an empty value", provider_id=provider_id)
        return {auth.header_name: secret}
    raise UnauthorizedError(  # pragma: no cover - AuthMode is exhaustively handled above.
        "The configured authentication mode is not supported here",
        provider_id=provider_id,
    )


def base64_encoded_length(decoded_bytes: int) -> int:
    if decoded_bytes < 0:
        raise ValueError("Decoded byte limit cannot be negative")
    return ((decoded_bytes + 2) // 3) * 4


def decode_base64_limited(value: str, *, max_bytes: int, provider_id: str) -> bytes:
    if len(value) > base64_encoded_length(max_bytes):
        raise ProviderValidationError("content_base64 exceeds the configured upload limit", provider_id=provider_id)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderValidationError("content_base64 is invalid", provider_id=provider_id) from exc
    if len(decoded) > max_bytes:
        raise ProviderValidationError("content_base64 exceeds the configured upload limit", provider_id=provider_id)
    return decoded


def signing_credential(context: InvocationContext, *, provider_id: str) -> SigningCredential:
    if not isinstance(context.credential, SigningCredential):
        raise UnauthorizedError("A compatible signing credential is required", provider_id=provider_id)
    return context.credential


def request_signing_credential(context: InvocationContext, *, provider_id: str) -> RequestSigningCredential:
    if not isinstance(context.credential, RequestSigningCredential):
        raise UnauthorizedError("A compatible request-signing credential is required", provider_id=provider_id)
    return context.credential


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        reset = response.headers.get("X-RateLimit-Reset")
        if reset and reset.isdigit():
            return max(0.0, float(reset) - datetime.now(tz=UTC).timestamp())
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        return max(0.0, (target - datetime.now(tz=target.tzinfo or UTC)).total_seconds())


def _wait_for_retry(context: InvocationContext, delay: float, *, provider_id: str) -> None:
    remaining = context.remaining_seconds(provider_id=provider_id)
    wait_seconds = (
        min(delay, MAX_RETRY_DELAY_SECONDS, remaining) if remaining is not None else min(delay, MAX_RETRY_DELAY_SECONDS)
    )
    if wait_seconds == 0:
        context.sleep(0)
        context.raise_if_cancelled_or_expired(provider_id=provider_id)
        return
    while wait_seconds > 0:
        context.raise_if_cancelled_or_expired(provider_id=provider_id)
        interval = min(wait_seconds, RETRY_WAIT_SLICE_SECONDS)
        context.sleep(interval)
        wait_seconds -= interval
    context.raise_if_cancelled_or_expired(provider_id=provider_id)


def send(
    context: InvocationContext,
    *,
    provider_id: str,
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    content: bytes | None = None,
    timeout: float = 20.0,
    max_retries: int = 0,
    idempotent: bool,
    consent_on_forbidden: bool = False,
    passthrough_statuses: frozenset[int] = frozenset(),
) -> tuple[httpx.Response, int]:
    attempts = 0
    started = monotonic()
    while True:
        remaining = context.remaining_seconds(provider_id=provider_id)
        request_timeout = timeout if remaining is None else min(timeout, remaining)
        attempts += 1
        try:
            response = context.transport.request(
                method,
                url,
                headers=headers,
                params=params,
                json=plain_json(json_body),
                content=content,
                timeout=request_timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            if idempotent and attempts <= max_retries:
                _wait_for_retry(context, min(2 ** (attempts - 1), 8), provider_id=provider_id)
                continue
            raise ProviderTimeoutError("Provider request timed out", provider_id=provider_id) from exc
        context.raise_if_cancelled_or_expired(provider_id=provider_id)
        retry_after = _retry_after(response)
        if response.status_code in RETRY_STATUSES and idempotent and attempts <= max_retries:
            delay = retry_after if retry_after is not None else min(2 ** (attempts - 1), 8)
            _wait_for_retry(context, delay, provider_id=provider_id)
            continue
        if response.status_code in passthrough_statuses:
            response.extensions["provider_elapsed_ms"] = round(
                (monotonic() - started) * 1000,
                3,
            )
            return response, attempts
        if response.status_code == 401:
            raise UnauthorizedError("Provider rejected the credential", provider_id=provider_id)
        if response.status_code == 429 or (
            response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0"
        ):
            raise RateLimitError("Provider rate limit was exceeded", provider_id=provider_id, retry_after=retry_after)
        if response.status_code == 403:
            if consent_on_forbidden:
                raise NeedsConsentError("Provider denied required permission or consent", provider_id=provider_id)
            raise UnauthorizedError("Provider denied access", provider_id=provider_id)
        if 300 <= response.status_code < 400:
            raise UpstreamError(
                "Provider redirects are not followed",
                provider_id=provider_id,
            )
        if response.status_code >= 400:
            raise UpstreamError(
                f"Provider returned HTTP {response.status_code} from {urlsplit(url).netloc}",
                provider_id=provider_id,
                retry_after=retry_after,
            )
        response.extensions["provider_elapsed_ms"] = round(
            (monotonic() - started) * 1000,
            3,
        )
        return response, attempts


def json_object(response: httpx.Response, *, provider_id: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamError("Provider returned invalid JSON", provider_id=provider_id) from exc
    if not isinstance(payload, dict):
        raise UpstreamError("Provider returned a non-object JSON payload", provider_id=provider_id)
    return payload


def collection(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    for key in ("value", "data", "items", "results", "agents", "connections", "deployments", "models", "functions"):
        value = payload.get(key)
        if isinstance(value, list):
            return tuple(item for item in value if isinstance(item, Mapping))
    return ()


def stable_resource_id(prefix: str, raw: str) -> str:
    import hashlib
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")[:32] or "resource"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"{prefix}.{slug}.{digest}"
