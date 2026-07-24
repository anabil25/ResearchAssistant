"""Microsoft Foundry project provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any, cast
from urllib.parse import quote

from ._http import (
    auth_headers,
    binding_safe_endpoint,
    collection,
    json_object,
    require_endpoint,
    safe_url,
    send,
    stable_resource_id,
)
from .config import FoundryConfig
from .contracts import (
    ApprovalPolicy,
    AuthMode,
    CapabilityBinding,
    CapabilityInstance,
    CapabilityRecord,
    DiscoveryResult,
    HealthReport,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    Maturity,
    OperationClass,
    OperationDescriptor,
    ProviderDescriptor,
    ProviderError,
    Readiness,
    UnauthorizedError,
    ValidationReport,
    audit_metadata,
    canonical_json_hash,
    capability_instance,
    discovery_result,
    find_operation,
    health_for_target,
    official_provenance,
    operation_allows_retry,
    validation_for_target,
)

PROVIDER_ID = "microsoft_foundry"
DOCS = (
    "https://learn.microsoft.com/azure/foundry/agents/environment-setup",
    "https://learn.microsoft.com/azure/foundry/agents/concepts/tool-catalog",
    "https://learn.microsoft.com/azure/foundry/agents/how-to/tools/file-search",
    "https://learn.microsoft.com/azure/foundry/how-to/connections-add",
)
PROVENANCE = official_provenance(
    DOCS,
    source_version="Foundry project REST 2025-05-01",
    last_verified_at="2026-07-23T08:37:02Z",
)
OBJECT_SCHEMA: dict[str, Any] = {"type": "object"}
RESPONSES_INPUT: dict[str, Any] = {
    "type": "object",
    "required": ["model", "input"],
    "properties": {
        "model": {"type": "string"},
        "input": {"type": "string"},
        "conversation": {"type": "string"},
    },
    "additionalProperties": False,
}
FILE_SEARCH_INPUT: dict[str, Any] = {
    "type": "object",
    "required": ["model", "input"],
    "properties": {
        "model": {"type": "string"},
        "input": {"type": "string"},
        "conversation": {"type": "string"},
        "max_num_results": {"type": "integer"},
    },
    "additionalProperties": False,
}


def _operation(
    operation_id: str,
    *,
    maturity: Maturity = Maturity.GA,
    operation_class: OperationClass = OperationClass.READ,
    approval_policy: ApprovalPolicy = ApprovalPolicy.NEVER,
    side_effect_destinations: tuple[str, ...] = (),
    input_schema: dict[str, Any] = OBJECT_SCHEMA,
) -> OperationDescriptor:
    return OperationDescriptor(
        operation_id=operation_id,
        operation_version="1.0.0",
        maturity=maturity,
        input_schema=input_schema,
        output_schema=OBJECT_SCHEMA,
        operation_class=operation_class,
        approval_policy=approval_policy,
        external_side_effect=bool(side_effect_destinations),
        side_effect_destinations=side_effect_destinations,
        idempotency=(
            Idempotency.CALLER_KEY if operation_class is OperationClass.PRIVILEGED else Idempotency.PROVIDER_NATIVE
        ),
        least_privilege_scopes=("https://ai.azure.com/.default",),
        least_privilege_roles=(
            ("Foundry Agent Consumer",) if operation_class is OperationClass.PRIVILEGED else ("Reader",)
        ),
        docs=DOCS,
    )


def _capability(
    capability_id: str,
    name: str,
    resource_kind: str,
    operation: OperationDescriptor,
    readiness: Readiness,
    *,
    evidence: tuple[str, ...],
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    auth_modes: tuple[AuthMode, ...] = (AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
    data_boundary: str = "configured Foundry project endpoint",
    config: FoundryConfig,
) -> CapabilityRecord:
    safe_endpoint, endpoint_digest = binding_safe_endpoint(
        config.endpoint,
        invalid_label="invalid:foundry-endpoint",
    )
    bound_operation = (
        replace(
            operation,
            side_effect_destinations=(
                f"{safe_endpoint}#url-sha256={endpoint_digest}",
            ),
        )
        if operation.side_effect_destinations
        else operation
    )
    return capability_instance(
        provider_id=PROVIDER_ID,
        instance_id=capability_id,
        family="microsoft_foundry",
        resource_kind=resource_kind,
        name=name,
        readiness=readiness,
        auth_modes=auth_modes,
        tenant_boundary="configured Microsoft Entra tenant",
        data_boundary=data_boundary,
        resource_id=str((metadata or {}).get("resource_id") or capability_id),
        operations=(bound_operation,),
        provenance=PROVENANCE,
        status_evidence=evidence,
        unavailable_reason=reason,
        configuration={
            "provider_endpoint": safe_endpoint,
            "provider_endpoint_digest": endpoint_digest,
            "api_version_digest": canonical_json_hash(config.api_version),
            "models_path_digest": canonical_json_hash(config.models_path),
            "deployments_path_digest": canonical_json_hash(config.deployments_path),
            "agents_path_digest": canonical_json_hash(config.agents_path),
            "connections_path_digest": canonical_json_hash(config.connections_path),
            "vector_stores_path_digest": canonical_json_hash(config.vector_stores_path),
            "responses_path_digest": canonical_json_hash(config.responses_path),
            **(metadata or {}),
        },
        selected_auth_mode=config.auth.mode,
        connection_id=config.auth.connection_ref,
        connection_scopes=config.auth.connection_scopes,
        connection_version=config.auth.connection_version,
        connection_identity_mode=config.auth.effective_identity_mode,
        connection_roles=config.auth.authorized_roles,
    )


class FoundryProvider:
    def __init__(self, config: FoundryConfig) -> None:
        self._config = config
        self._memory = _capability(
            "foundry.memory.preview",
            "Foundry Memory",
            "memory",
            _operation(
                "foundry.memory.use",
                maturity=Maturity.PREVIEW,
                operation_class=OperationClass.WRITE_IRREVERSIBLE,
                approval_policy=ApprovalPolicy.REQUIRED,
                side_effect_destinations=(config.endpoint or "unconfigured:foundry-project",),
            ),
            Readiness.UNAVAILABLE,
            evidence=("Service status: preview; provider policy blocks attachment.",),
            reason="Foundry Memory is preview and is not attachable",
            config=config,
        )
        self._descriptor = ProviderDescriptor(
            provider_id=PROVIDER_ID,
            family="microsoft_foundry",
            name="Microsoft Foundry project",
            description="Project-scoped Foundry deployments, agents, connections, and Responses operations.",
            auth_modes=(AuthMode.OAUTH, AuthMode.MANAGED_IDENTITY),
            provenance=PROVENANCE,
            capability_descriptors=(self._memory.descriptor,),
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def _validate_configuration(self, context: InvocationContext) -> ValidationReport:
        if not self._config.endpoint:
            return ValidationReport(Readiness.MISCONFIGURED, ("Foundry endpoint is not configured.",))
        if not self._config.tenant_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("Foundry tenant boundary is required.",))
        if self._config.tenant_id and context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            require_endpoint(self._config.endpoint)
            auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        if not any(
            (
                self._config.models_path,
                self._config.deployments_path,
                self._config.agents_path,
                self._config.connections_path,
                self._config.vector_stores_path,
            )
        ):
            return ValidationReport(Readiness.MISCONFIGURED, ("No Foundry discovery path is configured.",))
        return ValidationReport(Readiness.READY)

    def _unavailable(self, readiness: Readiness, reason: str) -> tuple[CapabilityRecord, ...]:
        capabilities = [self._memory]
        paths = {
            "models": self._config.models_path,
            "deployments": self._config.deployments_path,
            "agents": self._config.agents_path,
            "connections": self._config.connections_path,
            "vector_stores": self._config.vector_stores_path,
        }
        for kind, _path in paths.items():
            capabilities.append(
                _capability(
                    f"foundry.{kind}.inventory",
                    f"Foundry {kind}",
                    kind,
                    _operation(f"foundry.{kind}.list"),
                    readiness,
                    evidence=("A project discovery path is configured.",),
                    reason=reason,
                    config=self._config,
                )
            )
        if self._config.responses_path:
            capabilities.append(
                _capability(
                    "foundry.responses",
                    "Foundry Responses",
                    "conversation",
                    _operation(
                        "foundry.responses.create",
                        operation_class=OperationClass.PRIVILEGED,
                        approval_policy=ApprovalPolicy.REQUIRED,
                        side_effect_destinations=(self._config.endpoint or "unconfigured:foundry-project",),
                    ),
                    readiness,
                    evidence=("No successful project discovery is available.",),
                    reason=reason,
                    config=self._config,
                )
            )
        return tuple(capabilities)

    def _discover_instances(self, context: InvocationContext) -> tuple[CapabilityRecord, ...]:
        validation = self._validate_configuration(context)
        if validation.readiness is not Readiness.READY:
            return self._unavailable(validation.readiness, "; ".join(validation.reasons))
        endpoint = require_endpoint(self._config.endpoint)
        headers = auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        paths = {
            "models": self._config.models_path,
            "deployments": self._config.deployments_path,
            "agents": self._config.agents_path,
            "connections": self._config.connections_path,
            "vector_stores": self._config.vector_stores_path,
        }
        capabilities: list[CapabilityRecord] = [self._memory]
        successful = 0
        deployment_ids: set[str] = set()
        for kind, path in paths.items():
            if path is None:
                capabilities.append(
                    _capability(
                        f"foundry.{kind}.inventory",
                        f"Foundry {kind}",
                        kind,
                        _operation(f"foundry.{kind}.list"),
                        Readiness.MISCONFIGURED,
                        evidence=("No discovery request was sent.",),
                        reason=f"{kind} discovery path is not configured",
                        config=self._config,
                    )
                )
                continue
            try:
                response, _ = send(
                    context,
                    provider_id=PROVIDER_ID,
                    method="GET",
                    url=safe_url(endpoint, path),
                    headers=headers,
                    params={"api-version": self._config.api_version},
                    idempotent=True,
                )
                items = collection(json_object(response, provider_id=PROVIDER_ID))
            except ProviderError as exc:
                readiness = Readiness.UNAUTHORIZED if isinstance(exc, UnauthorizedError) else Readiness.DEGRADED
                capabilities.append(
                    _capability(
                        f"foundry.{kind}.inventory",
                        f"Foundry {kind}",
                        kind,
                        _operation(f"foundry.{kind}.list"),
                        readiness,
                        evidence=(f"Discovery failed with typed error {exc.code}.",),
                        reason=str(exc),
                        config=self._config,
                    )
                )
                continue
            successful += 1
            if kind == "deployments":
                deployment_ids.update(
                    resource_id for item in items if (resource_id := str(item.get("id") or item.get("name") or ""))
                )
            capabilities.append(
                _capability(
                    f"foundry.{kind}.inventory",
                    f"Foundry {kind}",
                    kind,
                    _operation(f"foundry.{kind}.list"),
                    Readiness.READY,
                    evidence=(f"Project discovery succeeded; {len(items)} resource(s) observed.",),
                    metadata={"resource_ids": tuple(str(item.get("id") or item.get("name")) for item in items)},
                    config=self._config,
                )
            )
            for item in items:
                resource_id = str(item.get("id") or item.get("name") or "")
                if resource_id:
                    operation = _operation(f"foundry.{kind}.observe")
                    resource_kind = kind.rstrip("s")
                    data_boundary = "configured Foundry project endpoint"
                    resource_readiness = Readiness.READY
                    resource_reason = None
                    if kind == "agents":
                        operation = _operation(
                            "foundry.agents.observe",
                            maturity=Maturity.UNKNOWN,
                            operation_class=OperationClass.PRIVILEGED,
                            approval_policy=ApprovalPolicy.REQUIRED,
                            side_effect_destinations=(endpoint,),
                        )
                        resource_readiness = Readiness.UNAVAILABLE
                        resource_reason = "Discovered agent release has not been attested by trusted provider policy"
                    elif kind == "vector_stores" and self._config.responses_path:
                        model_schema = dict(FILE_SEARCH_INPUT)
                        model_schema["properties"] = dict(FILE_SEARCH_INPUT["properties"])
                        if deployment_ids:
                            model_schema["properties"]["model"] = {
                                "type": "string",
                                "enum": sorted(deployment_ids),
                            }
                        else:
                            resource_readiness = Readiness.DEGRADED
                            resource_reason = "No model deployment was discovered for File Search"
                        operation = _operation(
                            "foundry.file_search.query",
                            operation_class=OperationClass.PRIVILEGED,
                            approval_policy=ApprovalPolicy.REQUIRED,
                            side_effect_destinations=(endpoint,),
                            input_schema=model_schema,
                        )
                        resource_kind = "project_knowledge"
                        data_boundary = "discovered project vector store; app-owned conversation state"
                    capabilities.append(
                        _capability(
                            stable_resource_id(f"foundry.{kind}", resource_id),
                            str(item.get("name") or resource_id),
                            resource_kind,
                            operation,
                            resource_readiness,
                            evidence=(f"Resource observed through configured {kind} collection.",),
                            reason=resource_reason,
                            metadata={"resource_id": resource_id, "source": "untrusted_remote_metadata"},
                            data_boundary=data_boundary,
                            config=self._config,
                        )
                    )
        if self._config.responses_path:
            ready = Readiness.READY if successful and deployment_ids else Readiness.DEGRADED
            responses_schema = dict(RESPONSES_INPUT)
            responses_schema["properties"] = dict(RESPONSES_INPUT["properties"])
            if deployment_ids:
                responses_schema["properties"]["model"] = {
                    "type": "string",
                    "enum": sorted(deployment_ids),
                }
            reason = None
            if not successful:
                reason = "No configured project discovery endpoint succeeded"
            elif not deployment_ids:
                reason = "No model deployment was discovered"
            capabilities.append(
                _capability(
                    "foundry.responses",
                    "Foundry Responses",
                    "conversation",
                    _operation(
                        "foundry.responses.create",
                        operation_class=OperationClass.PRIVILEGED,
                        approval_policy=ApprovalPolicy.REQUIRED,
                        input_schema=responses_schema,
                        side_effect_destinations=(endpoint,),
                    ),
                    ready,
                    evidence=(f"{successful} configured project discovery endpoint(s) succeeded.",),
                    reason=reason,
                    data_boundary="configured Foundry project; app-owned conversation/session state",
                    config=self._config,
                )
            )
        return tuple(capabilities)

    def discover(self, context: InvocationContext) -> DiscoveryResult:
        return discovery_result(
            self._discover_instances(context),
            tenant_id=self._config.tenant_id or context.tenant_id,
            project_id=context.project_id,
        )

    def validate(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> ValidationReport:
        return validation_for_target(
            self.discover(context),
            target,
            provider_id=PROVIDER_ID,
            policy_ref=context.policy_ref,
            logical_agent_id=context.logical_agent_id,
        )

    def health(
        self,
        target: CapabilityInstance | CapabilityBinding,
        context: InvocationContext,
    ) -> HealthReport:
        return health_for_target(
            self.discover(context),
            target,
            provider_id=PROVIDER_ID,
            policy_ref=context.policy_ref,
            logical_agent_id=context.logical_agent_id,
        )

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        capabilities = self.discover(context)
        instance, operation = find_operation(
            capabilities,
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        endpoint = require_endpoint(self._config.endpoint)
        headers = auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        if operation.operation_id == "foundry.responses.create":
            path = self._config.responses_path
            method = "POST"
            body = {
                key: request.arguments[key] for key in ("model", "input", "conversation") if key in request.arguments
            }
        elif operation.operation_id == "foundry.file_search.query":
            path = self._config.responses_path
            method = "POST"
            tool: dict[str, Any] = {
                "type": "file_search",
                "vector_store_ids": [str(instance.configuration["resource_id"])],
            }
            if "max_num_results" in request.arguments:
                tool["max_num_results"] = request.arguments["max_num_results"]
            body = {
                key: request.arguments[key] for key in ("model", "input", "conversation") if key in request.arguments
            }
            body["tools"] = [tool]
        else:
            kind = operation.operation_id.split(".")[1]
            path = getattr(self._config, f"{kind}_path")
            if operation.operation_id.endswith(".observe"):
                resource_id = quote(str(instance.configuration["resource_id"]), safe="")
                path = f"{cast(str, path).rstrip('/')}/{resource_id}"
            method = "GET"
            body = None
        path = cast(str, path)
        if request.idempotency_key:
            headers["Idempotency-Key"] = request.idempotency_key
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method=method,
            url=safe_url(endpoint, path),
            headers=headers,
            params=None if method == "POST" else {"api-version": self._config.api_version},
            json_body=body,
            timeout=operation.timeout_seconds,
            max_retries=operation.max_retries,
            idempotent=operation_allows_retry(
                operation,
                idempotency_key=request.idempotency_key,
            ),
        )
        output = json_object(response, provider_id=PROVIDER_ID)
        return InvocationResult(
            PROVIDER_ID,
            instance.instance_id,
            operation.operation_id,
            response.status_code,
            output,
            audit_metadata(
                context,
                provider_id=PROVIDER_ID,
                instance_id=instance.instance_id,
                operation_id=operation.operation_id,
                attempts=attempts,
                response=response,
            ),
        )
