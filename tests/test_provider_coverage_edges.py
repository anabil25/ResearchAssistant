from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from research_assistant_connectors.providers import (
    AccessToken,
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
    MCPConfig,
    MCPStreamableHTTPProvider,
    MicrosoftGraphProvider,
    OpenAPIConfig,
    OpenAPIOperationPolicy,
    OpenAPIProvider,
    ProviderValidationError,
    Readiness,
    Risk,
    SearchConfig,
    UpstreamError,
    WebhookConfig,
    WebhookProvider,
)
from research_assistant_connectors.providers._http import require_endpoint
from research_assistant_connectors.providers.contracts import _freeze, plain_json, validate_json
from research_assistant_connectors.providers.mcp import _safe_input_schema
from research_assistant_connectors.providers.openapi import _argument_schema


class Token:
    def get_token(self, *scopes: str) -> AccessToken:
        return AccessToken("token", 2_000_000_000)


def ctx(handler: Any, *, credential: object | None = None) -> InvocationContext:
    return InvocationContext(
        "tenant",
        "principal",
        frozenset(),
        Token() if credential is None else credential,
        httpx.Client(transport=httpx.MockTransport(handler)),
        "correlation",
        "trace",
        lambda _: None,
    )


def test_contract_and_http_edges() -> None:
    assert plain_json({"items": ({"value": 1},)}) == {"items": [{"value": 1}]}
    assert _freeze({"values"}) == frozenset({"values"})
    validate_json({"type": "array", "items": {"type": "string"}}, [])
    validate_json({"type": "array"}, [])
    with pytest.raises(ValueError, match="not configured"):
        require_endpoint(None)


def test_remaining_provider_validation_edges() -> None:
    context = ctx(lambda _: httpx.Response(200, json={}))
    no_credential = replace(context, credential=object())
    oauth = AuthConfig(AuthMode.OAUTH, "scope")
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
        assert provider.validate(no_credential).readiness in {Readiness.MISCONFIGURED, Readiness.UNAUTHORIZED}
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
    assert all(provider.validate(context).readiness is Readiness.MISCONFIGURED for provider in missing_tenants)


def test_foundry_unavailable_response_and_no_response_success() -> None:
    missing = FoundryProvider(FoundryConfig(None, "tenant", responses_path="/responses"))
    capabilities = missing.discover(ctx(lambda _: httpx.Response(500)))
    assert any(item.capability_id == "foundry.responses" for item in capabilities)

    provider = FoundryProvider(FoundryConfig("https://foundry.test", "tenant", models_path="/models"))
    context = ctx(lambda _: httpx.Response(200, json={"models": []}))
    assert all(item.capability_id != "foundry.responses" for item in provider.discover(context))

    vector_only = FoundryProvider(
        FoundryConfig(
            "https://foundry.test",
            "tenant",
            vector_stores_path="/vector-stores",
        )
    )
    vector_context = ctx(
        lambda _: httpx.Response(200, json={"data": [{"id": "vector-1"}]})
    )
    vector_resource = next(
        capability
        for capability in vector_only.discover(vector_context)
        if capability.metadata.get("resource_id") == "vector-1"
    )
    assert vector_resource.resource_kind == "vector_store"
    assert vector_resource.operations[0].operation_id == "foundry.vector_stores.observe"

    knowledge_without_models = FoundryProvider(
        FoundryConfig(
            "https://foundry.test",
            "tenant",
            vector_stores_path="/vector-stores",
            responses_path="/responses",
        )
    )
    knowledge_resource = next(
        capability
        for capability in knowledge_without_models.discover(vector_context)
        if capability.metadata.get("resource_id") == "vector-1"
    )
    assert knowledge_resource.readiness is Readiness.DEGRADED
    assert knowledge_resource.attachable is False
    assert "enum" not in knowledge_resource.operations[0].input_schema["properties"]["model"]
    responses = next(
        capability
        for capability in knowledge_without_models.discover(vector_context)
        if capability.capability_id == "foundry.responses"
    )
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
            function_policies=(FunctionPolicy("Read", Risk.READ),),
        )
    )
    context = ctx(function_handler)
    capability = function.discover(context)[0]
    approved = replace(context, approved_capability_ids=frozenset({capability.capability_id}))
    assert function.invoke(
        InvocationRequest(capability.capability_id, "functions.http.invoke", {}),
        approved,
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
            InvocationRequest(read.capability_id, "github.issues.list", {"state": "all"}),
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
        GitHubConfig("https://github.test", "tenant", AuthConfig(AuthMode.NONE))
    )
    context = ctx(handler)
    write = next(
        capability
        for capability in provider.discover(context)
        if capability.name.endswith("issues-write")
    )
    approved = replace(
        context,
        approved_capability_ids=frozenset({write.capability_id}),
    )
    with pytest.raises(UpstreamError):
        provider.invoke(
            InvocationRequest(
                write.capability_id,
                "github.issues.create",
                {"title": "No duplicate"},
                "unsupported-key",
            ),
            approved,
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
            InvocationRequest(capability.capability_id, "blob.blobs.list", {}),
            context,
        ).output["blobs"]
        == ()
    )


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
    assert no_session._sessions[("tenant", "principal")] is None

    def bad_notification(request: httpx.Request) -> httpx.Response:
        response = _mcp_response(request)
        return httpx.Response(200) if json.loads(request.content)["method"] != "initialize" else response

    with pytest.raises(UpstreamError, match="not accepted"):
        MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))._initialize_with_session(
            ctx(bad_notification)
        )


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

    provider = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    context = ctx(handler)
    with pytest.raises(UpstreamError, match="tools array"):
        provider.discover(context)
    mode = "tools"
    capability = provider.discover(context)[0]
    approved = replace(context, approved_capability_ids=frozenset({capability.capability_id}))
    with pytest.raises(UpstreamError, match="execution error"):
        provider.invoke(
            InvocationRequest(capability.capability_id, "mcp.tools.call", {}, "key"),
            approved,
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
    assert len(provider.discover(ctx(lambda _: httpx.Response(200)))) == 1
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
                    Risk.EXTERNAL_SIDE_EFFECT,
                    ApprovalPolicy.REQUIRED,
                    Idempotency.REQUIRED,
                ),
            ),
        )
    )
    context = ctx(api_handler)
    capability = provider.discover(context)[0]
    approved = replace(context, approved_capability_ids=frozenset({capability.capability_id}))
    assert provider.invoke(
        InvocationRequest(capability.capability_id, "publish", {}, "key"),
        approved,
    ).output["ok"]

    def webhook_handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["idempotency-key"] == "key"
        assert "x-signature" not in request.headers
        return httpx.Response(204)

    webhook = WebhookProvider(WebhookConfig("https://hooks.test", "tenant", "send", health_method=None))
    webhook_context = ctx(webhook_handler)
    webhook_capability = webhook.discover(webhook_context)[0]
    webhook_approved = replace(
        webhook_context,
        approved_capability_ids=frozenset({webhook_capability.capability_id}),
    )
    assert (
        webhook.invoke(
            InvocationRequest(webhook_capability.capability_id, "send", {}, "key"),
            webhook_approved,
        ).status_code
        == 204
    )
