"""GitHub REST operational provider."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from ._http import auth_headers, json_object, require_endpoint, safe_url, send, stable_resource_id
from .config import GitHubConfig
from .contracts import (
    ApprovalPolicy,
    AuthMode,
    CapabilityDescriptor,
    HealthReport,
    Idempotency,
    InvocationContext,
    InvocationRequest,
    InvocationResult,
    Maturity,
    OperationDescriptor,
    ProviderDescriptor,
    Readiness,
    Risk,
    UnauthorizedError,
    UpstreamError,
    ValidationReport,
    audit_metadata,
    find_operation,
)

PROVIDER_ID = "github_rest"
DOCS = (
    "https://docs.github.com/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app",
    "https://docs.github.com/rest/using-the-rest-api/best-practices-for-using-the-rest-api",
)


def _operation(
    operation_id: str,
    risk: Risk,
    schema: dict[str, Any],
    *,
    permissions: tuple[str, ...],
    idempotency: Idempotency,
) -> OperationDescriptor:
    return OperationDescriptor(
        operation_id,
        Maturity.GA,
        schema,
        {},
        risk,
        ApprovalPolicy.NEVER if risk is Risk.READ else ApprovalPolicy.REQUIRED,
        idempotency=idempotency,
        least_privilege_scopes=permissions,
        least_privilege_roles=permissions,
        docs=DOCS,
    )


READ_OPERATIONS = (
    _operation(
        "github.repository.get",
        Risk.READ,
        {"type": "object", "additionalProperties": False},
        permissions=("Metadata: read",),
        idempotency=Idempotency.INHERENT,
    ),
    _operation(
        "github.issues.list",
        Risk.READ,
        {
            "type": "object",
            "properties": {"state": {"type": "string", "enum": ["open", "closed", "all"]}},
            "additionalProperties": False,
        },
        permissions=("Issues: read",),
        idempotency=Idempotency.INHERENT,
    ),
)
CREATE_ISSUE = _operation(
    "github.issues.create",
    Risk.WRITE,
    {
        "type": "object",
        "required": ["title"],
        "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
        "additionalProperties": False,
    },
    permissions=("Issues: write",),
    idempotency=Idempotency.NONE,
)
CREATE_COMMENT = _operation(
    "github.issue_comments.create",
    Risk.WRITE,
    {
        "type": "object",
        "required": ["issue_number", "body"],
        "properties": {"issue_number": {"type": "integer"}, "body": {"type": "string"}},
        "additionalProperties": False,
    },
    permissions=("Issues: write",),
    idempotency=Idempotency.NONE,
)


def _repo_capability(full_name: str, suffix: str, operations: tuple[OperationDescriptor, ...]) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        PROVIDER_ID,
        stable_resource_id(f"github.repository.{suffix}", full_name),
        "github",
        "repository",
        f"{full_name} {suffix}",
        Readiness.READY,
        True,
        (AuthMode.OAUTH, AuthMode.GITHUB_APP),
        "configured organization/account and application installation",
        f"repository {full_name}",
        operations,
        DOCS,
        ("Repository returned by an authenticated GitHub REST discovery request.",),
        metadata={"full_name": full_name},
    )


class GitHubProvider:
    def __init__(self, config: GitHubConfig) -> None:
        self._config = config
        self._descriptor = ProviderDescriptor(
            PROVIDER_ID,
            "github",
            "GitHub REST",
            "Discovers repositories and exposes selected GA repository and issue operations.",
            (AuthMode.OAUTH, AuthMode.GITHUB_APP),
            DOCS,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def validate(self, context: InvocationContext) -> ValidationReport:
        if not self._config.endpoint:
            return ValidationReport(Readiness.MISCONFIGURED, ("GitHub endpoint is not configured.",))
        if not self._config.tenant_id:
            return ValidationReport(Readiness.MISCONFIGURED, ("GitHub tenant boundary is required.",))
        if self._config.tenant_id and context.tenant_id != self._config.tenant_id:
            return ValidationReport(Readiness.UNAUTHORIZED, ("Invocation tenant does not match configuration.",))
        try:
            require_endpoint(self._config.endpoint)
            auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        except ValueError as exc:
            return ValidationReport(Readiness.MISCONFIGURED, (str(exc),))
        except UnauthorizedError as exc:
            return ValidationReport(Readiness.UNAUTHORIZED, (str(exc),))
        return ValidationReport(Readiness.READY)

    def _headers(self, context: InvocationContext) -> dict[str, str]:
        headers = auth_headers(self._config.auth, context, provider_id=PROVIDER_ID)
        headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self._config.api_version,
            }
        )
        return headers

    def discover(self, context: InvocationContext) -> tuple[CapabilityDescriptor, ...]:
        validation = self.validate(context)
        if validation.readiness is not Readiness.READY:
            operation = READ_OPERATIONS[0]
            return (
                CapabilityDescriptor(
                    PROVIDER_ID,
                    "github.configuration",
                    "github",
                    "account",
                    "GitHub configuration",
                    validation.readiness,
                    False,
                    (self._config.auth.mode,),
                    "configured account",
                    "configured GitHub endpoint",
                    (operation,),
                    DOCS,
                    ("No repository discovery request was sent.",),
                    unavailable_reason="; ".join(validation.reasons),
                ),
            )
        path = f"/orgs/{quote(self._config.owner, safe='')}/repos" if self._config.owner else "/user/repos"
        response, _ = send(
            context,
            provider_id=PROVIDER_ID,
            method="GET",
            url=safe_url(require_endpoint(self._config.endpoint), path),
            headers=self._headers(context),
            params={"per_page": 100, "type": "all"},
            idempotent=True,
            consent_on_forbidden=True,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise UpstreamError("GitHub returned invalid JSON", provider_id=PROVIDER_ID) from exc
        if not isinstance(payload, list):
            raise UpstreamError("GitHub repository discovery returned a non-array payload", provider_id=PROVIDER_ID)
        capabilities: list[CapabilityDescriptor] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            full_name = item.get("full_name")
            if not isinstance(full_name, str) or full_name.count("/") != 1:
                continue
            capabilities.extend(
                (
                    _repo_capability(full_name, "read", READ_OPERATIONS),
                    _repo_capability(full_name, "issues-write", (CREATE_ISSUE, CREATE_COMMENT)),
                )
            )
        return tuple(capabilities)

    def health(self, context: InvocationContext) -> HealthReport:
        capabilities = self.discover(context)
        ready = all(item.readiness is Readiness.READY for item in capabilities)
        return HealthReport(
            Readiness.READY if ready else capabilities[0].readiness,
            (f"{len(capabilities) // 2} repository resource(s) discovered.",),
        )

    def invoke(self, request: InvocationRequest, context: InvocationContext) -> InvocationResult:
        capability, operation = find_operation(
            self.discover(context),
            request,
            context,
            provider_id=PROVIDER_ID,
            tenant_id=self._config.tenant_id,
        )
        full_name = str(capability.metadata["full_name"])
        owner, repo = (quote(part, safe="") for part in full_name.split("/", 1))
        method = "GET"
        params: Mapping[str, Any] | None = None
        body: Mapping[str, Any] | None = None
        if operation.operation_id == "github.repository.get":
            path = f"/repos/{owner}/{repo}"
        elif operation.operation_id == "github.issues.list":
            path = f"/repos/{owner}/{repo}/issues"
            params = request.arguments
        elif operation.operation_id == "github.issues.create":
            method, path, body = "POST", f"/repos/{owner}/{repo}/issues", request.arguments
        else:
            method = "POST"
            path = f"/repos/{owner}/{repo}/issues/{request.arguments['issue_number']}/comments"
            body = {"body": request.arguments["body"]}
        headers = self._headers(context)
        response, attempts = send(
            context,
            provider_id=PROVIDER_ID,
            method=method,
            url=safe_url(require_endpoint(self._config.endpoint), path),
            headers=headers,
            params=params,
            json_body=body,
            timeout=operation.timeout_seconds,
            max_retries=operation.max_retries,
            idempotent=method == "GET",
            consent_on_forbidden=True,
        )
        return InvocationResult(
            PROVIDER_ID,
            capability.capability_id,
            operation.operation_id,
            response.status_code,
            json_object(response, provider_id=PROVIDER_ID),
            audit_metadata(
                context,
                provider_id=PROVIDER_ID,
                capability_id=capability.capability_id,
                operation_id=operation.operation_id,
                attempts=attempts,
                response=response,
            ),
        )
