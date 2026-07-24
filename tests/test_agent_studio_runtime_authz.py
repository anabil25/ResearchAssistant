from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from research_assistant_api.agent_studio.models import DeploymentEnvironment
from research_assistant_api.agent_studio.runtime_authz import (
    RuntimeAuthorizationError,
    RuntimeAuthPolicy,
    RuntimeAuthzDecision,
    RuntimeAuthzReason,
    RuntimePrincipal,
    authorize_runtime_request,
    enforce_runtime_authorization,
    uniform_denial,
)
from research_assistant_api.agent_studio.runtime_deployment_mapping import (
    AllowedClientAppRoleBinding,
    RuntimeBindingDescriptor,
    RuntimeDeploymentMapping,
    RuntimeDescriptorRef,
    RuntimeDestinationHashPolicy,
    RuntimeMappingLifecycleState,
    RuntimeOperationRef,
)

ISSUER = "https://login.microsoftonline.com/tenant-1/v2.0"
AUDIENCE = "api://research-assistant-runtime"
RUNTIME_ROLE = "research-assistant.runtime"
CLIENT_APP_ID = "client-app-1"
FIXED_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _mapping(
    *,
    deployment_id: str = "dep-1",
    lifecycle_state: RuntimeMappingLifecycleState = RuntimeMappingLifecycleState.ACTIVE,
    allowed: tuple[AllowedClientAppRoleBinding, ...] | None = None,
) -> RuntimeDeploymentMapping:
    binding = RuntimeBindingDescriptor(
        binding_id="binding-1",
        provider_contract_version="provider.contract.v7",
        descriptor_ref=RuntimeDescriptorRef(id="foundry.azure_ai_search", version="1", digest="sha256:aa"),
        operation_ref=RuntimeOperationRef(id="search", version="1"),
        destination_hash_policy=RuntimeDestinationHashPolicy(binding_id="binding-1", operation_id="search"),
    )
    return RuntimeDeploymentMapping(
        deployment_id=deployment_id,
        tenant_id="tenant-1",
        project_id="project-1",
        environment=DeploymentEnvironment.DEVELOPMENT,
        logical_agent_id="agent-1",
        harness_release_id="harness-release-1",
        harness_manifest_digest="sha256:harness",
        backend_release_id="backend-release-1",
        backend_version="1.2.3",
        provider_contract_version="provider.contract.v7",
        provider_artifact_digest="sha256:provider-artifact",
        binding=binding,
        allowed_client_app_role_bindings=(
            allowed
            if allowed is not None
            else (AllowedClientAppRoleBinding(client_app_id=CLIENT_APP_ID, app_role=RUNTIME_ROLE),)
        ),
        lifecycle_state=lifecycle_state,
        created_at=FIXED_NOW,
        created_by="release-service",
    )


def _policy() -> RuntimeAuthPolicy:
    return RuntimeAuthPolicy(expected_issuer=ISSUER, expected_audience=AUDIENCE, required_app_role=RUNTIME_ROLE)


def _principal(
    *,
    issuer: str = ISSUER,
    audiences: tuple[str, ...] = (AUDIENCE,),
    app_roles: tuple[str, ...] = (RUNTIME_ROLE,),
    client_app_id: str = CLIENT_APP_ID,
) -> RuntimePrincipal:
    return RuntimePrincipal(issuer=issuer, audiences=audiences, app_roles=app_roles, client_app_id=client_app_id)


class _Loader:
    """Models the server-owned client->deployment authority + mapping load.

    Returns the mapping only when called with the exact authorized
    ``(client_app_id, deployment_id)`` pair (constant-time equivalent), else
    ``None`` -- exactly like ``build_authorized_mapping_loader``.
    """

    def __init__(
        self,
        mapping: RuntimeDeploymentMapping | None,
        *,
        authorized_client: str = CLIENT_APP_ID,
        authorized_deployment: str = "dep-1",
    ) -> None:
        self._mapping = mapping
        self._authorized_client = authorized_client
        self._authorized_deployment = authorized_deployment
        self.calls = 0

    def __call__(self, client_app_id: str, deployment_id: str) -> RuntimeDeploymentMapping | None:
        self.calls += 1
        if client_app_id == self._authorized_client and deployment_id == self._authorized_deployment:
            return self._mapping
        return None


def _authorize(
    mapping: RuntimeDeploymentMapping | None,
    *,
    principal: RuntimePrincipal | None = None,
    loader: _Loader | None = None,
    **overrides: object,
) -> tuple[RuntimeAuthzDecision, _Loader]:
    presented = mapping if mapping is not None else _mapping()
    resolved_principal = principal if principal is not None else _principal()
    if loader is None:
        # Default: the authenticated client is bound to exactly this deployment.
        loader = _Loader(
            mapping,
            authorized_client=resolved_principal.client_app_id,
            authorized_deployment=presented.deployment_id,
        )
    kwargs = {
        "policy": _policy(),
        "principal": resolved_principal,
        "presented_deployment_id": presented.deployment_id,
        "presented_mapping_ref": presented.mapping_ref,
        "presented_mapping_digest": presented.mapping_digest,
        "load_authorized_mapping": loader,
        "now": FIXED_NOW,
    }
    kwargs.update(overrides)
    decision = authorize_runtime_request(**kwargs)  # type: ignore[arg-type]
    return decision, loader


# --- happy path ------------------------------------------------------------


def test_authorized_when_all_checks_pass() -> None:
    mapping = _mapping()
    decision, loader = _authorize(mapping)
    assert decision.authorized
    assert decision.reason is RuntimeAuthzReason.AUTHORIZED
    assert decision.mapping is mapping
    assert loader.calls == 1


# --- pre-load gates (loader must NOT run) -----------------------------------


def test_issuer_mismatch_denies_without_loading_mapping() -> None:
    decision, loader = _authorize(_mapping(), principal=_principal(issuer="https://evil.example/v2.0"))
    assert decision.reason is RuntimeAuthzReason.ISSUER_MISMATCH
    assert decision.mapping is None
    assert loader.calls == 0


def test_audience_mismatch_denies_without_loading_mapping() -> None:
    decision, loader = _authorize(_mapping(), principal=_principal(audiences=("api://someone-else",)))
    assert decision.reason is RuntimeAuthzReason.AUDIENCE_MISMATCH
    assert loader.calls == 0


def test_missing_app_role_denies_without_loading_mapping() -> None:
    decision, loader = _authorize(_mapping(), principal=_principal(app_roles=("some.other.role",)))
    assert decision.reason is RuntimeAuthzReason.MISSING_APP_ROLE
    assert loader.calls == 0


# --- post-load gates -------------------------------------------------------


def test_mapping_not_found_is_denied() -> None:
    decision, loader = _authorize(None)
    assert decision.reason is RuntimeAuthzReason.MAPPING_NOT_FOUND
    assert loader.calls == 1


def test_superseded_mapping_is_denied() -> None:
    mapping = _mapping(lifecycle_state=RuntimeMappingLifecycleState.SUPERSEDED)
    decision, _ = _authorize(mapping)
    assert decision.reason is RuntimeAuthzReason.MAPPING_SUPERSEDED


def test_retired_mapping_is_denied() -> None:
    mapping = _mapping(lifecycle_state=RuntimeMappingLifecycleState.RETIRED)
    decision, _ = _authorize(mapping)
    assert decision.reason is RuntimeAuthzReason.MAPPING_RETIRED


def test_not_yet_effective_mapping_is_denied() -> None:
    mapping = _mapping().model_copy(update={"created_at": FIXED_NOW + timedelta(days=1)})
    decision, _ = _authorize(mapping)
    assert decision.reason is RuntimeAuthzReason.MAPPING_NOT_YET_EFFECTIVE


def test_expired_mapping_is_denied_at_injected_now() -> None:
    mapping = _mapping().model_copy(update={"expires_at": FIXED_NOW - timedelta(hours=1)})
    decision, _ = _authorize(mapping)
    assert decision.reason is RuntimeAuthzReason.MAPPING_EXPIRED


def test_not_yet_expired_mapping_passes_lifecycle_at_injected_now() -> None:
    # The same mapping evaluated at an earlier injected instant is still valid,
    # proving expiry is decided against the supplied now, not the wall clock.
    mapping = _mapping().model_copy(update={"expires_at": FIXED_NOW + timedelta(hours=1)})
    decision, _ = _authorize(mapping)
    assert decision.authorized


def test_revoked_mapping_is_denied() -> None:
    mapping = _mapping().model_copy(update={"revoked_at": FIXED_NOW - timedelta(minutes=1)})
    decision, _ = _authorize(mapping)
    assert decision.reason is RuntimeAuthzReason.MAPPING_REVOKED


def test_authorize_rejects_naive_now() -> None:
    with pytest.raises(ValueError, match="must be timezone-aware"):
        _authorize(_mapping(), now=datetime(2026, 6, 1, 12, 0, 0))


def test_client_not_bound_is_uniform_not_found() -> None:
    # The authenticated client is bound to a DIFFERENT client id in the index,
    # so the loader returns None -> the same MAPPING_NOT_FOUND as no-such-mapping.
    mapping = _mapping()
    loader = _Loader(mapping, authorized_client="some-other-client", authorized_deployment="dep-1")
    decision, used = _authorize(mapping, loader=loader)
    assert decision.reason is RuntimeAuthzReason.MAPPING_NOT_FOUND
    assert decision.mapping is None
    assert used.calls == 1


def test_client_bound_to_other_deployment_is_uniform_not_found() -> None:
    # Bound client, but to a different deployment than the asserted one.
    mapping = _mapping(deployment_id="dep-1")
    loader = _Loader(mapping, authorized_client=CLIENT_APP_ID, authorized_deployment="dep-elsewhere")
    decision, _ = _authorize(mapping, loader=loader)
    assert decision.reason is RuntimeAuthzReason.MAPPING_NOT_FOUND


def test_misbehaving_loader_returning_wrong_deployment_is_denied() -> None:
    # Defense in depth: a loader that ignores the binding and returns a mapping
    # for a DIFFERENT deployment than asserted must still be caught by the
    # authorization function's own constant-time invariant, not trusted.
    other = _mapping(deployment_id="dep-OTHER")

    def _bad_loader(_client: str, _deployment: str) -> RuntimeDeploymentMapping:
        return other

    decision = authorize_runtime_request(
        policy=_policy(),
        principal=_principal(),
        presented_deployment_id="dep-1",
        presented_mapping_ref=other.mapping_ref,
        presented_mapping_digest=other.mapping_digest,
        load_authorized_mapping=_bad_loader,
        now=FIXED_NOW,
    )
    assert decision.reason is RuntimeAuthzReason.DEPLOYMENT_ID_MISMATCH


def test_client_not_in_allowlist_is_denied() -> None:
    mapping = _mapping()
    decision, _ = _authorize(mapping, principal=_principal(client_app_id="unknown-client"))
    assert decision.reason is RuntimeAuthzReason.CLIENT_NOT_ALLOWED


def test_client_present_but_role_not_held_is_denied() -> None:
    # Allowlist binds CLIENT_APP_ID to an admin role, but the principal only
    # holds the base runtime role -> the entry must not authorize.
    mapping = _mapping(
        allowed=(AllowedClientAppRoleBinding(client_app_id=CLIENT_APP_ID, app_role="research-assistant.runtime.admin"),)
    )
    decision, _ = _authorize(
        mapping,
        principal=_principal(app_roles=(RUNTIME_ROLE,)),
    )
    assert decision.reason is RuntimeAuthzReason.CLIENT_NOT_ALLOWED


def test_mapping_ref_mismatch_is_denied() -> None:
    mapping = _mapping()
    decision, _ = _authorize(mapping, presented_mapping_ref="runtime-deployment-mapping:v1:tampered")
    assert decision.reason is RuntimeAuthzReason.MAPPING_REF_MISMATCH


def test_mapping_digest_mismatch_is_denied() -> None:
    mapping = _mapping()
    decision, _ = _authorize(mapping, presented_mapping_digest="runtime-deployment-mapping:v1:sha256:deadbeef")
    assert decision.reason is RuntimeAuthzReason.MAPPING_DIGEST_MISMATCH


# --- enforce wrapper + uniform denial --------------------------------------


def test_enforce_returns_mapping_on_success() -> None:
    mapping = _mapping()
    returned = enforce_runtime_authorization(
        policy=_policy(),
        principal=_principal(),
        presented_deployment_id=mapping.deployment_id,
        presented_mapping_ref=mapping.mapping_ref,
        presented_mapping_digest=mapping.mapping_digest,
        load_authorized_mapping=lambda _client, _dep: mapping,
        now=FIXED_NOW,
    )
    assert returned is mapping


def test_enforce_raises_uniform_error_on_denial() -> None:
    with pytest.raises(RuntimeAuthorizationError) as excinfo:
        enforce_runtime_authorization(
            policy=_policy(),
            principal=_principal(app_roles=("nope",)),
            presented_deployment_id="dep-1",
            presented_mapping_ref="x",
            presented_mapping_digest="y",
            load_authorized_mapping=lambda _client, _dep: _mapping(),
            now=FIXED_NOW,
        )
    assert excinfo.value.decision.reason is RuntimeAuthzReason.MISSING_APP_ROLE


def test_uniform_denial_message_is_existence_agnostic() -> None:
    message = uniform_denial()
    assert "not available" in message
    # Must not reveal whether the deployment exists or the caller is forbidden.
    assert "forbidden" not in message.lower()
    assert "exist" not in message.lower()
