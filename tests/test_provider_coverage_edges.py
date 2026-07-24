from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from typing import Any

import httpx
import pytest
from research_assistant_connectors.providers import (
    AccessToken,
    ApprovalConsumptionRequest,
    ApprovalConsumptionResult,
    ApprovalConsumptionStatus,
    ApprovalPolicy,
    AuthConfig,
    AuthMode,
    AzureAISearchProvider,
    AzureBlobProvider,
    AzureFunctionsProvider,
    BlobConfig,
    FoundryConfig,
    FoundryProvider,
    FunctionPolicy,
    FunctionsConfig,
    GitHubConfig,
    GitHubProvider,
    GraphConfig,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    Maturity,
    MCPConfig,
    MCPStreamableHTTPProvider,
    MCPToolPolicy,
    MicrosoftGraphProvider,
    OpenAPIConfig,
    OpenAPIOperationPolicy,
    OpenAPIProvider,
    OperationClass,
    ProviderValidationError,
    Readiness,
    SearchConfig,
    UpstreamError,
    WebhookConfig,
    WebhookProvider,
    approval_decision,
    capability_instance_fingerprint,
)
from research_assistant_connectors.providers._http import decode_base64_limited, require_endpoint
from research_assistant_connectors.providers.contracts import _freeze, plain_json, validate_json
from research_assistant_connectors.providers.mcp import _safe_input_schema
from research_assistant_connectors.providers.openapi import _argument_schema


async def consume_approval(
    _request: ApprovalConsumptionRequest,
) -> ApprovalConsumptionResult:
    return ApprovalConsumptionResult(
        ApprovalConsumptionStatus.CONSUMED,
        "consumption-record",
        "2026-07-23T14:00:00Z",
    )


class Token:
    def get_token(self, *scopes: str) -> AccessToken:
        return AccessToken("token", 2_000_000_000)


def ctx(handler: Any, *, credential: object | None = None) -> InvocationContext:
    return InvocationContext(
        tenant_id="tenant",
        principal_id="principal",
        project_id="project",
        credential=Token() if credential is None else credential,
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        correlation_id="correlation",
        trace_id="trace",
        sleep=lambda _: None,
        release_id="release-1",
        invocation_id="invocation-1",
        consume_approval=consume_approval,
        logical_agent_id="agent-1",
    )


def approve(
    context: InvocationContext,
    discovery: Any,
    target: Any,
    operation_id: str,
    arguments: dict[str, Any],
) -> InvocationContext:
    descriptor = discovery.descriptor_for(target)
    operation = next(item for item in descriptor.operations if item.operation_id == operation_id)
    return replace(
        context,
        approval_decisions=(
            approval_decision(
                context,
                target=target,
                instance=target,
                descriptor=descriptor,
                operation=operation,
                arguments=arguments,
                decision_id=f"approve:{target.instance_id}:{operation_id}",
                expires_at="2999-01-01T00:00:00Z",
            ),
        ),
    )


def test_contract_and_http_edges() -> None:
    assert plain_json({"items": ({"value": 1},)}) == {"items": [{"value": 1}]}
    assert _freeze({"values"}) == frozenset({"values"})
    validate_json({"type": "array", "items": {"type": "string"}}, [])
    validate_json({"type": "array"}, [])
    with pytest.raises(ValueError, match="not configured"):
        require_endpoint(None)
    with pytest.raises(ProviderValidationError, match="upload limit"):
        decode_base64_limited(
            base64.b64encode(b"too-large").decode(),
            max_bytes=1,
            provider_id="provider",
        )


def test_remaining_provider_validation_edges() -> None:
    context = ctx(lambda _: httpx.Response(200, json={}))
    no_credential = replace(context, credential=object())
    oauth = AuthConfig(AuthMode.OAUTH, "scope", connection_ref="oauth-connection")
    with pytest.raises(ValueError, match="Connection version"):
        AuthConfig(AuthMode.OAUTH, connection_version="")
    with pytest.raises(ValueError, match="configured identity"):
        AuthConfig(AuthMode.OAUTH, identity_mode="")
    with pytest.raises(ValueError, match="connection roles"):
        AuthConfig(AuthMode.OAUTH, authorized_roles=("role", "role"))
    with pytest.raises(ValueError, match="connection roles"):
        AuthConfig(AuthMode.OAUTH, authorized_roles=("",))
    providers = (
        FoundryProvider(FoundryConfig("ftp://bad", "tenant", models_path="/models")),
        FoundryProvider(FoundryConfig("https://foundry.test", "tenant", oauth, models_path="/models")),
        AzureAISearchProvider(SearchConfig("ftp://bad", "tenant")),
        AzureBlobProvider(BlobConfig("ftp://bad", "tenant")),
        AzureBlobProvider(BlobConfig("https://blob.test", "tenant")),
        AzureFunctionsProvider(
            FunctionsConfig(
                "https://functions.test",
                "tenant",
                oauth,
                "https://functions.test/functions",
            )
        ),
        GitHubProvider(GitHubConfig("ftp://bad", "tenant", AuthConfig(AuthMode.NONE))),
        GitHubProvider(GitHubConfig("https://github.test", "tenant", oauth)),
        MicrosoftGraphProvider(GraphConfig("ftp://bad", "tenant")),
        MicrosoftGraphProvider(GraphConfig("https://graph.test/v1.0", "tenant")),
        MCPStreamableHTTPProvider(MCPConfig("ftp://bad", "tenant")),
        MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant", oauth)),
        OpenAPIProvider(OpenAPIConfig("ftp://bad", "tenant", document={"openapi": "3.1.0", "paths": {}})),
        OpenAPIProvider(OpenAPIConfig("https://api.test", "tenant")),
        OpenAPIProvider(
            OpenAPIConfig(
                "https://api.test",
                "tenant",
                oauth,
                document={"openapi": "3.1.0", "paths": {}},
            )
        ),
        OpenAPIProvider(
            OpenAPIConfig(
                "https://api.test",
                "tenant",
                document_url="ftp://bad",
            )
        ),
        WebhookProvider(WebhookConfig("ftp://bad", "tenant", "send")),
        WebhookProvider(WebhookConfig("https://hooks.test", "tenant", "send", auth=oauth)),
        WebhookProvider(
            WebhookConfig(
                "https://hooks.test",
                "tenant",
                "send",
                signing_algorithm="hmac",
            )
        ),
    )
    for provider in providers:
        target = provider.discover(no_credential)[-1]
        assert provider.validate(target, no_credential).readiness in {
            Readiness.MISCONFIGURED,
            Readiness.UNAUTHORIZED,
        }
    missing_tenants = (
        FoundryProvider(FoundryConfig("https://foundry.test", None, models_path="/models")),
        AzureAISearchProvider(SearchConfig("https://search.test", None)),
        AzureBlobProvider(BlobConfig("https://blob.test", None)),
        AzureFunctionsProvider(
            FunctionsConfig(
                "https://functions.test",
                None,
                AuthConfig(AuthMode.NONE),
                "https://functions.test/functions",
            )
        ),
        GitHubProvider(GitHubConfig("https://github.test", None, AuthConfig(AuthMode.NONE))),
        MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", None)),
        OpenAPIProvider(
            OpenAPIConfig(
                "https://api.test",
                None,
                document={"openapi": "3.1.0", "paths": {}},
            )
        ),
        WebhookProvider(WebhookConfig("https://hooks.test", None, "send")),
    )
    assert all(
        provider.validate(provider.discover(context)[-1], context).readiness is Readiness.MISCONFIGURED
        for provider in missing_tenants
    )


def test_foundry_unavailable_response_and_no_response_success() -> None:
    missing = FoundryProvider(FoundryConfig(None, "tenant", responses_path="/responses"))
    capabilities = missing.discover(ctx(lambda _: httpx.Response(500)))
    assert any(item.instance_id == "foundry.responses" for item in capabilities)

    provider = FoundryProvider(FoundryConfig("https://foundry.test", "tenant", models_path="/models"))
    context = ctx(lambda _: httpx.Response(200, json={"models": []}))
    assert all(item.instance_id != "foundry.responses" for item in provider.discover(context))

    vector_only = FoundryProvider(
        FoundryConfig(
            "https://foundry.test",
            "tenant",
            vector_stores_path="/vector-stores",
        )
    )
    vector_context = ctx(lambda _: httpx.Response(200, json={"data": [{"id": "vector-1"}]}))
    vector_discovery = vector_only.discover(vector_context)
    vector_resource = next(
        capability for capability in vector_discovery if capability.configuration.get("resource_id") == "vector-1"
    )
    vector_descriptor = vector_discovery.descriptor_for(vector_resource)
    assert vector_descriptor.resource_kind == "vector_store"
    assert vector_descriptor.operations[0].operation_id == "foundry.vector_stores.observe"

    knowledge_without_models = FoundryProvider(
        FoundryConfig(
            "https://foundry.test",
            "tenant",
            vector_stores_path="/vector-stores",
            responses_path="/responses",
        )
    )
    knowledge_discovery = knowledge_without_models.discover(vector_context)
    knowledge_resource = next(
        capability for capability in knowledge_discovery if capability.configuration.get("resource_id") == "vector-1"
    )
    assert knowledge_resource.readiness is Readiness.DEGRADED
    assert (
        "enum"
        not in knowledge_discovery.descriptor_for(knowledge_resource).operations[0].input_schema["properties"]["model"]
    )
    responses = next(capability for capability in knowledge_discovery if capability.instance_id == "foundry.responses")
    assert responses.readiness is Readiness.DEGRADED
    assert responses.unavailable_reason == "No model deployment was discovered"


def test_function_without_idempotency_key_and_github_issue_list() -> None:
    def function_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"value": [{"name": "Read"}]})
        assert "idempotency-key" not in request.headers
        return httpx.Response(200, json={"ok": True})

    function = AzureFunctionsProvider(
        FunctionsConfig(
            "https://functions.test",
            "tenant",
            AuthConfig(AuthMode.NONE),
            "https://functions.test/functions",
            function_policies=(
                FunctionPolicy(
                    "Read",
                    OperationClass.READ,
                    maturity=Maturity.GA,
                ),
            ),
        )
    )
    context = ctx(function_handler)
    function_discovery = function.discover(context)
    capability = function_discovery[0]
    assert function.invoke(
        InvocationRequest(capability, "functions.http.invoke", {}),
        approve(context, function_discovery, capability, "functions.http.invoke", {}),
    ).output["ok"]

    def github_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/repos":
            return httpx.Response(200, json=[{"full_name": "owner/repo"}])
        assert request.url.params["state"] == "all"
        return httpx.Response(200, json={"items": []})

    github = GitHubProvider(GitHubConfig("https://github.test", "tenant", AuthConfig(AuthMode.NONE)))
    github_context = ctx(github_handler)
    read = github.discover(github_context)[0]
    assert (
        github.invoke(
            InvocationRequest(read, "github.issues.list", {"state": "all"}),
            github_context,
        ).status_code
        == 200
    )


def test_github_writes_do_not_retry_when_an_idempotency_header_is_unsupported() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(200, json=[{"full_name": "owner/repository"}])
        calls += 1
        assert "idempotency-key" not in request.headers
        return httpx.Response(503)

    provider = GitHubProvider(
        GitHubConfig(
            "https://github.test",
            "tenant",
            AuthConfig(
                AuthMode.OAUTH,
                "github.scope",
                connection_ref="github-oauth",
            ),
        )
    )
    context = ctx(handler)
    github_discovery = provider.discover(context)
    write = next(capability for capability in github_discovery if capability.name.endswith("issues-write"))
    arguments = {"title": "No duplicate"}
    with pytest.raises(UpstreamError):
        provider.invoke(
            InvocationRequest(
                write,
                "github.issues.create",
                arguments,
            ),
            approve(context, github_discovery, write, "github.issues.create", arguments),
        )
    assert calls == 1


def test_blob_list_without_prefix() -> None:
    containers = b"<R><Containers><Container><Name>c</Name></Container></Containers></R>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "blob.test" and request.url.params.get("restype") is None:
            return httpx.Response(200, content=containers)
        assert "prefix" not in request.url.params
        return httpx.Response(200, content=b"<R><Blobs /></R>")

    provider = AzureBlobProvider(BlobConfig("https://blob.test", "tenant"))
    context = ctx(handler)
    capability = provider.discover(context)[0]
    assert (
        provider.invoke(
            InvocationRequest(capability, "blob.blobs.list", {}),
            context,
        ).output["blobs"]
        == ()
    )


def test_write_instance_fingerprints_pin_configured_origins() -> None:
    def fingerprint(discovered: Any, instance: Any) -> str:
        return capability_instance_fingerprint(
            instance,
            discovered.descriptor_for(instance),
            policy_ref="agent-studio-v1",
        )

    containers = b"<R><Containers><Container><Name>c</Name></Container></Containers></R>"
    blob_context = ctx(lambda _: httpx.Response(200, content=containers))
    blob_discoveries = (
        AzureBlobProvider(BlobConfig("https://one.blob.test", "tenant")).discover(blob_context),
        AzureBlobProvider(BlobConfig("https://two.blob.test", "tenant")).discover(blob_context),
    )
    blob_fingerprints = tuple(fingerprint(discovered, discovered[0]) for discovered in blob_discoveries)
    with pytest.raises(ValueError, match="cannot contain userinfo") as blob_userinfo_error:
        BlobConfig(
            "https://user:password@blob.test/container?sig=secret",
            "tenant",
        )
    assert str(blob_userinfo_error.value) == "Configured provider URLs cannot contain userinfo"
    assert "password" not in str(blob_userinfo_error.value)
    assert "secret" not in str(blob_userinfo_error.value)

    github_context = ctx(lambda _: httpx.Response(200, json=[{"full_name": "owner/repo"}]))
    github_fingerprints = []
    for endpoint in ("https://one.github.test", "https://two.github.test"):
        discovered = GitHubProvider(GitHubConfig(endpoint, "tenant", AuthConfig(AuthMode.NONE))).discover(
            github_context
        )
        instance = next(
            instance
            for instance in discovered
            if instance.name.endswith("issues-write")
        )
        github_fingerprints.append(fingerprint(discovered, instance))

    def graph_handler(request: httpx.Request) -> httpx.Response:
        payload = {"value": [{"id": "site"}]} if request.url.path.endswith("/sites") else {"value": [{"id": "drive"}]}
        return httpx.Response(200, json=payload)

    graph_context = ctx(graph_handler)

    def graph_fingerprint(endpoint: str) -> str:
        discovered = MicrosoftGraphProvider(GraphConfig(endpoint, "tenant", discover_items=False)).discover(
            graph_context
        )
        instance = next(
            instance
            for instance in discovered
            if discovered.descriptor_for(instance).operations[0].operation_id == "graph.drive.content.put"
        )
        return fingerprint(discovered, instance)

    graph_fingerprints = tuple(
        graph_fingerprint(endpoint) for endpoint in ("https://one.graph.test/v1.0", "https://two.graph.test/v1.0")
    )

    webhook_fingerprints = []
    for method in ("POST", "PUT"):
        discovered = WebhookProvider(
            WebhookConfig(
                "https://hooks.test/events",
                "tenant",
                "publish",
                method=method,
                health_method=None,
            )
        ).discover(ctx(lambda _: httpx.Response(200)))
        webhook_fingerprints.append(fingerprint(discovered, discovered[0]))

    signed_webhook_fingerprints = []
    for signature in ("first-secret", "second-secret"):
        discovered = WebhookProvider(
            WebhookConfig(
                f"https://hooks.test/events?sig={signature}",
                "tenant",
                "publish",
                health_method=None,
            )
        ).discover(ctx(lambda _: httpx.Response(200)))
        instance = discovered[0]
        serialized = json.dumps(
            {
                "descriptor": plain_json(discovered.descriptor_for(instance).metadata),
                "destinations": discovered.descriptor_for(instance).operations[0].side_effect_destinations,
                "configuration": plain_json(instance.configuration),
                "resource_id": instance.provider_resource_id,
            }
        )
        assert signature not in serialized
        signed_webhook_fingerprints.append(fingerprint(discovered, instance))
    with pytest.raises(ValueError, match="cannot contain userinfo") as userinfo_error:
        WebhookConfig(
            "https://user:password@hooks.test/events?sig=secret",
            "tenant",
            "publish",
            health_method=None,
        )
    assert str(userinfo_error.value) == "Configured provider URLs cannot contain userinfo"
    assert "password" not in str(userinfo_error.value)
    assert "secret" not in str(userinfo_error.value)
    path_secret_provider = WebhookProvider(
        WebhookConfig(
            "https://hooks.test/services/path-one",
            "tenant",
            "publish",
            health_method=None,
        )
    )
    path_secret_discovery = path_secret_provider.discover(
        ctx(lambda _: httpx.Response(200))
    )
    path_secret_instance = path_secret_discovery[0]
    path_secret_serialized = json.dumps(
        {
            "destinations": path_secret_discovery.descriptor_for(
                path_secret_instance
            ).operations[0].side_effect_destinations,
            "configuration": plain_json(path_secret_instance.configuration),
            "resource_id": path_secret_instance.provider_resource_id,
        }
    )
    assert "path-one" in path_secret_serialized
    malformed_webhook = WebhookProvider(
        WebhookConfig("https://[", "tenant", "publish", health_method=None)
    ).discover(ctx(lambda _: httpx.Response(200)))
    assert malformed_webhook[0].readiness is Readiness.MISCONFIGURED
    assert malformed_webhook[0].provider_resource_id == "invalid:webhook-destination"
    invalid_port_webhook = WebhookProvider(
        WebhookConfig(
            "https://hooks.test:notaport/events",
            "tenant",
            "publish",
            health_method=None,
        )
    ).discover(ctx(lambda _: httpx.Response(200)))
    assert invalid_port_webhook[0].readiness is Readiness.MISCONFIGURED

    openapi_document = {
        "openapi": "3.0.0",
        "paths": {
            "/ping": {
                "get": {
                    "operationId": "ping",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    openapi_fingerprints = []
    for endpoint in ("https://one.openapi.test", "https://two.openapi.test"):
        discovered = OpenAPIProvider(
            OpenAPIConfig(
                endpoint,
                "tenant",
                document=openapi_document,
                operation_policies=(
                    OpenAPIOperationPolicy(
                        "ping",
                        OperationClass.READ,
                        ApprovalPolicy.NEVER,
                        maturity=Maturity.GA,
                    ),
                ),
            )
        ).discover(ctx(lambda _: httpx.Response(200)))
        openapi_fingerprints.append(fingerprint(discovered, discovered[0]))
    managed_openapi = OpenAPIProvider(
        OpenAPIConfig(
            "https://managed.openapi.test",
            "tenant",
            AuthConfig(
                AuthMode.MANAGED_IDENTITY,
                "api://managed/.default",
                connection_ref="managed-connection",
            ),
            document=openapi_document,
            operation_policies=(
                OpenAPIOperationPolicy(
                    "ping",
                    OperationClass.READ,
                    ApprovalPolicy.NEVER,
                    maturity=Maturity.GA,
                ),
            ),
        )
    ).discover(ctx(lambda _: httpx.Response(200)))
    assert managed_openapi[0].auth_mode in managed_openapi.descriptor_for(
        managed_openapi[0]
    ).auth_modes

    foundry_fingerprints = []
    foundry_secrets = ("/responses/path-secret-one", "/responses/path-secret-two")
    for index, responses_path in enumerate(foundry_secrets):
        discovered = FoundryProvider(
            FoundryConfig(
                None,
                "tenant",
                responses_path=responses_path,
                api_version=f"api-secret-{index}",
            )
        ).discover(ctx(lambda _: httpx.Response(200)))
        instance = discovered[0]
        serialized = json.dumps(
            {
                "descriptor": plain_json(discovered.descriptor_for(instance).metadata),
                "configuration": plain_json(instance.configuration),
                "evidence": instance.status_evidence,
            }
        )
        assert responses_path not in serialized
        assert f"api-secret-{index}" not in serialized
        foundry_fingerprints.append(fingerprint(discovered, instance))

    function_fingerprints = []
    function_templates = ("/api/route-secret-one/{name}", "/api/route-secret-two/{name}")
    for invoke_path_template in function_templates:
        discovered = AzureFunctionsProvider(
            FunctionsConfig(
                None,
                "tenant",
                AuthConfig(AuthMode.NONE),
                invoke_path_template=invoke_path_template,
            )
        ).discover(ctx(lambda _: httpx.Response(200)))
        instance = discovered[0]
        assert invoke_path_template not in json.dumps(plain_json(instance.configuration))
        function_fingerprints.append(fingerprint(discovered, instance))

    fallback_fingerprints: list[tuple[str, str]] = []
    fallback_providers = (
        tuple(
            (
                secret,
                GitHubProvider(
                    GitHubConfig(
                        f"https://service.test/private/resource?token={secret}",
                        "tenant",
                        AuthConfig(AuthMode.NONE),
                    )
                ),
            )
            for secret in ("first-secret", "second-secret")
        ),
        tuple(
            (
                secret,
                MicrosoftGraphProvider(
                    GraphConfig(
                        f"https://service.test/private/resource?token={secret}",
                        "tenant",
                    )
                ),
            )
            for secret in ("first-secret", "second-secret")
        ),
    )
    for provider_group in fallback_providers:
        provider_fingerprints = []
        for secret, provider in provider_group:
            response_payload: Any = (
                [{"full_name": "owner/repo"}]
                if isinstance(provider, GitHubProvider)
                else {"value": []}
            )
            discovered = provider.discover(
                ctx(lambda _, payload=response_payload: httpx.Response(200, json=payload))
            )
            instance = next(
                (
                    candidate
                    for candidate in discovered
                    if candidate.instance_id.endswith(".configuration")
                ),
                discovered[0],
            )
            serialized = json.dumps(
                {
                    "configuration": plain_json(instance.configuration),
                    "evidence": instance.status_evidence,
                    "reason": instance.unavailable_reason,
                }
            )
            assert "user:password" not in serialized
            assert "password" not in serialized
            assert secret not in serialized
            provider_fingerprints.append(fingerprint(discovered, instance))
        fallback_fingerprints.append((provider_fingerprints[0], provider_fingerprints[1]))

    fingerprint_groups = (
        ("blob", blob_fingerprints),
        ("github", github_fingerprints),
        ("graph", graph_fingerprints),
        ("webhook", webhook_fingerprints),
        ("openapi", openapi_fingerprints),
        ("foundry", foundry_fingerprints),
        ("functions", function_fingerprints),
    )
    for label, fingerprints in fingerprint_groups:
        assert fingerprints[0] != fingerprints[1], label
    assert signed_webhook_fingerprints[0] == signed_webhook_fingerprints[1]
    assert fallback_fingerprints[0][0] == fallback_fingerprints[0][1]
    assert fallback_fingerprints[1][0] == fallback_fingerprints[1][1]


def test_provider_fingerprints_pin_auth_routing_and_protocol_versions() -> None:
    context = ctx(lambda _: httpx.Response(200))

    def fingerprint(provider: Any) -> str:
        discovered = provider.discover(context)
        instance = next(
            (
                candidate
                for candidate in discovered
                if candidate.instance_id.endswith(".configuration")
            ),
            discovered[0],
        )
        return capability_instance_fingerprint(
            instance,
            discovered.descriptor_for(instance),
            policy_ref="agent-studio-v1",
        )

    def api_key(header_name: str, *, mode: AuthMode = AuthMode.API_KEY) -> AuthConfig:
        return AuthConfig(
            mode,
            secret_name="provider-key",
            header_name=header_name,
            connection_ref="connection",
        )

    auth_routing_groups = (
        (
            AzureFunctionsProvider(FunctionsConfig(None, "tenant", api_key("x-key-one"))),
            AzureFunctionsProvider(FunctionsConfig(None, "tenant", api_key("x-key-two"))),
        ),
        (
            MCPStreamableHTTPProvider(MCPConfig(None, "tenant", api_key("x-key-one"))),
            MCPStreamableHTTPProvider(MCPConfig(None, "tenant", api_key("x-key-two"))),
        ),
        (
            OpenAPIProvider(OpenAPIConfig(None, "tenant", api_key("x-key-one"))),
            OpenAPIProvider(OpenAPIConfig(None, "tenant", api_key("x-key-two"))),
        ),
        (
            AzureAISearchProvider(SearchConfig(None, "tenant", api_key("x-key-one"))),
            AzureAISearchProvider(SearchConfig(None, "tenant", api_key("x-key-two"))),
        ),
        (
            AzureBlobProvider(
                BlobConfig(None, "tenant", api_key("x-key-one", mode=AuthMode.SHARED_KEY))
            ),
            AzureBlobProvider(
                BlobConfig(None, "tenant", api_key("x-key-two", mode=AuthMode.SHARED_KEY))
            ),
        ),
    )
    for providers in auth_routing_groups:
        assert fingerprint(providers[0]) != fingerprint(providers[1])

    protocol_version_groups = (
        (
            AzureBlobProvider(BlobConfig(None, "tenant", api_version="version-one")),
            AzureBlobProvider(BlobConfig(None, "tenant", api_version="version-two")),
        ),
        (
            AzureAISearchProvider(SearchConfig(None, "tenant", api_version="version-one")),
            AzureAISearchProvider(SearchConfig(None, "tenant", api_version="version-two")),
        ),
        (
            MCPStreamableHTTPProvider(MCPConfig(None, "tenant", protocol_version="version-one")),
            MCPStreamableHTTPProvider(MCPConfig(None, "tenant", protocol_version="version-two")),
        ),
    )
    for providers in protocol_version_groups:
        assert fingerprint(providers[0]) != fingerprint(providers[1])


def _mcp_response(request: httpx.Request, *, version: str = "2025-06-18", session: str | None = None) -> httpx.Response:
    body = json.loads(request.content)
    if body["method"] == "initialize":
        headers = {"Mcp-Session-Id": session} if session is not None else {}
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": version}},
            headers=headers,
        )
    return httpx.Response(202)


def test_mcp_schema_and_initialization_edges() -> None:
    assert _safe_input_schema({"type": "object", "properties": "bad", "required": "bad"}) == {
        "type": "object",
        "properties": {},
        "required": (),
        "additionalProperties": False,
    }
    schema = _safe_input_schema(
        {
            "type": "object",
            "properties": {1: {"type": "string"}, "x": "bad", "y": {"type": "unknown"}},
        }
    )
    assert schema["properties"] == {}
    response = httpx.Response(
        200,
        text='data: []\n\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n',
        headers={"content-type": "text/event-stream"},
    )
    assert MCPStreamableHTTPProvider._rpc_body(response, 1, "mcp") == {}

    bad_version = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    with pytest.raises(UpstreamError, match="protocol version"):
        bad_version._initialize_with_session(ctx(lambda request: _mcp_response(request, version="bad")))

    invalid_session = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    with pytest.raises(UpstreamError, match="session identifier"):
        invalid_session._initialize_with_session(ctx(lambda request: _mcp_response(request, session="bad session")))

    no_session = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    no_session_context = ctx(_mcp_response)
    no_session._initialize_with_session(no_session_context)
    assert no_session._sessions[("tenant", "project", "principal")] is None

    def bad_notification(request: httpx.Request) -> httpx.Response:
        response = _mcp_response(request)
        return httpx.Response(200) if json.loads(request.content)["method"] != "initialize" else response

    with pytest.raises(UpstreamError, match="not accepted"):
        MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))._initialize_with_session(
            ctx(bad_notification)
        )

    missing_session = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    missing_session_context = ctx(
        lambda request: (
            _mcp_response(request)
            if json.loads(request.content)["method"] in {"initialize", "notifications/initialized"}
            else httpx.Response(404)
        )
    )
    missing_session._initialize_with_session(missing_session_context)
    with pytest.raises(UpstreamError, match="endpoint returned HTTP 404"):
        missing_session._rpc("tools/list", {}, missing_session_context, idempotent=True, max_retries=1)

    sessions = 0
    list_requests = 0

    def repeatedly_stale(request: httpx.Request) -> httpx.Response:
        nonlocal sessions, list_requests
        body = json.loads(request.content)
        if body["method"] == "initialize":
            sessions += 1
            return _mcp_response(request, session=f"session-{sessions}")
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        list_requests += 1
        if list_requests == 1:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        return httpx.Response(404)

    repeatedly_stale_provider = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    repeatedly_stale_context = ctx(repeatedly_stale)
    repeatedly_stale_provider.discover(repeatedly_stale_context)
    with pytest.raises(UpstreamError, match="after reinitialization"):
        repeatedly_stale_provider._rpc(
            "tools/list",
            {},
            repeatedly_stale_context,
            idempotent=True,
            max_retries=1,
        )
    assert ("tenant", "project", "principal") not in repeatedly_stale_provider._sessions

    raced_provider = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    raced_sessions = 0
    raced_lists = 0

    def raced_stale(request: httpx.Request) -> httpx.Response:
        nonlocal raced_sessions, raced_lists
        body = json.loads(request.content)
        if body["method"] == "initialize":
            raced_sessions += 1
            return _mcp_response(request, session=f"race-{raced_sessions}")
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        raced_lists += 1
        if raced_lists == 1:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        if raced_lists == 3:
            raced_provider._sessions[("tenant", "project", "principal")] = "newer-session"
        return httpx.Response(404)

    raced_context = ctx(raced_stale)
    raced_provider.discover(raced_context)
    with pytest.raises(UpstreamError, match="after reinitialization"):
        raced_provider._rpc("tools/list", {}, raced_context, idempotent=True, max_retries=1)
    assert raced_provider._sessions[("tenant", "project", "principal")] == "newer-session"

    exhausted_provider = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    exhausted_calls = 0

    def exhausted_retry(request: httpx.Request) -> httpx.Response:
        nonlocal exhausted_calls
        body = json.loads(request.content)
        if body["method"] in {"initialize", "notifications/initialized"}:
            return _mcp_response(request, session="exhausted")
        exhausted_calls += 1
        if exhausted_calls == 1:
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
            )
        return httpx.Response(500) if exhausted_calls == 2 else httpx.Response(404)

    exhausted_context = ctx(exhausted_retry)
    exhausted_provider.discover(exhausted_context)
    with pytest.raises(UpstreamError, match="retry budget was exhausted"):
        exhausted_provider._rpc("tools/list", {}, exhausted_context, idempotent=True, max_retries=1)
    assert exhausted_calls == 3


def test_mcp_tools_shape_long_name_and_tool_error() -> None:
    mode = "shape"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "initialize":
            return _mcp_response(request)
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            tools: Any = {} if mode == "shape" else [{"name": "x" * 257}, {"name": "tool"}]
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": tools}})
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"isError": True}},
        )

    provider = MCPStreamableHTTPProvider(
        MCPConfig(
            "https://mcp.test",
            "tenant",
            tool_policies=(
                MCPToolPolicy(
                    "tool",
                    OperationClass.WRITE_IRREVERSIBLE,
                    ApprovalPolicy.REQUIRED,
                    Idempotency.PROVIDER_NATIVE,
                    Maturity.GA,
                ),
            ),
        )
    )
    context = ctx(handler)
    with pytest.raises(UpstreamError, match="tools array"):
        provider.discover(context)
    mode = "tools"
    mcp_discovery = provider.discover(context)
    capability = mcp_discovery[0]
    assert mcp_discovery.descriptor_for(capability).operations[0].max_retries == 0
    with pytest.raises(UpstreamError, match="execution error"):
        provider.invoke(
            InvocationRequest(capability, "mcp.tools.call", {}),
            approve(context, mcp_discovery, capability, "mcp.tools.call", {}),
        )


def test_openapi_schema_validation_and_render_edges() -> None:
    document: dict[str, Any] = {}
    assert _argument_schema(document, {"parameters": [None]}, {})["properties"]["path"]["properties"] == {}
    _argument_schema(document, {"parameters": [{"name": 1, "in": "path", "schema": {}}]}, {})
    optional = _argument_schema(
        document,
        {
            "parameters": [
                {"name": "id", "in": "path", "required": False, "schema": {}},
                {"name": "header", "in": "header", "schema": {}},
            ]
        },
        {},
    )
    assert optional["properties"]["path"]["required"] == []
    for body in (
        "bad",
        {"content": "bad"},
        {"content": {"application/json": "bad"}},
        {"content": {"application/json": {"schema": "bad"}}},
    ):
        _argument_schema(document, {}, {"requestBody": body})

    duplicate_document = {
        "openapi": "3.1.0",
        "paths": {
            "/none": None,
            "/one": {"get": {"operationId": "same"}, "post": {"operationId": None}},
            "/two": {"get": {"operationId": "same"}},
        },
    }
    provider = OpenAPIProvider(OpenAPIConfig("https://api.test", "tenant", document=duplicate_document))
    discovered = provider.discover(ctx(lambda _: httpx.Response(200)))
    assert len(discovered) == 2
    operation_mapping = {
        instance.configuration["path"]: (
            discovered.descriptor_for(instance).operations[0].operation_id,
            instance.configuration["source_operation_id"],
        )
        for instance in discovered
    }
    assert operation_mapping["/one"][1] == operation_mapping["/two"][1] == "same"
    assert operation_mapping["/one"][0] != operation_mapping["/two"][0]
    reversed_document = {
        "openapi": "3.1.0",
        "paths": {
            "/two": {"get": {"operationId": "same"}},
            "/one": {"post": {"operationId": None}, "get": {"operationId": "same"}},
            "/none": None,
        },
    }
    reversed_discovery = OpenAPIProvider(
        OpenAPIConfig("https://api.test", "tenant", document=reversed_document)
    ).discover(ctx(lambda _: httpx.Response(200)))
    assert {
        instance.configuration["path"]: (
            reversed_discovery.descriptor_for(instance).operations[0].operation_id,
            instance.configuration["source_operation_id"],
        )
        for instance in reversed_discovery
    } == operation_mapping
    assert all(
        re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{1,127}", operation_id)
        for operation_id, _ in operation_mapping.values()
    )
    dispatched_paths: list[str] = []

    def dispatch_handler(request: httpx.Request) -> httpx.Response:
        dispatched_paths.append(request.url.path)
        return httpx.Response(200, json={"path": request.url.path})

    configured_provider = OpenAPIProvider(
        OpenAPIConfig(
            "https://api.test",
            "tenant",
            document=duplicate_document,
            operation_policies=tuple(
                OpenAPIOperationPolicy(
                    operation_id,
                    OperationClass.READ,
                    ApprovalPolicy.NEVER,
                    maturity=Maturity.GA,
                )
                for operation_id, _ in operation_mapping.values()
            ),
        )
    )
    dispatch_context = ctx(dispatch_handler)
    configured_discovery = configured_provider.discover(dispatch_context)
    for instance in configured_discovery:
        operation_id = configured_discovery.descriptor_for(instance).operations[0].operation_id
        result = configured_provider.invoke(
            InvocationRequest(instance, operation_id, {}),
            dispatch_context,
        )
        assert result.output == {"path": instance.configuration["path"]}
    assert set(dispatched_paths) == {"/one", "/two"}
    with pytest.raises(ProviderValidationError, match="required"):
        OpenAPIProvider._render_path("/x/{id}", {})
    with pytest.raises(ProviderValidationError, match="malformed"):
        OpenAPIProvider._render_path("/x/{id", {"id": "x"})


def test_openapi_and_unsigned_webhook_idempotency_header() -> None:
    document = {
        "openapi": "3.1.0",
        "paths": {"/events": {"post": {"operationId": "publish"}}},
    }

    def api_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["idempotency-key"] == "key"
        return httpx.Response(200, json={"ok": True})

    provider = OpenAPIProvider(
        OpenAPIConfig(
            "https://api.test",
            "tenant",
            document=document,
            operation_policies=(
                OpenAPIOperationPolicy(
                    "publish",
                    OperationClass.PRIVILEGED,
                    ApprovalPolicy.REQUIRED,
                    Idempotency.CALLER_KEY,
                    Maturity.GA,
                ),
            ),
        )
    )
    context = ctx(api_handler)
    api_discovery = provider.discover(context)
    capability = api_discovery[0]
    assert provider.invoke(
        InvocationRequest(capability, "publish", {}, "key"),
        approve(context, api_discovery, capability, "publish", {}),
    ).output["ok"]

    def webhook_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["idempotency-key"] == "key"
        assert "x-signature" not in request.headers
        return httpx.Response(204)

    webhook = WebhookProvider(WebhookConfig("https://hooks.test", "tenant", "send", health_method=None))
    webhook_context = ctx(webhook_handler)
    webhook_discovery = webhook.discover(webhook_context)
    webhook_capability = webhook_discovery[0]
    assert (
        webhook.invoke(
            InvocationRequest(webhook_capability, "send", {}, "key"),
            approve(webhook_context, webhook_discovery, webhook_capability, "send", {}),
        ).status_code
        == 204
    )
