from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import grpc  # type: ignore[import-untyped]
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from durabletask.azuremanaged.client import DurableTaskSchedulerClient

from research_assistant_api.config import Settings


class RunSchedulingError(RuntimeError):
    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


class RunScheduler(Protocol):
    configured: bool

    def schedule(
        self,
        *,
        instance_id: str,
        payload: dict[str, Any],
    ) -> str: ...

    def approve(
        self,
        *,
        instance_id: str,
        approval_id: str,
        idempotency_key: str,
        approved: bool,
    ) -> None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class InMemoryRunScheduler:
    configured: bool = False

    def schedule(
        self,
        *,
        instance_id: str,
        payload: dict[str, Any],
    ) -> str:
        return instance_id

    def approve(
        self,
        *,
        instance_id: str,
        approval_id: str,
        idempotency_key: str,
        approved: bool,
    ) -> None:
        return None

    def close(self) -> None:
        return None


class DurableRunScheduler:
    configured = True

    def __init__(
        self,
        *,
        host_address: str,
        task_hub: str,
        credential: TokenCredential,
        secure_channel: bool,
    ) -> None:
        self._client = DurableTaskSchedulerClient(
            host_address=host_address,
            taskhub=task_hub,
            token_credential=credential if secure_channel else None,
            secure_channel=secure_channel,
        )

    def schedule(
        self,
        *,
        instance_id: str,
        payload: dict[str, Any],
    ) -> str:
        try:
            existing = self._client.get_orchestration_state(
                instance_id,
                fetch_payloads=False,
            )
        except grpc.RpcError as exc:
            raise RunSchedulingError(
                f"Durable run {instance_id} could not be checked before scheduling.",
                ambiguous=True,
            ) from exc
        if existing is not None:
            return instance_id
        try:
            return self._client.schedule_new_orchestration(
                "research_pipeline",
                input=payload,
                instance_id=instance_id,
                tags={
                    "tenantId": str(payload["tenant_id"]),
                    "projectId": str(payload["project_id"]),
                    "capability": str(payload["capability"]),
                },
            )
        except grpc.RpcError as exc:
            try:
                existing = self._client.get_orchestration_state(
                    instance_id,
                    fetch_payloads=False,
                )
            except grpc.RpcError as reconciliation_exc:
                raise RunSchedulingError(
                    f"Durable run {instance_id} has an ambiguous scheduling result.",
                    ambiguous=True,
                ) from reconciliation_exc
            if existing is not None:
                return instance_id
            raise RunSchedulingError(
                f"Durable run {instance_id} was not scheduled.",
                ambiguous=True,
            ) from exc

    def approve(
        self,
        *,
        instance_id: str,
        approval_id: str,
        idempotency_key: str,
        approved: bool,
    ) -> None:
        try:
            self._client.raise_orchestration_event(
                instance_id=instance_id,
                event_name="review_decision",
                data={
                    "approved": approved,
                    "approval_id": approval_id,
                    "idempotency_key": idempotency_key,
                },
            )
        except grpc.RpcError as exc:
            raise RunSchedulingError(f"Approval event could not be delivered to {instance_id}.") from exc

    def close(self) -> None:
        self._client.close()


def _credential(client_id: str | None) -> TokenCredential:
    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return DefaultAzureCredential()


def build_run_scheduler(settings: Settings) -> RunScheduler:
    raw = settings.durable_task_connection_string
    if not raw:
        return InMemoryRunScheduler()
    fields: dict[str, str] = {}
    for part in raw.split(";"):
        key, separator, value = part.partition("=")
        if separator and key.strip() and value.strip():
            fields[key.strip().lower()] = value.strip()
    endpoint = fields.get("endpoint")
    if not endpoint:
        raise ValueError("Scheduler connection string is missing Endpoint")
    parsed = urlparse(endpoint)
    host_address = parsed.netloc or parsed.path
    if not host_address:
        raise ValueError("Scheduler connection string has an invalid Endpoint")
    client_id = fields.get("clientid") or settings.managed_identity_client_id
    secure_channel = parsed.scheme == "https"
    return DurableRunScheduler(
        host_address=host_address,
        task_hub=fields.get("taskhub", "default"),
        credential=_credential(client_id),
        secure_channel=secure_channel,
    )
