from __future__ import annotations

import base64
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
    MicrosoftGraphProvider,
    NeedsConsentError,
    OpenAPIConfig,
    OpenAPIOperationPolicy,
    OpenAPIProvider,
    PolicyError,
    ProviderValidationError,
    RateLimitError,
    Readiness,
    Risk,
    SearchConfig,
    UpstreamError,
    WebhookConfig,
    WebhookProvider,
)
from research_assistant_connectors.providers.mcp import _safe_input_schema


class Credential:
    signed_headers: dict[str, str] | None = None

    def get_token(self, *scopes: str) -> AccessToken:
        return AccessToken(f"token-for:{scopes[0]}", 2_000_000_000)

    def get_secret(self, name: str) -> str:
        return f"secret:{name}"

    def sign(self, payload: bytes, *, algorithm: str) -> str:
        return f"{algorithm}:{len(payload)}"

    def authorization(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        content_length: int,
    ) -> str:
        self.signed_headers = dict(headers)
        return f"SharedKey account:{method}:{content_length}"


def make_context(
    handler: Any,
    *,
    approved: frozenset[str] = frozenset(),
    tenant: str = "tenant",
    credential: object | None = None,
    sleeps: list[float] | None = None,
) -> InvocationContext:
    return InvocationContext(
        tenant,
        "principal",
        approved,
        Credential() if credential is None else credential,
        httpx.Client(transport=httpx.MockTransport(handler)),
        "correlation",
        "trace",
        (sleeps if sleeps is not None else []).append,
    )


def test_foundry_dynamic_discovery_invocation_and_preview_policy() -> None:
    calls: list[tuple[str, str]] = []
    response_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers["authorization"].startswith("Bearer ")
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"models": [{"id": "model-1", "name": "Model One"}, {}]})
        if request.url.path.endswith("/deployments"):
            return httpx.Response(200, json={"deployments": [{"name": "deployment-1"}]})
        if request.url.path.endswith("/agents"):
            return httpx.Response(200, json={"agents": [{"id": "agent-1"}]})
        if request.url.path.endswith("/connections"):
            return httpx.Response(200, json={"connections": [{"id": "connection-1"}]})
        if request.url.path.endswith("/vector-stores"):
            return httpx.Response(200, json={"data": [{"id": "vector-1", "name": "Project knowledge"}]})
        if request.url.path.endswith("/responses"):
            response_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "response-1", "status": "completed"})
        raise AssertionError(request.url)

    config = FoundryConfig(
        "https://foundry.test/project",
        "tenant",
        models_path="/models",
        deployments_path="/deployments",
        agents_path="/agents",
        connections_path="/connections",
        vector_stores_path="/vector-stores",
        responses_path="/responses",
    )
    provider = FoundryProvider(config)
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    assert provider.descriptor.capabilities[0].operations[0].maturity is Maturity.PREVIEW
    assert provider.descriptor.capabilities[0].attachable is False
    assert provider.health(ctx).readiness is Readiness.READY
    responses = next(item for item in capabilities if item.capability_id == "foundry.responses")
    approved = replace(ctx, approved_capability_ids=frozenset({responses.capability_id}))
    result = provider.invoke(
        InvocationRequest(
            responses.capability_id,
            "foundry.responses.create",
            {"model": "deployment-1", "input": "Summarize", "conversation": "conversation-1"},
            "request-1",
        ),
        approved,
    )
    assert result.output["status"] == "completed"
    assert result.audit_metadata["attempts"] == 1
    models = next(item for item in capabilities if item.capability_id == "foundry.models.inventory")
    listed = provider.invoke(InvocationRequest(models.capability_id, "foundry.models.list", {}), ctx)
    assert listed.output["models"][0]["id"] == "model-1"
    knowledge = next(item for item in capabilities if item.resource_kind == "project_knowledge")
    knowledge_context = replace(
        ctx,
        approved_capability_ids=frozenset({knowledge.capability_id}),
    )
    provider.invoke(
        InvocationRequest(
            knowledge.capability_id,
            "foundry.file_search.query",
            {
                "model": "deployment-1",
                "input": "Find evidence",
                "max_num_results": 3,
            },
            "request-2",
        ),
        knowledge_context,
    )
    provider.invoke(
        InvocationRequest(
            knowledge.capability_id,
            "foundry.file_search.query",
            {"model": "deployment-1", "input": "Find more evidence"},
            "request-2b",
        ),
        knowledge_context,
    )
    assert response_bodies[0] == {
        "model": "deployment-1",
        "input": "Summarize",
        "conversation": "conversation-1",
    }
    assert response_bodies[1]["tools"] == [
        {
            "type": "file_search",
            "vector_store_ids": ["vector-1"],
            "max_num_results": 3,
        }
    ]
    assert response_bodies[2]["tools"] == [
        {"type": "file_search", "vector_store_ids": ["vector-1"]}
    ]
    with pytest.raises(ProviderValidationError, match="declared values"):
        provider.invoke(
            InvocationRequest(
                responses.capability_id,
                "foundry.responses.create",
                {"model": "unknown", "input": "Unsafe"},
                "request-unknown-model",
            ),
            approved,
        )
    with pytest.raises(ProviderValidationError, match="unsupported"):
        provider.invoke(
            InvocationRequest(
                responses.capability_id,
                "foundry.responses.create",
                {
                    "model": "deployment-1",
                    "input": "Unsafe",
                    "tools": [{"type": "memory"}],
                },
                "request-3",
            ),
            approved,
        )
    assert ("POST", "/project/responses") in calls


def test_foundry_missing_configuration_tenant_auth_and_degraded_discovery() -> None:
    ctx = make_context(lambda _: httpx.Response(503))
    provider = FoundryProvider(FoundryConfig(None, "tenant"))
    assert provider.validate(ctx).readiness is Readiness.MISCONFIGURED
    capabilities = provider.discover(ctx)
    assert all(item.readiness is not Readiness.READY for item in capabilities)
    assert provider.health(ctx).readiness is Readiness.MISCONFIGURED
    wrong_tenant = FoundryProvider(FoundryConfig("https://foundry.test", "other", models_path="/models"))
    assert wrong_tenant.validate(ctx).readiness is Readiness.UNAUTHORIZED
    no_credential = replace(ctx, credential=object())
    auth_provider = FoundryProvider(FoundryConfig("https://foundry.test", "tenant", models_path="/models"))
    assert auth_provider.validate(no_credential).readiness is Readiness.UNAUTHORIZED
    no_paths = FoundryProvider(FoundryConfig("https://foundry.test", "tenant"))
    assert no_paths.validate(ctx).readiness is Readiness.MISCONFIGURED
    partial = FoundryProvider(
        FoundryConfig("https://foundry.test", "tenant", models_path="/models", responses_path="/responses")
    )
    degraded = partial.discover(ctx)
    assert (
        next(item for item in degraded if item.capability_id == "foundry.models.inventory").readiness
        is Readiness.DEGRADED
    )
    assert (
        next(item for item in degraded if item.capability_id == "foundry.agents.inventory").readiness
        is Readiness.MISCONFIGURED
    )
    assert next(item for item in degraded if item.capability_id == "foundry.responses").readiness is Readiness.DEGRADED


def test_search_discovers_indexes_retries_and_queries_documents() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith("/indexes"):
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, headers={"Retry-After": "0"})
            return httpx.Response(200, json={"value": [{"name": "research/2026"}, {}]})
        assert request.url.raw_path.startswith(b"/indexes/research%2F2026/docs/search")
        assert request.url.params["api-version"] == "2025-09-01"
        return httpx.Response(200, json={"value": [{"id": "1"}]})

    provider = AzureAISearchProvider(SearchConfig("https://search.test", "tenant"))
    ctx = make_context(handler, sleeps=sleeps)
    capabilities = provider.discover(ctx)
    assert len(capabilities) == 1
    result = provider.invoke(
        InvocationRequest(capabilities[0].capability_id, "search.documents.query", {"search": "evidence", "top": 2}),
        ctx,
    )
    assert result.output["value"][0]["id"] == "1"
    assert sleeps == [0.0]
    assert provider.health(ctx).readiness is Readiness.READY


def test_search_missing_configuration_and_empty_discovery() -> None:
    ctx = make_context(lambda _: httpx.Response(200, json={"value": []}))
    missing = AzureAISearchProvider(SearchConfig(None, "tenant"))
    assert missing.discover(ctx)[0].readiness is Readiness.MISCONFIGURED
    assert missing.health(ctx).readiness is Readiness.DEGRADED
    wrong = AzureAISearchProvider(SearchConfig("https://search.test", "other"))
    assert wrong.validate(ctx).readiness is Readiness.UNAUTHORIZED
    no_auth = replace(ctx, credential=object())
    assert (
        AzureAISearchProvider(SearchConfig("https://search.test", "tenant")).validate(no_auth).readiness
        is Readiness.UNAUTHORIZED
    )
    empty = AzureAISearchProvider(SearchConfig("https://search.test", "tenant"))
    assert empty.discover(ctx) == ()
    assert empty.health(ctx).readiness is Readiness.DEGRADED


def test_functions_admin_discovery_policy_and_json_and_text_invocation() -> None:
    invoked = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal invoked
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"functions": [{"name": "SafeRead"}, {"name": "bad/name"}, {"name": ""}]},
            )
        invoked += 1
        assert request.headers["idempotency-key"] == "key"
        if invoked == 1:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    config = FunctionsConfig(
        "https://functions.test",
        "tenant",
        AuthConfig(AuthMode.API_KEY, secret_name="function-key", header_name="x-functions-key"),
        "https://functions.test/admin/functions",
        "admin",
        function_policies=(
            FunctionPolicy(
                "SafeRead",
                Risk.READ,
                ApprovalPolicy.NEVER,
                Idempotency.REQUIRED,
            ),
        ),
    )
    provider = AzureFunctionsProvider(config)
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    assert [item.name for item in capabilities] == ["SafeRead"]
    request = InvocationRequest(capabilities[0].capability_id, "functions.http.invoke", {"query": "x"}, "key")
    assert provider.invoke(request, ctx).output["ok"] is True
    assert provider.invoke(request, ctx).output == "ok"
    assert provider.health(ctx).readiness is Readiness.READY


def test_functions_validation_and_default_restrictive_policy() -> None:
    ctx = make_context(lambda _: httpx.Response(200, json={"value": [{"name": "Run"}]}))
    for config in (
        FunctionsConfig(None, "tenant", AuthConfig(AuthMode.NONE)),
        FunctionsConfig(
            "https://functions.test",
            "tenant",
            AuthConfig(AuthMode.NONE),
            "https://functions.test/functions",
            "invalid",
        ),
        FunctionsConfig(
            "https://functions.test",
            "tenant",
            AuthConfig(AuthMode.NONE),
            "https://functions.test/functions",
            invoke_path_template="/api/static",
        ),
        FunctionsConfig(
            "https://functions.test",
            "tenant",
            AuthConfig(AuthMode.NONE),
            "https://other.test/functions",
            "admin",
        ),
    ):
        assert AzureFunctionsProvider(config).validate(ctx).readiness is Readiness.MISCONFIGURED
    wrong = AzureFunctionsProvider(
        FunctionsConfig(
            "https://functions.test",
            "other",
            AuthConfig(AuthMode.NONE),
            "https://functions.test/functions",
        )
    )
    assert wrong.validate(ctx).readiness is Readiness.UNAUTHORIZED
    provider = AzureFunctionsProvider(
        FunctionsConfig(
            "https://functions.test",
            "tenant",
            AuthConfig(AuthMode.NONE),
            "https://functions.test/functions",
        )
    )
    capability = provider.discover(ctx)[0]
    assert capability.operations[0].risk is Risk.EXTERNAL_SIDE_EFFECT
    with pytest.raises(PolicyError):
        provider.invoke(InvocationRequest(capability.capability_id, "functions.http.invoke", {}), ctx)


def test_blob_discovery_list_get_put_and_shared_key_headers() -> None:
    requests: list[httpx.Request] = []
    containers_xml = (
        b"<EnumerationResults><Containers><Container><Name>research</Name></Container></Containers></EnumerationResults>"
    )
    blobs_xml = b"<EnumerationResults><Blobs><Blob><Name>folder/a.txt</Name></Blob></Blobs></EnumerationResults>"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["x-ms-version"] == "2023-11-03"
        if request.url.params.get("comp") == "list" and request.url.params.get("restype") is None:
            return httpx.Response(200, content=containers_xml)
        if request.url.params.get("comp") == "list":
            return httpx.Response(200, content=blobs_xml)
        if request.method == "GET":
            return httpx.Response(200, content=b"blob-data", headers={"etag": "etag"})
        return httpx.Response(201, headers={"etag": "new", "x-ms-version-id": "v1"})

    provider = AzureBlobProvider(BlobConfig("https://account.blob.core.windows.net", "tenant"))
    ctx = make_context(handler)
    capability = provider.discover(ctx)[0]
    listed = provider.invoke(
        InvocationRequest(capability.capability_id, "blob.blobs.list", {"prefix": "folder/"}),
        ctx,
    )
    assert listed.output["blobs"] == ("folder/a.txt",)
    fetched = provider.invoke(
        InvocationRequest(capability.capability_id, "blob.get", {"blob": "folder/a.txt"}),
        ctx,
    )
    assert base64.b64decode(fetched.output["content_base64"]) == b"blob-data"
    approved = replace(ctx, approved_capability_ids=frozenset({capability.capability_id}))
    written = provider.invoke(
        InvocationRequest(
            capability.capability_id,
            "blob.put",
            {"blob": "folder/a.txt", "content_base64": base64.b64encode(b"new").decode(), "content_type": "text/plain"},
        ),
        approved,
    )
    assert written.output["version_id"] == "v1"
    assert any(request.headers.get("x-ms-blob-type") == "BlockBlob" for request in requests)

    shared = AzureBlobProvider(
        BlobConfig(
            "https://account.blob.core.windows.net",
            "tenant",
            AuthConfig(AuthMode.SHARED_KEY),
        )
    )
    shared_context = make_context(handler)
    assert shared.discover(shared_context)[0].readiness is Readiness.READY
    shared_capability = shared.discover(shared_context)[0]
    shared_approved = replace(
        shared_context,
        approved_capability_ids=frozenset({shared_capability.capability_id}),
    )
    shared.invoke(
        InvocationRequest(
            shared_capability.capability_id,
            "blob.put",
            {"blob": "signed.txt", "content_base64": "YQ==", "content_type": "text/plain"},
        ),
        shared_approved,
    )
    credential = shared_context.credential
    assert isinstance(credential, Credential)
    assert credential.signed_headers is not None
    assert credential.signed_headers["x-ms-blob-type"] == "BlockBlob"
    assert credential.signed_headers["Content-Type"] == "text/plain"
    assert any(request.headers["authorization"].startswith("SharedKey") for request in requests)


def test_blob_validation_xml_path_and_content_failures() -> None:
    ctx = make_context(lambda _: httpx.Response(200, content=b"bad xml"))
    missing = AzureBlobProvider(BlobConfig(None, "tenant"))
    assert missing.discover(ctx)[0].readiness is Readiness.MISCONFIGURED
    assert missing.health(ctx).readiness is Readiness.MISCONFIGURED
    wrong = AzureBlobProvider(BlobConfig("https://blob.test", "other"))
    assert wrong.validate(ctx).readiness is Readiness.UNAUTHORIZED
    with pytest.raises(ProviderValidationError, match="invalid XML"):
        AzureBlobProvider(BlobConfig("https://blob.test", "tenant")).discover(ctx)
    with pytest.raises(ProviderValidationError, match="safe relative"):
        AzureBlobProvider._blob_path("container", "../bad")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<EnumerationResults><Containers><Container><Name>c</Name></Container></Containers></EnumerationResults>",
        )

    provider = AzureBlobProvider(BlobConfig("https://blob.test", "tenant"))
    valid_ctx = make_context(handler)
    capability = provider.discover(valid_ctx)[0]
    approved = replace(valid_ctx, approved_capability_ids=frozenset({capability.capability_id}))
    with pytest.raises(ProviderValidationError, match="invalid"):
        provider.invoke(
            InvocationRequest(capability.capability_id, "blob.put", {"blob": "a", "content_base64": "***"}),
            approved,
        )


def test_mcp_protocol_session_untrusted_annotations_and_tool_call() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = body["method"]
        methods.append(method)
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                headers={"Mcp-Session-Id": "session-1"},
            )
        assert request.headers["mcp-session-id"] == "session-1"
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {
                        "tools": [
                            {
                                "name": "write_everything",
                                "description": "pretend safe",
                                "annotations": {"readOnlyHint": True},
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "value": {"type": "string"},
                                        "ignored": {"type": "made-up"},
                                    },
                                    "required": ["value", "missing"],
                                },
                            },
                            {"name": ""},
                            "bad",
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "done"}]}},
        )

    provider = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    ctx = make_context(handler)
    capability = provider.discover(ctx)[0]
    assert capability.operations[0].risk is Risk.EXTERNAL_SIDE_EFFECT
    assert capability.metadata["untrusted_tool_metadata"]["annotations"]["readOnlyHint"] is True
    assert tuple(capability.operations[0].input_schema["required"]) == ("value",)
    approved = replace(ctx, approved_capability_ids=frozenset({capability.capability_id}))
    result = provider.invoke(
        InvocationRequest(capability.capability_id, "mcp.tools.call", {"value": "x"}, "idempotency"),
        approved,
    )
    assert result.output["content"][0]["text"] == "done"
    assert methods.count("initialize") == 1
    assert provider.health(ctx).readiness is Readiness.READY
    other_principal = replace(ctx, principal_id="other-principal")
    provider.discover(other_principal)
    assert methods.count("initialize") == 2


def test_mcp_validation_sse_and_protocol_failures() -> None:
    ctx = make_context(lambda _: httpx.Response(500))
    missing = MCPStreamableHTTPProvider(MCPConfig(None, "tenant"))
    assert missing.discover(ctx)[0].readiness is Readiness.MISCONFIGURED
    assert missing.health(ctx).readiness is Readiness.MISCONFIGURED
    wrong = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "other"))
    assert wrong.validate(ctx).readiness is Readiness.UNAUTHORIZED
    assert _safe_input_schema("bad") == {"type": "object"}

    sse = httpx.Response(
        200,
        text='data: invalid\n\ndata: {"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n',
        headers={"content-type": "text/event-stream"},
    )
    assert MCPStreamableHTTPProvider._rpc_body(sse, 7, "mcp") == {"ok": True}
    for response, message in (
        (
            httpx.Response(
                200, text='data: {"jsonrpc":"2.0","id":8,"result":{}}\n', headers={"content-type": "text/event-stream"}
            ),
            "did not contain",
        ),
        (httpx.Response(200, json={"jsonrpc": "1.0", "id": 7, "result": {}}), "correlation"),
        (httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "error": {}}), "JSON-RPC error"),
        (httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": []}), "object result"),
    ):
        with pytest.raises(UpstreamError, match=message):
            MCPStreamableHTTPProvider._rpc_body(response, 7, "mcp")


def test_openapi_discovery_fixed_destination_policy_and_invocation() -> None:
    document = {
        "openapi": "3.1.0",
        "servers": [{"url": "https://evil.test"}],
        "components": {"schemas": {"Pet": {"type": "object"}}},
        "paths": {
            "/pets/{petId}": {
                "parameters": [
                    {"name": "petId", "in": "path", "required": True, "schema": {"type": "string"}},
                ],
                "get": {
                    "operationId": "getPet",
                    "parameters": [{"name": "expand", "in": "query", "schema": {"type": "string"}}],
                },
                "post": {
                    "operationId": "updatePet",
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}},
                },
                "delete": {},
            },
            "/https://evil.test/steal": {"get": {"operationId": "steal"}},
            "unsafe": {"get": {"operationId": "unsafe"}},
        },
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"url": str(request.url), "method": request.method})

    config = OpenAPIConfig(
        "https://api.test/v1",
        "tenant",
        document=document,
        operation_policies=(OpenAPIOperationPolicy("getPet", Risk.READ, ApprovalPolicy.NEVER),),
    )
    provider = OpenAPIProvider(config)
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    assert {item.name for item in capabilities} == {"getPet", "updatePet"}
    get_cap = next(item for item in capabilities if item.name == "getPet")
    result = provider.invoke(
        InvocationRequest(
            get_cap.capability_id,
            "getPet",
            {"path": {"petId": "a/b"}, "query": {"expand": "owner"}},
        ),
        ctx,
    )
    assert result.output["method"] == "GET"
    assert requests[-1].url.host == "api.test"
    assert b"a%2Fb" in requests[-1].url.raw_path
    update = next(item for item in capabilities if item.name == "updatePet")
    with pytest.raises(PolicyError):
        provider.invoke(
            InvocationRequest(update.capability_id, "updatePet", {"path": {"petId": "1"}, "body": {}}),
            ctx,
        )
    assert provider.health(ctx).readiness is Readiness.READY


def test_openapi_document_retrieval_validation_and_reference_failures() -> None:
    document = {"openapi": "3.0.3", "paths": {"/ping": {"get": {"operationId": "ping"}}}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/openapi.json":
            return httpx.Response(200, json=document)
        return httpx.Response(204)

    provider = OpenAPIProvider(
        OpenAPIConfig("https://api.test", "tenant", document_url="https://docs.test/openapi.json")
    )
    ctx = make_context(handler)
    assert provider.discover(ctx)[0].name == "ping"
    missing = OpenAPIProvider(OpenAPIConfig(None, "tenant"))
    assert missing.discover(ctx)[0].readiness is Readiness.MISCONFIGURED
    assert missing.health(ctx).readiness is Readiness.MISCONFIGURED
    wrong = OpenAPIProvider(OpenAPIConfig("https://api.test", "other", document=document))
    assert wrong.validate(ctx).readiness is Readiness.UNAUTHORIZED
    for bad, message in (
        ({"openapi": "2.0", "paths": {}}, "Only OpenAPI"),
        ({"openapi": "3.1.0"}, "paths object"),
        (
            {
                "openapi": "3.1.0",
                "paths": {"/x": {"get": {"operationId": "x", "parameters": [{"$ref": "https://evil"}]}}},
            },
            "safe local",
        ),
        (
            {
                "openapi": "3.1.0",
                "paths": {"/x": {"get": {"operationId": "x", "parameters": [{"$ref": "#/components/missing"}]}}},
            },
            "could not",
        ),
    ):
        with pytest.raises((ProviderValidationError, ValueError), match=message):
            OpenAPIProvider(OpenAPIConfig("https://api.test", "tenant", document=bad)).discover(ctx)


def test_webhook_fixed_url_signing_health_idempotency_and_validation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(204)
        return httpx.Response(202, json={"accepted": True})

    provider = WebhookProvider(
        WebhookConfig(
            "https://hooks.test/events",
            "tenant",
            "publish",
            signing_algorithm="hmac-sha256",
        )
    )
    ctx = make_context(handler)
    capability = provider.discover(ctx)[0]
    assert provider.health(ctx).readiness is Readiness.READY
    approved = replace(ctx, approved_capability_ids=frozenset({capability.capability_id}))
    with pytest.raises(PolicyError, match="idempotency"):
        provider.invoke(InvocationRequest(capability.capability_id, "publish", {"event": "x"}), approved)
    result = provider.invoke(
        InvocationRequest(capability.capability_id, "publish", {"event": "x"}, "event-1"),
        approved,
    )
    assert result.output["accepted"] is True
    assert requests[-1].headers["x-signature"].startswith("hmac-sha256:")
    assert requests[-1].url == httpx.URL("https://hooks.test/events")

    no_health = WebhookProvider(WebhookConfig("https://hooks.test", "tenant", "send", health_method=None))
    assert no_health.health(ctx).readiness is Readiness.READY
    for config in (
        WebhookConfig(None, "tenant", "send"),
        WebhookConfig("https://hooks.test", "tenant", "send", method="GET"),
    ):
        assert WebhookProvider(config).validate(ctx).readiness is Readiness.MISCONFIGURED
    assert (
        WebhookProvider(WebhookConfig("https://hooks.test", "other", "send")).validate(ctx).readiness
        is Readiness.UNAUTHORIZED
    )


def test_github_dynamic_repositories_read_write_rate_and_consent() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/orgs/acme/repos":
            return httpx.Response(200, json=[{"full_name": "acme/research"}, {}, "bad"])
        if request.method == "POST":
            assert "idempotency-key" not in request.headers
            return httpx.Response(201, json={"id": 2, "title": "Issue"})
        return httpx.Response(200, json={"full_name": "acme/research"})

    provider = GitHubProvider(
        GitHubConfig(
            "https://api.github.test",
            "tenant",
            AuthConfig(AuthMode.GITHUB_APP, "github"),
            owner="acme",
        )
    )
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    assert len(capabilities) == 2
    read = next(item for item in capabilities if item.name.endswith("read"))
    assert provider.invoke(InvocationRequest(read.capability_id, "github.repository.get", {}), ctx).status_code == 200
    write = next(item for item in capabilities if item.name.endswith("issues-write"))
    approved = replace(ctx, approved_capability_ids=frozenset({write.capability_id}))
    created = provider.invoke(
        InvocationRequest(write.capability_id, "github.issues.create", {"title": "Issue"}, "issue-1"),
        approved,
    )
    assert created.status_code == 201
    comment = provider.invoke(
        InvocationRequest(
            write.capability_id,
            "github.issue_comments.create",
            {"issue_number": 1, "body": "Comment"},
            "comment-1",
        ),
        approved,
    )
    assert comment.status_code == 201
    assert all(request.headers["x-github-api-version"] == "2022-11-28" for request in requests)
    assert provider.health(ctx).readiness is Readiness.READY

    rate = make_context(lambda _: httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}))
    with pytest.raises(RateLimitError, match="rate limit"):
        provider.discover(rate)
    denied = make_context(lambda _: httpx.Response(403))
    with pytest.raises(NeedsConsentError, match="permission"):
        provider.discover(denied)


def test_github_validation_user_discovery_and_payload_errors() -> None:
    ctx = make_context(lambda _: httpx.Response(200, json=[]))
    missing = GitHubProvider(GitHubConfig(None, "tenant", AuthConfig(AuthMode.NONE)))
    assert missing.discover(ctx)[0].readiness is Readiness.MISCONFIGURED
    assert missing.health(ctx).readiness is Readiness.MISCONFIGURED
    wrong = GitHubProvider(GitHubConfig("https://api.github.test", "other", AuthConfig(AuthMode.NONE)))
    assert wrong.validate(ctx).readiness is Readiness.UNAUTHORIZED
    user = GitHubProvider(GitHubConfig("https://api.github.test", "tenant", AuthConfig(AuthMode.NONE)))
    assert user.discover(ctx) == ()
    bad_json = make_context(lambda _: httpx.Response(200, text="bad"))
    with pytest.raises(UpstreamError, match="invalid JSON"):
        user.discover(bad_json)
    object_json = make_context(lambda _: httpx.Response(200, json={}))
    with pytest.raises(UpstreamError, match="non-array"):
        user.discover(object_json)


def test_graph_sites_drives_items_ga_operations_and_work_iq_preview() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/sites"):
            return httpx.Response(200, json={"value": [{"id": "site,1", "displayName": "Research"}, {}]})
        if path.endswith("/drives"):
            return httpx.Response(200, json={"value": [{"id": "drive-1", "name": "Documents"}, {}]})
        if path.endswith("/root/children"):
            return httpx.Response(200, json={"value": [{"id": "item-1", "name": "Paper"}, {}]})
        if request.method == "PUT":
            return httpx.Response(201, json={"id": "new-item"})
        return httpx.Response(200, json={"id": "resource"})

    provider = MicrosoftGraphProvider(GraphConfig("https://graph.microsoft.test/v1.0", "tenant"))
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    preview = capabilities[0]
    assert preview.capability_id == "graph.work_iq.preview"
    assert preview.attachable is False
    assert preview.operations[0].maturity is Maturity.PREVIEW
    site = next(item for item in capabilities if item.resource_kind == "sharepoint_site")
    assert provider.invoke(InvocationRequest(site.capability_id, "graph.site.get", {}), ctx).output["id"] == "resource"
    item = next(item for item in capabilities if item.resource_kind == "drive_item")
    assert provider.invoke(InvocationRequest(item.capability_id, "graph.item.get", {}), ctx).status_code == 200
    drive = next(
        item
        for item in capabilities
        if item.resource_kind == "drive" and item.operations[0].operation_id == "graph.drive.children.list"
    )
    assert (
        provider.invoke(InvocationRequest(drive.capability_id, "graph.drive.children.list", {}), ctx).status_code == 200
    )
    write = next(
        item
        for item in capabilities
        if item.resource_kind == "drive" and item.operations[0].operation_id == "graph.drive.content.put"
    )
    approved = replace(ctx, approved_capability_ids=frozenset({write.capability_id}))
    result = provider.invoke(
        InvocationRequest(
            write.capability_id,
            "graph.drive.content.put",
            {"path": "folder/paper.txt", "content_base64": base64.b64encode(b"paper").decode()},
        ),
        approved,
    )
    assert result.status_code == 201
    assert all(request.url.host == "graph.microsoft.test" for request in requests)
    assert provider.health(ctx).readiness is Readiness.READY


def test_graph_validation_no_item_discovery_and_write_validation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sites"):
            return httpx.Response(200, json={"value": [{"id": "site"}]})
        return httpx.Response(200, json={"value": [{"id": "drive"}]})

    ctx = make_context(handler)
    missing = MicrosoftGraphProvider(GraphConfig(None, None))
    unavailable = missing.discover(ctx)
    assert unavailable[1].readiness is Readiness.MISCONFIGURED
    assert missing.health(ctx).readiness is Readiness.MISCONFIGURED
    no_tenant = MicrosoftGraphProvider(GraphConfig("https://graph.test/v1.0", None))
    assert no_tenant.validate(ctx).readiness is Readiness.MISCONFIGURED
    wrong = MicrosoftGraphProvider(GraphConfig("https://graph.test/v1.0", "other"))
    assert wrong.validate(ctx).readiness is Readiness.UNAUTHORIZED
    provider = MicrosoftGraphProvider(GraphConfig("https://graph.test/v1.0", "tenant", discover_items=False))
    capabilities = provider.discover(ctx)
    assert not any(item.resource_kind == "drive_item" for item in capabilities)
    write = next(item for item in capabilities if item.operations[0].operation_id == "graph.drive.content.put")
    approved = replace(ctx, approved_capability_ids=frozenset({write.capability_id}))
    for arguments, message in (
        ({"path": "../bad", "content_base64": "YQ=="}, "safe relative"),
        ({"path": "good", "content_base64": "***"}, "invalid"),
    ):
        with pytest.raises(ProviderValidationError, match=message):
            provider.invoke(
                InvocationRequest(write.capability_id, "graph.drive.content.put", arguments),
                approved,
            )
