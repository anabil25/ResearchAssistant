from __future__ import annotations

import os
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from .errors import ConfigurationError


class HarnessSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    foundry_project_endpoint: HttpUrl
    model_deployment_name: str = Field(min_length=1)
    model_deployment_version: str | None = Field(default=None, min_length=1)
    managed_identity_client_id: str | None = None
    toolbox_endpoint: HttpUrl | None = None
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

    def readiness(self, *, toolbox_required: bool = False) -> dict[str, str | bool]:
        return {
            "ready": not toolbox_required or self.toolbox_endpoint is not None,
            "environment": self.environment,
            "managed_identity": bool(self.managed_identity_client_id),
            "toolbox_configured": self.toolbox_endpoint is not None,
        }
