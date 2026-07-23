from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class SchedulerSettings:
    host_address: str
    task_hub: str
    secure_channel: bool
    managed_identity_client_id: str | None


def parse_scheduler_settings() -> SchedulerSettings:
    raw = os.getenv("DURABLE_TASK_SCHEDULER_CONNECTION_STRING", "")
    if not raw:
        return SchedulerSettings(
            host_address="localhost:8080",
            task_hub="default",
            secure_channel=False,
            managed_identity_client_id=None,
        )

    fields = {}
    for part in raw.split(";"):
        key, _, value = part.partition("=")
        if key and value:
            fields[key.strip().lower()] = value.strip()

    endpoint = fields.get("endpoint")
    if not endpoint:
        raise ValueError("Scheduler connection string is missing Endpoint")
    parsed = urlparse(endpoint)
    host_address = parsed.netloc or parsed.path
    return SchedulerSettings(
        host_address=host_address,
        task_hub=fields.get("taskhub", "default"),
        secure_channel=parsed.scheme == "https",
        managed_identity_client_id=(fields.get("clientid") or os.getenv("AZURE_CLIENT_ID")),
    )
