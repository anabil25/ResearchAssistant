"""Environment policy tests for ``Settings``.

These cover the production guard on ``allow_demo_identity`` (Phase2 review
finding #4): the unauthenticated "demo sandbox" identity bypass
(``research_assistant_api.identity.DEMO_SANDBOX_SOURCE``) must be an
explicit, impossible-to-misconfigure-into-production local/dev/test
adapter, never a silent default that survives into a production
deployment through a missing or unrecognized ``RESEARCH_ENVIRONMENT``.

They also cover the equivalent production guard on the
``ReleaseAttestation`` signing key: an unkeyed SHA-256 digest is honest
integrity labeling, never authentication, and must never become the
silent production default either.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from research_assistant_api.config import (
    ATTESTATION_UNSIGNED_DIGEST_SAFE_ENVIRONMENTS,
    DEMO_IDENTITY_SAFE_ENVIRONMENTS,
    ENTRA_AUTH_UNENFORCED_SAFE_ENVIRONMENTS,
    Settings,
)


def test_default_settings_disable_demo_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The out-of-the-box default must be least-privilege: demo identity is
    off unless explicitly opted into via ``RESEARCH_ALLOW_DEMO_IDENTITY``,
    even in the default ``development`` environment. A default of ``True``
    would let an unconfigured deployment silently boot with an
    unauthenticated, group-bearing identity. This clears the test session's
    own opt-in env var (set in ``tests/conftest.py`` so unauthenticated
    endpoint tests can exercise the demo identity) to verify the field's
    true unconfigured default, not the test session's explicit override.
    """
    monkeypatch.delenv("RESEARCH_ALLOW_DEMO_IDENTITY", raising=False)
    settings = Settings()
    assert settings.environment == "development"
    assert settings.allow_demo_identity is False


@pytest.mark.parametrize("environment", sorted(DEMO_IDENTITY_SAFE_ENVIRONMENTS))
def test_demo_identity_is_permitted_in_every_safe_environment(environment: str) -> None:
    settings = Settings(environment=environment, allow_demo_identity=True)
    assert settings.environment == environment


@pytest.mark.parametrize("environment", sorted(DEMO_IDENTITY_SAFE_ENVIRONMENTS))
def test_demo_identity_safe_environment_names_are_case_and_whitespace_insensitive(environment: str) -> None:
    settings = Settings(environment=f"  {environment.upper()}  ", allow_demo_identity=True)
    assert settings.allow_demo_identity is True


@pytest.mark.parametrize("environment", ["production", "prod", "staging", "PRODUCTION", "unknown-env"])
def test_demo_identity_is_refused_outside_safe_environments(environment: str) -> None:
    with pytest.raises(ValidationError, match="RESEARCH_ALLOW_DEMO_IDENTITY"):
        Settings(environment=environment, allow_demo_identity=True)


@pytest.mark.parametrize("environment", ["production", "prod", "staging", "unknown-env"])
def test_demo_identity_disabled_is_always_permitted_regardless_of_environment(environment: str) -> None:
    # A signing key (+ version) is supplied here so this test exercises only
    # the demo-identity guard in isolation, not the independent
    # attestation-signing-key production guard covered below.
    settings = Settings(
        environment=environment,
        allow_demo_identity=False,
        agent_studio_attestation_signing_key="operator-key",
        agent_studio_attestation_signing_key_version="v1",
    )
    assert settings.allow_demo_identity is False
    assert settings.environment == environment


# --- ReleaseAttestation signing-key production guard -----------------------


def test_default_settings_have_no_attestation_signing_key_in_development() -> None:
    settings = Settings()
    assert settings.environment == "development"
    assert settings.agent_studio_attestation_signing_key is None
    assert settings.agent_studio_attestation_signing_key_version is None


@pytest.mark.parametrize("environment", sorted(ATTESTATION_UNSIGNED_DIGEST_SAFE_ENVIRONMENTS))
def test_unsigned_attestation_digest_fallback_is_permitted_in_every_safe_environment(environment: str) -> None:
    settings = Settings(environment=environment)
    assert settings.agent_studio_attestation_signing_key is None


@pytest.mark.parametrize("environment", ["production", "prod", "staging", "PRODUCTION", "unknown-env"])
def test_unsigned_attestation_digest_fallback_is_refused_outside_safe_environments(environment: str) -> None:
    with pytest.raises(ValidationError, match="AGENT_STUDIO_ATTESTATION_SIGNING_KEY"):
        Settings(environment=environment, allow_demo_identity=False)


@pytest.mark.parametrize("environment", ["production", "prod", "staging", "unknown-env"])
def test_configured_signing_key_with_version_is_permitted_in_every_environment(environment: str) -> None:
    settings = Settings(
        environment=environment,
        allow_demo_identity=False,
        agent_studio_attestation_signing_key="operator-key",
        agent_studio_attestation_signing_key_version="v1",
    )
    assert settings.agent_studio_attestation_signing_key == "operator-key"
    assert settings.agent_studio_attestation_signing_key_version == "v1"
    assert settings.environment == environment


def test_configured_signing_key_without_version_is_always_refused() -> None:
    with pytest.raises(ValidationError, match="AGENT_STUDIO_ATTESTATION_SIGNING_KEY_VERSION"):
        Settings(
            environment="development",
            agent_studio_attestation_signing_key="operator-key",
            agent_studio_attestation_signing_key_version=None,
        )


# --- Entra auth enforcement guard on trust_platform_identity_headers -------


def test_default_settings_do_not_trust_platform_headers_or_claim_entra_enforcement() -> None:
    settings = Settings()
    assert settings.trust_platform_identity_headers is False
    assert settings.entra_auth_enforced is False


@pytest.mark.parametrize("environment", sorted(ENTRA_AUTH_UNENFORCED_SAFE_ENVIRONMENTS))
def test_trusting_platform_headers_without_entra_enforcement_is_permitted_in_every_safe_environment(
    environment: str,
) -> None:
    settings = Settings(
        environment=environment,
        trust_platform_identity_headers=True,
        entra_auth_enforced=False,
        agent_studio_attestation_signing_key="operator-key",
        agent_studio_attestation_signing_key_version="v1",
    )
    assert settings.trust_platform_identity_headers is True
    assert settings.entra_auth_enforced is False
    assert settings.environment == environment


@pytest.mark.parametrize("environment", sorted(ENTRA_AUTH_UNENFORCED_SAFE_ENVIRONMENTS))
def test_entra_enforcement_safe_environment_names_are_case_and_whitespace_insensitive(
    environment: str,
) -> None:
    settings = Settings(
        environment=f"  {environment.upper()}  ",
        trust_platform_identity_headers=True,
        entra_auth_enforced=False,
        agent_studio_attestation_signing_key="operator-key",
        agent_studio_attestation_signing_key_version="v1",
    )
    assert settings.trust_platform_identity_headers is True


@pytest.mark.parametrize("environment", ["production", "prod", "staging", "PRODUCTION", "unknown-env"])
def test_trusting_platform_headers_without_entra_enforcement_is_refused_outside_safe_environments(
    environment: str,
) -> None:
    with pytest.raises(ValidationError, match="RESEARCH_ENTRA_AUTH_ENFORCED"):
        Settings(
            environment=environment,
            trust_platform_identity_headers=True,
            entra_auth_enforced=False,
            allow_demo_identity=False,
            agent_studio_attestation_signing_key="operator-key",
            agent_studio_attestation_signing_key_version="v1",
        )


@pytest.mark.parametrize("environment", ["production", "prod", "staging", "unknown-env"])
def test_trusting_platform_headers_with_confirmed_entra_enforcement_is_permitted_in_every_environment(
    environment: str,
) -> None:
    settings = Settings(
        environment=environment,
        trust_platform_identity_headers=True,
        entra_auth_enforced=True,
        allow_demo_identity=False,
        agent_studio_attestation_signing_key="operator-key",
        agent_studio_attestation_signing_key_version="v1",
    )
    assert settings.entra_auth_enforced is True
    assert settings.environment == environment


@pytest.mark.parametrize("environment", ["production", "prod", "staging", "unknown-env"])
def test_untrusted_platform_headers_are_always_permitted_regardless_of_entra_enforcement(
    environment: str,
) -> None:
    # trust_platform_identity_headers stays at its default (False), so the
    # guard must not fire even though entra_auth_enforced is also left
    # unset/False and the environment is production-like -- there is no
    # header-trust boundary in play to confirm.
    settings = Settings(
        environment=environment,
        allow_demo_identity=False,
        agent_studio_attestation_signing_key="operator-key",
        agent_studio_attestation_signing_key_version="v1",
    )
    assert settings.trust_platform_identity_headers is False
    assert settings.entra_auth_enforced is False
