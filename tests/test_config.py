"""Environment and authentication-switch behaviour for ``Settings``.

``entra_auth_enforced`` is the single authentication switch: false means no
gateway is in front of the API and ``resolve_identity`` issues the local
developer identity; true means a gateway validated the token and injected
``x-ms-client-principal``. Settings construction itself is deliberately
environment-agnostic so a deployment's own generated environment name can
never stop the API from starting.
"""

from __future__ import annotations

import pytest
from research_assistant_api.config import Settings


def test_default_settings_run_without_a_gateway_in_development() -> None:
    settings = Settings()
    assert settings.environment == "development"
    assert settings.entra_auth_enforced is False


@pytest.mark.parametrize(
    "environment",
    ["development", "test", "production", "staging", "unknown-env", "qv34itwn-azure"],
)
def test_settings_construct_in_any_environment(environment: str) -> None:
    """azd names each deployment from a random resource token, so no
    environment name may block startup."""
    assert Settings(environment=environment).environment == environment


@pytest.mark.parametrize("environment", ["development", "production", "qv34itwn-azure"])
def test_gateway_enforcement_is_independent_of_environment(environment: str) -> None:
    assert Settings(environment=environment, entra_auth_enforced=True).entra_auth_enforced is True
    assert Settings(environment=environment, entra_auth_enforced=False).entra_auth_enforced is False


