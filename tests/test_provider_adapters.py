from __future__ import annotations

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Lock
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
    NeedsConsentError,
    OpenAPIConfig,
    OpenAPIOperationPolicy,
    OpenAPIProvider,
    OperationClass,
    PolicyError,
    ProviderValidationError,
    RateLimitError,
    Readiness,
    SearchConfig,
    UnavailableError,
    UpstreamError,
    WebhookConfig,
    WebhookProvider,
    approval_decision,
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
    tenant: str = "tenant",
    credential: object | None = None,
    sleeps: list[float] | None = None,
) -> InvocationContext:
    return InvocationContext(
        tenant_id=tenant,
        principal_id="principal",
        project_id="project",
        credential=Credential() if credential is None else credential,
        transport=httpx.Client(transport=httpx.MockTransport(handler)),
        correlation_id="correlation",
        trace_id="trace",
        sleep=(sleeps if sleeps is not None else []).append,
        consume_approval=lambda _: True,
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
    decision = approval_decision(
        context,
        target=target,
        instance=target,
        operation=operation,
        arguments=arguments,
        decision_id=f"approve:{target.instance_id}:{operation_id}",
        expires_at="2999-01-01T00:00:00Z",
    )
    return replace(context, approval_decisions=(decision,))


def test_foundry_dynamic_discovery_invocation_and_preview_policy() -> None:
    calls: list[tuple[str, str]] = []
    response_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        assert request.headers["authorization"].startswith("Bearer ")
        if request.url.path.endswith("/models/model-1"):
            return httpx.Response(200, json={"id": "model-1", "name": "Model One"})
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
    assert provider.descriptor.capability_descriptors[0].operations[0].maturity is Maturity.PREVIEW
    responses = next(item for item in capabilities if item.instance_id == "foundry.responses")
    assert provider.health(responses, ctx).readiness is Readiness.READY
    responses_operation = capabilities.descriptor_for(responses).operations[0]
    assert responses_operation.operation_class is OperationClass.PRIVILEGED
    assert responses_operation.approval_policy is ApprovalPolicy.REQUIRED
    assert responses_operation.side_effect_destinations == ("https://foundry.test/project",)
    response_arguments = {
        "model": "deployment-1",
        "input": "Summarize",
        "conversation": "conversation-1",
    }
    result = provider.invoke(
        InvocationRequest(
            responses,
            "foundry.responses.create",
            response_arguments,
            "request-1",
        ),
        approve(ctx, capabilities, responses, "foundry.responses.create", response_arguments),
    )
    assert result.output["status"] == "completed"
    assert result.audit_metadata["attempts"] == 1
    models = next(item for item in capabilities if item.instance_id == "foundry.models.inventory")
    listed = provider.invoke(InvocationRequest(models, "foundry.models.list", {}), ctx)
    assert listed.output["models"][0]["id"] == "model-1"
    model = next(item for item in capabilities if item.provider_resource_id == "model-1")
    observed = provider.invoke(InvocationRequest(model, "foundry.models.observe", {}), ctx)
    assert observed.output["id"] == "model-1"
    assert ("GET", "/project/models/model-1") in calls
    agent = next(item for item in capabilities if item.provider_resource_id == "agent-1")
    assert agent.readiness is Readiness.UNAVAILABLE
    assert capabilities.descriptor_for(agent).operations[0].maturity is Maturity.UNKNOWN
    knowledge = next(
        item for item in capabilities if capabilities.descriptor_for(item).resource_kind == "project_knowledge"
    )
    first_search = {
        "model": "deployment-1",
        "input": "Find evidence",
        "max_num_results": 3,
    }
    provider.invoke(
        InvocationRequest(
            knowledge,
            "foundry.file_search.query",
            first_search,
            "request-2",
        ),
        approve(ctx, capabilities, knowledge, "foundry.file_search.query", first_search),
    )
    second_search = {"model": "deployment-1", "input": "Find more evidence"}
    provider.invoke(
        InvocationRequest(
            knowledge,
            "foundry.file_search.query",
            second_search,
            "request-2b",
        ),
        approve(ctx, capabilities, knowledge, "foundry.file_search.query", second_search),
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
    assert response_bodies[2]["tools"] == [{"type": "file_search", "vector_store_ids": ["vector-1"]}]
    with pytest.raises(ProviderValidationError, match="declared values"):
        provider.invoke(
            InvocationRequest(
                responses,
                "foundry.responses.create",
                {"model": "unknown", "input": "Unsafe"},
                "request-unknown-model",
            ),
            ctx,
        )
    with pytest.raises(ProviderValidationError, match="unsupported"):
        provider.invoke(
            InvocationRequest(
                responses,
                "foundry.responses.create",
                {
                    "model": "deployment-1",
                    "input": "Unsafe",
                    "tools": [{"type": "memory"}],
                },
                "request-3",
            ),
            ctx,
        )
    assert ("POST", "/project/responses") in calls


def test_foundry_missing_configuration_tenant_auth_and_degraded_discovery() -> None:
    ctx = make_context(lambda _: httpx.Response(503))
    provider = FoundryProvider(FoundryConfig(None, "tenant"))
    assert provider.validate(provider.discover(ctx)[1], ctx).readiness is Readiness.MISCONFIGURED
    capabilities = provider.discover(ctx)
    assert all(item.readiness is not Readiness.READY for item in capabilities)
    assert provider.health(provider.discover(ctx)[1], ctx).readiness is Readiness.MISCONFIGURED
    wrong_tenant = FoundryProvider(FoundryConfig("https://foundry.test", "other", models_path="/models"))
    assert wrong_tenant.validate(wrong_tenant.discover(ctx)[1], ctx).readiness is Readiness.UNAUTHORIZED
    no_credential = replace(ctx, credential=object())
    auth_provider = FoundryProvider(FoundryConfig("https://foundry.test", "tenant", models_path="/models"))
    assert (
        auth_provider.validate(auth_provider.discover(no_credential)[1], no_credential).readiness
        is Readiness.UNAUTHORIZED
    )
    no_paths = FoundryProvider(FoundryConfig("https://foundry.test", "tenant"))
    assert no_paths.validate(no_paths.discover(ctx)[1], ctx).readiness is Readiness.MISCONFIGURED
    partial = FoundryProvider(
        FoundryConfig("https://foundry.test", "tenant", models_path="/models", responses_path="/responses")
    )
    degraded = partial.discover(ctx)
    assert (
        next(item for item in degraded if item.instance_id == "foundry.models.inventory").readiness
        is Readiness.DEGRADED
    )
    assert (
        next(item for item in degraded if item.instance_id == "foundry.agents.inventory").readiness
        is Readiness.MISCONFIGURED
    )
    assert next(item for item in degraded if item.instance_id == "foundry.responses").readiness is Readiness.DEGRADED


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
        InvocationRequest(
            capabilities[0],
            "search.documents.query",
            {"search": "evidence", "top": 2},
        ),
        ctx,
    )
    assert result.output["value"][0]["id"] == "1"
    assert sleeps == [0.0]
    assert provider.health(capabilities[0], ctx).readiness is Readiness.READY


def test_search_missing_configuration_and_empty_discovery() -> None:
    ctx = make_context(lambda _: httpx.Response(200, json={"value": []}))
    missing = AzureAISearchProvider(SearchConfig(None, "tenant"))
    assert missing.discover(ctx)[0].readiness is Readiness.MISCONFIGURED
    assert missing.health(missing.discover(ctx)[0], ctx).readiness is Readiness.MISCONFIGURED
    wrong = AzureAISearchProvider(SearchConfig("https://search.test", "other"))
    assert wrong.validate(wrong.discover(ctx)[0], ctx).readiness is Readiness.UNAUTHORIZED
    no_auth = replace(ctx, credential=object())
    unauthorized = AzureAISearchProvider(SearchConfig("https://search.test", "tenant"))
    assert unauthorized.validate(unauthorized.discover(no_auth)[0], no_auth).readiness is Readiness.UNAUTHORIZED
    empty = AzureAISearchProvider(SearchConfig("https://search.test", "tenant"))
    assert empty.discover(ctx).instances == ()
    assert empty.discover(ctx).instances == ()


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
                OperationClass.READ,
                ApprovalPolicy.NEVER,
                Idempotency.CALLER_KEY,
                Maturity.GA,
            ),
        ),
    )
    provider = AzureFunctionsProvider(config)
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    assert [item.name for item in capabilities] == ["SafeRead"]
    request = InvocationRequest(
        capabilities[0],
        "functions.http.invoke",
        {"query": "x"},
        "key",
    )
    assert provider.invoke(request, ctx).output["ok"] is True
    assert provider.invoke(request, ctx).output == "ok"
    assert provider.health(capabilities[0], ctx).readiness is Readiness.READY


def test_functions_validation_and_default_restrictive_policy() -> None:
    ctx = make_context(lambda _: httpx.Response(200, json={"value": [{"name": "Run"}]}))
    for config in (
        FunctionsConfig(None, "tenant", AuthConfig(AuthMode.NONE)),
        FunctionsConfig(
            None,
            "tenant",
            AuthConfig(AuthMode.NONE),
            function_policies=(FunctionPolicy("Declared"),),
        ),
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
        invalid = AzureFunctionsProvider(config)
        target = invalid.discover(ctx)[0]
        assert invalid.validate(target, ctx).readiness is Readiness.MISCONFIGURED
    wrong = AzureFunctionsProvider(
        FunctionsConfig(
            "https://functions.test",
            "other",
            AuthConfig(AuthMode.NONE),
            "https://functions.test/functions",
        )
    )
    assert wrong.validate(wrong.discover(ctx)[0], ctx).readiness is Readiness.UNAUTHORIZED
    provider = AzureFunctionsProvider(
        FunctionsConfig(
            "https://functions.test",
            "tenant",
            AuthConfig(AuthMode.NONE),
            "https://functions.test/functions",
        )
    )
    capabilities = provider.discover(ctx)
    capability = capabilities[0]
    assert capabilities.descriptor_for(capability).operations[0].operation_class is OperationClass.PRIVILEGED
    with pytest.raises(UnavailableError, match="Only GA"):
        provider.invoke(InvocationRequest(capability, "functions.http.invoke", {}), ctx)


def test_blob_discovery_list_get_put_and_shared_key_headers() -> None:
    requests: list[httpx.Request] = []
    containers_xml = (
        b"<EnumerationResults><Containers><Container><Name>research</Name>"
        b"</Container></Containers></EnumerationResults>"
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

    provider = AzureBlobProvider(BlobConfig("https://account.blob.core.windows.net", "tenant", max_upload_bytes=3))
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    capability = capabilities[0]
    put_operation = next(
        operation
        for operation in capabilities.descriptor_for(capability).operations
        if operation.operation_id == "blob.put"
    )
    assert put_operation.operation_class is OperationClass.WRITE_IRREVERSIBLE
    assert put_operation.approval_policy is ApprovalPolicy.REQUIRED
    assert put_operation.version == "1.1.0"
    assert put_operation.input_schema["properties"]["content_base64"]["maxLength"] == 4
    assert capability.configuration["max_upload_bytes"] == 3
    assert put_operation.side_effect_destinations == ("https://account.blob.core.windows.net/research",)
    listed = provider.invoke(
        InvocationRequest(capability, "blob.blobs.list", {"prefix": "folder/"}),
        ctx,
    )
    assert listed.output["blobs"] == ("folder/a.txt",)
    fetched = provider.invoke(
        InvocationRequest(capability, "blob.get", {"blob": "folder/a.txt"}),
        ctx,
    )
    assert base64.b64decode(fetched.output["content_base64"]) == b"blob-data"
    write_arguments = {
        "blob": "folder/a.txt",
        "content_base64": base64.b64encode(b"new").decode(),
        "content_type": "text/plain",
    }
    written = provider.invoke(
        InvocationRequest(
            capability,
            "blob.put",
            write_arguments,
        ),
        approve(ctx, capabilities, capability, "blob.put", write_arguments),
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
    shared_capabilities = shared.discover(shared_context)
    assert shared_capabilities[0].readiness is Readiness.READY
    shared_capability = shared_capabilities[0]
    shared_arguments = {
        "blob": "signed.txt",
        "content_base64": "YQ==",
        "content_type": "text/plain",
    }
    shared.invoke(
        InvocationRequest(
            shared_capability,
            "blob.put",
            shared_arguments,
        ),
        approve(shared_context, shared_capabilities, shared_capability, "blob.put", shared_arguments),
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
    assert missing.health(missing.discover(ctx)[0], ctx).readiness is Readiness.MISCONFIGURED
    wrong = AzureBlobProvider(BlobConfig("https://blob.test", "other"))
    assert wrong.validate(wrong.discover(ctx)[0], ctx).readiness is Readiness.UNAUTHORIZED
    with pytest.raises(ProviderValidationError, match="invalid XML"):
        AzureBlobProvider(BlobConfig("https://blob.test", "tenant")).discover(ctx)
    with pytest.raises(ProviderValidationError, match="safe relative"):
        AzureBlobProvider._blob_path("container", "../bad")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"<EnumerationResults><Containers><Container><Name>c</Name></Container></Containers></EnumerationResults>",
        )

    provider = AzureBlobProvider(BlobConfig("https://blob.test", "tenant", max_upload_bytes=1))
    valid_ctx = make_context(handler)
    valid_capabilities = provider.discover(valid_ctx)
    capability = valid_capabilities[0]
    invalid_arguments = {"blob": "a", "content_base64": "***"}
    with pytest.raises(ProviderValidationError, match="invalid"):
        provider.invoke(
            InvocationRequest(
                capability,
                "blob.put",
                invalid_arguments,
            ),
            approve(valid_ctx, valid_capabilities, capability, "blob.put", invalid_arguments),
        )
    boundary_arguments = {"blob": "a", "content_base64": base64.b64encode(b"a").decode()}
    assert (
        provider.invoke(
            InvocationRequest(capability, "blob.put", boundary_arguments),
            approve(valid_ctx, valid_capabilities, capability, "blob.put", boundary_arguments),
        ).status_code
        == 200
    )
    for content in (b"ab", b"abcd"):
        oversized = {"blob": "a", "content_base64": base64.b64encode(content).decode()}
        with pytest.raises(ProviderValidationError, match="upload limit"):
            provider.invoke(
                InvocationRequest(capability, "blob.put", oversized),
                approve(valid_ctx, valid_capabilities, capability, "blob.put", oversized),
            )
    with pytest.raises(ValueError, match="positive"):
        BlobConfig("https://blob.test", "tenant", max_upload_bytes=0)


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

    provider = MCPStreamableHTTPProvider(
        MCPConfig(
            "https://mcp.test",
            "tenant",
            tool_policies=(
                MCPToolPolicy(
                    "write_everything",
                    OperationClass.PRIVILEGED,
                    ApprovalPolicy.REQUIRED,
                    Idempotency.NONE,
                    Maturity.GA,
                ),
            ),
        )
    )
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    capability = capabilities[0]
    assert capabilities.descriptor_for(capability).operations[0].operation_class is OperationClass.PRIVILEGED
    assert capability.configuration["untrusted_tool_metadata"]["annotations"]["readOnlyHint"] is True
    assert tuple(capabilities.descriptor_for(capability).operations[0].input_schema["required"]) == ("value",)
    arguments = {"value": "x"}
    result = provider.invoke(
        InvocationRequest(capability, "mcp.tools.call", arguments, "idempotency"),
        approve(ctx, capabilities, capability, "mcp.tools.call", arguments),
    )
    assert result.output["content"][0]["text"] == "done"
    assert methods.count("initialize") == 1
    assert provider.health(capability, ctx).readiness is Readiness.READY
    other_principal = replace(ctx, principal_id="other-principal")
    provider.discover(other_principal)
    assert methods.count("initialize") == 2
    provider.discover(replace(ctx, project_id="other-project"))
    assert methods.count("initialize") == 3


def test_mcp_concurrent_initialization_and_scope_isolation() -> None:
    barrier = Barrier(6)
    state_lock = Lock()
    initialization_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initialization_count
        body = json.loads(request.content)
        if body["method"] == "initialize":
            with state_lock:
                initialization_count += 1
                session_id = f"session-{initialization_count}"
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                headers={"Mcp-Session-Id": session_id},
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
        )

    provider = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "tenant"))
    context = make_context(handler)

    def discover() -> None:
        barrier.wait()
        provider.discover(context)

    with ThreadPoolExecutor(max_workers=6) as executor:
        tuple(executor.map(lambda _: discover(), range(6)))
    assert initialization_count == 1
    assert len(provider._initialized) == 1

    other_principal = replace(context, principal_id="other-principal")
    other_tenant = replace(context, tenant_id="other-tenant")
    provider._initialize_with_session(other_principal)
    provider._initialize_with_session(other_tenant)
    assert initialization_count == 3
    assert provider._sessions[("tenant", "project", "principal")] == "session-1"
    assert provider._sessions[("tenant", "project", "other-principal")] == "session-2"
    assert provider._sessions[("other-tenant", "project", "principal")] == "session-3"


def test_mcp_stale_session_recovery_and_non_idempotent_no_replay() -> None:
    initializations = 0
    tool_calls = 0
    initialize_headers: list[str | None] = []
    stale_calls = Barrier(2)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal initializations, tool_calls
        body = json.loads(request.content)
        method = body["method"]
        if method == "initialize":
            initializations += 1
            initialize_headers.append(request.headers.get("mcp-session-id"))
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                headers={"Mcp-Session-Id": f"session-{initializations}"},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]},
                },
            )
        tool_calls += 1
        if request.headers["mcp-session-id"] == "session-1":
            stale_calls.wait()
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": []}},
        )

    provider = MCPStreamableHTTPProvider(
        MCPConfig(
            "https://mcp.test",
            "tenant",
            tool_policies=(
                MCPToolPolicy(
                    "read",
                    OperationClass.READ,
                    ApprovalPolicy.NEVER,
                    Idempotency.PROVIDER_NATIVE,
                    Maturity.GA,
                ),
            ),
        )
    )
    context = make_context(handler)
    capability = provider.discover(context)[0]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(
                lambda _: provider.invoke(InvocationRequest(capability, "mcp.tools.call", {}), context),
                range(2),
            )
        )
    assert all(result.status_code == 200 for result in results)
    assert initializations == 2
    assert tool_calls == 4
    assert initialize_headers == [None, None]

    unsafe_initializations = 0
    unsafe_calls = 0

    def unsafe_handler(request: httpx.Request) -> httpx.Response:
        nonlocal unsafe_initializations, unsafe_calls
        body = json.loads(request.content)
        if body["method"] == "initialize":
            unsafe_initializations += 1
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                headers={"Mcp-Session-Id": "stale"},
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": [{"name": "write", "inputSchema": {"type": "object"}}]},
                },
            )
        unsafe_calls += 1
        return httpx.Response(404)

    unsafe = MCPStreamableHTTPProvider(
        MCPConfig(
            "https://mcp.test",
            "tenant",
            tool_policies=(
                MCPToolPolicy(
                    "write",
                    OperationClass.PRIVILEGED,
                    ApprovalPolicy.REQUIRED,
                    Idempotency.NONE,
                    Maturity.GA,
                ),
            ),
        )
    )
    unsafe_context = make_context(unsafe_handler)
    unsafe_discovery = unsafe.discover(unsafe_context)
    unsafe_capability = unsafe_discovery[0]
    approved = approve(unsafe_context, unsafe_discovery, unsafe_capability, "mcp.tools.call", {})
    with pytest.raises(UpstreamError, match="not replayed"):
        unsafe.invoke(InvocationRequest(unsafe_capability, "mcp.tools.call", {}), approved)
    assert unsafe_initializations == 1
    assert unsafe_calls == 1
    assert ("tenant", "project", "principal") not in unsafe._sessions

    keyed_sessions = 0
    keyed_headers: list[str | None] = []

    def keyed_handler(request: httpx.Request) -> httpx.Response:
        nonlocal keyed_sessions
        body = json.loads(request.content)
        if body["method"] == "initialize":
            keyed_sessions += 1
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18"}},
                headers={"Mcp-Session-Id": f"keyed-{keyed_sessions}"},
            )
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        if body["method"] == "tools/list":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "result": {"tools": [{"name": "keyed-write", "inputSchema": {"type": "object"}}]},
                },
            )
        keyed_headers.append(request.headers.get("idempotency-key"))
        if request.headers["mcp-session-id"] == "keyed-1":
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": []}},
        )

    keyed = MCPStreamableHTTPProvider(
        MCPConfig(
            "https://mcp.test",
            "tenant",
            tool_policies=(
                MCPToolPolicy(
                    "keyed-write",
                    OperationClass.PRIVILEGED,
                    ApprovalPolicy.REQUIRED,
                    Idempotency.CALLER_KEY,
                    Maturity.GA,
                ),
            ),
        )
    )
    keyed_context = make_context(keyed_handler)
    keyed_discovery = keyed.discover(keyed_context)
    keyed_capability = keyed_discovery[0]
    assert keyed_discovery.descriptor_for(keyed_capability).operations[0].max_retries == 1
    keyed_arguments: dict[str, Any] = {}
    keyed_approval = approve(
        keyed_context,
        keyed_discovery,
        keyed_capability,
        "mcp.tools.call",
        keyed_arguments,
    )
    assert (
        keyed.invoke(
            InvocationRequest(keyed_capability, "mcp.tools.call", keyed_arguments, "dedupe-1"),
            keyed_approval,
        ).status_code
        == 200
    )
    assert keyed_headers == ["dedupe-1", "dedupe-1"]


def test_mcp_validation_sse_and_protocol_failures() -> None:
    ctx = make_context(lambda _: httpx.Response(500))
    missing = MCPStreamableHTTPProvider(MCPConfig(None, "tenant"))
    assert missing.discover(ctx)[0].readiness is Readiness.MISCONFIGURED
    assert missing.health(missing.discover(ctx)[0], ctx).readiness is Readiness.MISCONFIGURED
    wrong = MCPStreamableHTTPProvider(MCPConfig("https://mcp.test", "other"))
    assert wrong.validate(wrong.discover(ctx)[0], ctx).readiness is Readiness.UNAUTHORIZED
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
        operation_policies=(
            OpenAPIOperationPolicy(
                "getPet",
                OperationClass.READ,
                ApprovalPolicy.NEVER,
                maturity=Maturity.GA,
            ),
            OpenAPIOperationPolicy(
                "updatePet",
                OperationClass.WRITE_IRREVERSIBLE,
                ApprovalPolicy.REQUIRED,
                maturity=Maturity.GA,
            ),
        ),
    )
    provider = OpenAPIProvider(config)
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    assert {item.name for item in capabilities} == {"getPet", "updatePet"}
    get_cap = next(item for item in capabilities if item.name == "getPet")
    get_operation = capabilities.descriptor_for(get_cap).operations[0]
    assert get_operation.operation_class is OperationClass.READ
    assert get_operation.approval_policy is ApprovalPolicy.NEVER
    assert get_operation.side_effect_destinations == ()
    result = provider.invoke(
        InvocationRequest(
            get_cap,
            "getPet",
            {"path": {"petId": "a/b"}, "query": {"expand": "owner"}},
        ),
        ctx,
    )
    assert result.output["method"] == "GET"
    assert requests[-1].url.host == "api.test"
    assert b"a%2Fb" in requests[-1].url.raw_path
    update = next(item for item in capabilities if item.name == "updatePet")
    update_operation = capabilities.descriptor_for(update).operations[0]
    assert update_operation.operation_class is OperationClass.WRITE_IRREVERSIBLE
    assert update_operation.approval_policy is ApprovalPolicy.REQUIRED
    assert update_operation.side_effect_destinations == ("https://api.test/v1",)
    with pytest.raises(PolicyError):
        provider.invoke(
            InvocationRequest(
                update,
                "updatePet",
                {"path": {"petId": "1"}, "body": {}},
            ),
            ctx,
        )
    assert provider.health(get_cap, ctx).readiness is Readiness.READY


def test_openapi_document_retrieval_validation_and_reference_failures() -> None:
    document = {"openapi": "3.0.3", "paths": {"/ping": {"get": {"operationId": "ping"}}}}
    refreshed_document = {"openapi": "3.0.3", "paths": {"/pong": {"get": {"operationId": "pong"}}}}
    fetches = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetches
        if request.url.path == "/openapi.json":
            fetches += 1
            return httpx.Response(200, json=document if fetches == 1 else refreshed_document)
        return httpx.Response(204)

    provider = OpenAPIProvider(
        OpenAPIConfig("https://api.test", "tenant", document_url="https://docs.test/openapi.json")
    )
    ctx = make_context(handler)
    assert provider.discover(ctx)[0].name == "ping"
    assert provider.discover(ctx)[0].name == "pong"
    assert fetches == 2
    missing = OpenAPIProvider(OpenAPIConfig(None, "tenant"))
    assert missing.discover(ctx)[0].readiness is Readiness.MISCONFIGURED
    assert missing.health(missing.discover(ctx)[0], ctx).readiness is Readiness.MISCONFIGURED
    wrong = OpenAPIProvider(OpenAPIConfig("https://api.test", "other", document=document))
    assert wrong.validate(wrong.discover(ctx)[0], ctx).readiness is Readiness.UNAUTHORIZED
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
            AuthConfig(AuthMode.SIGNATURE),
            signing_algorithm="hmac-sha256",
        )
    )
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    capability = capabilities[0]
    assert capabilities.descriptor_for(capability).auth_modes == (AuthMode.SIGNATURE,)
    assert provider.health(capability, ctx).readiness is Readiness.READY
    assert requests[-1].headers["x-signature"] == "hmac-sha256:0"
    arguments = {"event": "x"}
    approved = approve(ctx, capabilities, capability, "publish", arguments)
    with pytest.raises(PolicyError, match="idempotency"):
        provider.invoke(InvocationRequest(capability, "publish", {"event": "x"}), approved)
    result = provider.invoke(
        InvocationRequest(capability, "publish", {"event": "x"}, "event-1"),
        approved,
    )
    assert result.output["accepted"] is True
    assert requests[-1].headers["x-signature"].startswith("hmac-sha256:")
    assert requests[-1].url == httpx.URL("https://hooks.test/events")

    oauth = WebhookProvider(
        WebhookConfig(
            "https://hooks.test/oauth",
            "tenant",
            "send",
            AuthConfig(AuthMode.OAUTH, "webhook.scope"),
            signing_algorithm="hmac-sha256",
        )
    )
    oauth_capabilities = oauth.discover(ctx)
    oauth_capability = oauth_capabilities[0]
    assert oauth_capabilities.descriptor_for(oauth_capability).auth_modes == (AuthMode.OAUTH, AuthMode.SIGNATURE)
    assert oauth.health(oauth_capability, ctx).readiness is Readiness.READY
    assert requests[-1].headers["authorization"].startswith("Bearer ")
    assert requests[-1].headers["x-signature"] == "hmac-sha256:0"
    oauth_arguments = {"event": "oauth"}
    oauth.invoke(
        InvocationRequest(oauth_capability, "send", oauth_arguments, "oauth-1"),
        approve(ctx, oauth_capabilities, oauth_capability, "send", oauth_arguments),
    )
    assert requests[-1].headers["authorization"].startswith("Bearer ")
    assert requests[-1].headers["x-signature"].startswith("hmac-sha256:")

    api_key = WebhookProvider(
        WebhookConfig(
            "https://hooks.test/key",
            "tenant",
            "send",
            AuthConfig(AuthMode.API_KEY, secret_name="webhook-key", header_name="x-api-key"),
            signing_algorithm="hmac-sha256",
        )
    )
    api_key_capability = api_key.discover(ctx)[0]
    assert api_key.health(api_key_capability, ctx).readiness is Readiness.READY
    assert requests[-1].headers["x-api-key"] == "secret:webhook-key"
    assert requests[-1].headers["x-signature"] == "hmac-sha256:0"

    no_health = WebhookProvider(WebhookConfig("https://hooks.test", "tenant", "send", health_method=None))
    no_health_target = no_health.discover(ctx)[0]
    assert no_health.health(no_health_target, ctx).readiness is Readiness.READY
    for config in (
        WebhookConfig(None, "tenant", "send"),
        WebhookConfig("https://hooks.test", "tenant", "send", method="GET"),
        WebhookConfig("https://hooks.test", "tenant", "send", AuthConfig(AuthMode.SIGNATURE)),
        WebhookConfig(
            "https://hooks.test",
            "tenant",
            "send",
            AuthConfig(AuthMode.SIGNATURE),
            signing_algorithm="hmac-sha256",
            signature_header="Content-Type",
        ),
        WebhookConfig(
            "https://hooks.test",
            "tenant",
            "send",
            AuthConfig(AuthMode.API_KEY, secret_name="key", header_name="X-Signature"),
            signing_algorithm="hmac-sha256",
        ),
        WebhookConfig(
            "https://hooks.test",
            "tenant",
            "send",
            signing_algorithm="hmac-sha256",
            signature_header="",
        ),
        WebhookConfig(
            "https://hooks.test",
            "tenant",
            "send",
            signing_algorithm="hmac-sha256",
            signature_header="Bad Header",
        ),
        WebhookConfig(
            "https://hooks.test",
            "tenant",
            "send",
            signing_algorithm="hmac-sha256",
            signature_header="Host",
        ),
    ):
        invalid = WebhookProvider(config)
        assert invalid.validate(invalid.discover(ctx)[0], ctx).readiness is Readiness.MISCONFIGURED
    assert (
        WebhookProvider(WebhookConfig("https://hooks.test", "other", "send")).discover(ctx)[0].readiness
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
    assert (
        provider.invoke(
            InvocationRequest(read, "github.repository.get", {}),
            ctx,
        ).status_code
        == 200
    )
    write = next(item for item in capabilities if item.name.endswith("issues-write"))
    assert write.provider_resource_id == "acme/research"
    assert all(
        operation.operation_class is OperationClass.WRITE_IRREVERSIBLE
        and operation.approval_policy is ApprovalPolicy.REQUIRED
        and operation.side_effect_destinations == ("https://api.github.test/repos/acme/research/issues",)
        for operation in capabilities.descriptor_for(write).operations
    )
    create_arguments = {"title": "Issue"}
    created = provider.invoke(
        InvocationRequest(
            write,
            "github.issues.create",
            create_arguments,
            "issue-1",
        ),
        approve(ctx, capabilities, write, "github.issues.create", create_arguments),
    )
    assert created.status_code == 201
    comment_arguments = {"issue_number": 1, "body": "Comment"}
    comment = provider.invoke(
        InvocationRequest(
            write,
            "github.issue_comments.create",
            comment_arguments,
            "comment-1",
        ),
        approve(ctx, capabilities, write, "github.issue_comments.create", comment_arguments),
    )
    assert comment.status_code == 201
    assert all(request.headers["x-github-api-version"] == "2022-11-28" for request in requests)
    assert provider.health(read, ctx).readiness is Readiness.READY

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
    assert missing.health(missing.discover(ctx)[0], ctx).readiness is Readiness.MISCONFIGURED
    wrong = GitHubProvider(GitHubConfig("https://api.github.test", "other", AuthConfig(AuthMode.NONE)))
    assert wrong.validate(wrong.discover(ctx)[0], ctx).readiness is Readiness.UNAUTHORIZED
    user = GitHubProvider(GitHubConfig("https://api.github.test", "tenant", AuthConfig(AuthMode.NONE)))
    assert user.discover(ctx).instances == ()
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

    provider = MicrosoftGraphProvider(GraphConfig("https://graph.microsoft.test/v1.0", "tenant", max_upload_bytes=5))
    ctx = make_context(handler)
    capabilities = provider.discover(ctx)
    preview = capabilities[0]
    assert preview.instance_id == "graph.work_iq.preview"
    assert preview.readiness is Readiness.UNAVAILABLE
    assert capabilities.descriptor_for(preview).operations[0].maturity is Maturity.PREVIEW
    site = next(item for item in capabilities if capabilities.descriptor_for(item).resource_kind == "sharepoint_site")
    assert (
        provider.invoke(
            InvocationRequest(site, "graph.site.get", {}),
            ctx,
        ).output["id"]
        == "resource"
    )
    item = next(item for item in capabilities if capabilities.descriptor_for(item).resource_kind == "drive_item")
    assert (
        provider.invoke(
            InvocationRequest(item, "graph.item.get", {}),
            ctx,
        ).status_code
        == 200
    )
    drive = next(
        item
        for item in capabilities
        if capabilities.descriptor_for(item).resource_kind == "drive"
        and capabilities.descriptor_for(item).operations[0].operation_id == "graph.drive.children.list"
    )
    assert (
        provider.invoke(
            InvocationRequest(drive, "graph.drive.children.list", {}),
            ctx,
        ).status_code
        == 200
    )
    write = next(
        item
        for item in capabilities
        if capabilities.descriptor_for(item).resource_kind == "drive"
        and capabilities.descriptor_for(item).operations[0].operation_id == "graph.drive.content.put"
    )
    graph_write = capabilities.descriptor_for(write).operations[0]
    assert graph_write.operation_class is OperationClass.WRITE_IRREVERSIBLE
    assert graph_write.version == "1.1.0"
    assert graph_write.input_schema["properties"]["content_base64"]["maxLength"] == 8
    assert write.configuration["max_upload_bytes"] == 5
    assert graph_write.side_effect_destinations == ("https://graph.microsoft.test/v1.0/drives/drive-1/root",)
    write_arguments = {
        "path": "folder/paper.txt",
        "content_base64": base64.b64encode(b"paper").decode(),
    }
    result = provider.invoke(
        InvocationRequest(
            write,
            "graph.drive.content.put",
            write_arguments,
        ),
        approve(ctx, capabilities, write, "graph.drive.content.put", write_arguments),
    )
    assert result.status_code == 201
    assert all(request.url.host == "graph.microsoft.test" for request in requests)
    assert provider.health(site, ctx).readiness is Readiness.READY


def test_graph_validation_no_item_discovery_and_write_validation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sites"):
            return httpx.Response(200, json={"value": [{"id": "site"}]})
        return httpx.Response(200, json={"value": [{"id": "drive"}]})

    ctx = make_context(handler)
    missing = MicrosoftGraphProvider(GraphConfig(None, None))
    unavailable = missing.discover(ctx)
    assert unavailable[1].readiness is Readiness.MISCONFIGURED
    assert missing.health(unavailable[1], ctx).readiness is Readiness.MISCONFIGURED
    no_tenant = MicrosoftGraphProvider(GraphConfig("https://graph.test/v1.0", None))
    no_tenant_target = no_tenant.discover(ctx)[1]
    assert no_tenant.validate(no_tenant_target, ctx).readiness is Readiness.MISCONFIGURED
    wrong = MicrosoftGraphProvider(GraphConfig("https://graph.test/v1.0", "other"))
    wrong_target = wrong.discover(ctx)[1]
    assert wrong.validate(wrong_target, ctx).readiness is Readiness.UNAUTHORIZED
    provider = MicrosoftGraphProvider(
        GraphConfig("https://graph.test/v1.0", "tenant", discover_items=False, max_upload_bytes=1)
    )
    capabilities = provider.discover(ctx)
    assert not any(capabilities.descriptor_for(item).resource_kind == "drive_item" for item in capabilities)
    write = next(
        item
        for item in capabilities
        if capabilities.descriptor_for(item).operations[0].operation_id == "graph.drive.content.put"
    )
    for arguments, message in (
        ({"path": "../bad", "content_base64": "YQ=="}, "safe relative"),
        ({"path": "good", "content_base64": "***"}, "invalid"),
        ({"path": "good", "content_base64": base64.b64encode(b"ab").decode()}, "upload limit"),
        ({"path": "good", "content_base64": base64.b64encode(b"abcd").decode()}, "upload limit"),
    ):
        with pytest.raises(ProviderValidationError, match=message):
            provider.invoke(
                InvocationRequest(write, "graph.drive.content.put", arguments),
                approve(ctx, capabilities, write, "graph.drive.content.put", arguments),
            )
    boundary = {"path": "good", "content_base64": base64.b64encode(b"a").decode()}
    assert (
        provider.invoke(
            InvocationRequest(write, "graph.drive.content.put", boundary),
            approve(ctx, capabilities, write, "graph.drive.content.put", boundary),
        ).status_code
        == 200
    )
    with pytest.raises(ValueError, match="250 MB"):
        GraphConfig("https://graph.test/v1.0", "tenant", max_upload_bytes=250_000_001)
    with pytest.raises(ValueError, match="250 MB"):
        GraphConfig("https://graph.test/v1.0", "tenant", max_upload_bytes=0)
