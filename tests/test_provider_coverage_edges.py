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
from research_assistant_connectors.providers._http import require_endpoint
from research_assistant_connectors.providers.contracts import _freeze, plain_json, validate_json
from research_assistant_connectors.providers.mcp import _safe_input_schema
from research_assistant_connectors.providers.openapi import _argument_schema


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
        consume_approval=lambda _: True,
    )


def approve(
    context: InvocationContext,
    discovery: Any,
    target: Any,
    operation_id: str,
    arguments: dict[str, Any],
) -> InvocationContext:
    operation = next(item for item in discovery.descriptor_for(target).operations if item.operation_id == operation_id)
    return replace(
        context,
        approval_decisions=(
            approval_decision(
                context,
                target=target,
                instance=target,
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

    provider = GitHubProvider(GitHubConfig("https://github.test", "tenant", AuthConfig(AuthMode.NONE)))
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
                "unsupported-key",
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
    containers = b"<R><Containers><Container><Name>c</Name></Container></Containers></R>"
    blob_context = ctx(lambda _: httpx.Response(200, content=containers))
    blob_instances = (
        AzureBlobProvider(BlobConfig("https://one.blob.test", "tenant")).discover(blob_context)[0],
        AzureBlobProvider(BlobConfig("https://two.blob.test", "tenant")).discover(blob_context)[0],
    )

    github_context = ctx(lambda _: httpx.Response(200, json=[{"full_name": "owner/repo"}]))
    github_instances = tuple(
        next(
            instance
            for instance in GitHubProvider(GitHubConfig(endpoint, "tenant", AuthConfig(AuthMode.NONE))).discover(
                github_context
            )
            if instance.name.endswith("issues-write")
        )
        for endpoint in ("https://one.github.test", "https://two.github.test")
    )

    def graph_handler(request: httpx.Request) -> httpx.Response:
        payload = {"value": [{"id": "site"}]} if request.url.path.endswith("/sites") else {"value": [{"id": "drive"}]}
        return httpx.Response(200, json=payload)

    graph_context = ctx(graph_handler)

    def graph_write(endpoint: str) -> Any:
        discovered = MicrosoftGraphProvider(GraphConfig(endpoint, "tenant", discover_items=False)).discover(
            graph_context
        )
        return next(
            instance
            for instance in discovered
            if discovered.descriptor_for(instance).operations[0].operation_id == "graph.drive.content.put"
        )

    graph_instances = tuple(
        graph_write(endpoint) for endpoint in ("https://one.graph.test/v1.0", "https://two.graph.test/v1.0")
    )

    for instances in (blob_instances, github_instances, graph_instances):
        assert capability_instance_fingerprint(instances[0]) != capability_instance_fingerprint(instances[1])


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
            InvocationRequest(capability, "mcp.tools.call", {}, "key"),
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
