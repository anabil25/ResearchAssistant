"""Environment policy tests for ``Settings``.

These cover the production guard on ``allow_demo_identity`` (Phase2 review
finding #4): the unauthenticated "demo sandbox" identity bypass
(``research_assistant_api.identity.DEMO_SANDBOX_SOURCE``) must be an
explicit, impossible-to-misconfigure-into-production local/dev/test
adapter, never a silent default that survives into a production
deployment through a missing or unrecognized ``RESEARCH_ENVIRONMENT``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from research_assistant_api.config import DEMO_IDENTITY_SAFE_ENVIRONMENTS, Settings


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
    settings = Settings(environment=environment, allow_demo_identity=False)
    assert settings.allow_demo_identity is False
    assert settings.environment == environment
