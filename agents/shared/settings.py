from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

from .contracts import DeploymentScope
from .errors import ConfigurationError


class HarnessSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    foundry_project_endpoint: HttpUrl
    model_deployment_name: str = Field(min_length=1)
    model_deployment_version: str | None = Field(default=None, min_length=1)
    managed_identity_client_id: str | None = None
    toolbox_endpoint: HttpUrl | None = None
    deployment_tenant_id: str | None = Field(default=None, min_length=1, max_length=256)
    deployment_project_id: str | None = Field(default=None, min_length=1, max_length=512)
    default_timeout_seconds: float = Field(default=60, gt=0, le=600)
    telemetry_content_recording: bool = False
    environment: str = Field(default="production", min_length=1)

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> HarnessSettings:
        values = os.environ if environ is None else environ
        try:
            return cls.model_validate(
                {
                    "foundry_project_endpoint": values.get("FOUNDRY_PROJECT_ENDPOINT", ""),
                    "model_deployment_name": values.get("AZURE_AI_MODEL_DEPLOYMENT_NAME", ""),
                    "model_deployment_version": values.get("AZURE_AI_MODEL_DEPLOYMENT_VERSION") or None,
                    "managed_identity_client_id": values.get("AZURE_CLIENT_ID") or None,
                    "toolbox_endpoint": values.get("TOOLBOX_ENDPOINT") or None,
                    "deployment_tenant_id": values.get("RESEARCH_WORKSPACE_TENANT_ID")
                    or None,
                    "deployment_project_id": values.get("RESEARCH_WORKSPACE_PROJECT_ID")
                    or None,
                    "default_timeout_seconds": float(values.get("AGENT_DEFAULT_TIMEOUT_SECONDS", "60")),
                    "telemetry_content_recording": values.get(
                        "AZURE_TRACING_GEN_AI_CONTENT_RECORDING_ENABLED",
                        "false",
                    ).lower()
                    == "true",
                    "environment": values.get("AZURE_ENV_NAME", "production"),
                }
            )
        except (ValidationError, ValueError) as exc:
            raise ConfigurationError("Hosted Agent configuration is invalid") from exc

    @model_validator(mode="after")
    def deployment_scope_is_complete(self) -> HarnessSettings:
        if (self.deployment_tenant_id is None) != (self.deployment_project_id is None):
            raise ValueError(
                "Hosted Agent deployment tenant and project scopes must be supplied together"
            )
        return self

    @field_validator("deployment_tenant_id", "deployment_project_id")
    @classmethod
    def reject_template_scope_sentinels(cls, value: str | None) -> str | None:
        if value is not None and value.startswith("provider-discovery://"):
            raise ValueError("deployment scope must be a real application-owned identifier")
        return value

    @property
    def deployment_scope(self) -> DeploymentScope | None:
        if self.deployment_tenant_id is None or self.deployment_project_id is None:
            return None
        return DeploymentScope(
            tenant_id=self.deployment_tenant_id,
            project_id=self.deployment_project_id,
        )

    def readiness(
        self,
        *,
        toolbox_required: bool = False,
        deployment_scope_required: bool = False,
    ) -> dict[str, str | bool]:
        deployment_scope_configured = self.deployment_scope is not None
        return {
            "ready": (
                (not toolbox_required or self.toolbox_endpoint is not None)
                and (
                    not deployment_scope_required
                    or deployment_scope_configured
                )
            ),
            "environment": self.environment,
            "managed_identity": bool(self.managed_identity_client_id),
            "toolbox_configured": self.toolbox_endpoint is not None,
            "deployment_scope_configured": deployment_scope_configured,
        }
