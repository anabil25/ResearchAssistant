from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

AZ_CLI = "az.cmd" if os.name == "nt" else "az"
PLACEHOLDER_IMAGE = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
DEFAULT_TIMEOUT_SECONDS = 600
POLL_SECONDS = 5


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing azd environment value: {name}")
    return value


def run_json(command: list[str]) -> Any:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def _container(properties: dict[str, Any], service: str) -> dict[str, Any]:
    containers = properties.get("template", {}).get("containers", [])
    container = next(
        (item for item in containers if item.get("name") == service),
        None,
    )
    if not isinstance(container, dict):
        raise RuntimeError(f"Container App has no {service!r} container")
    return container


def _probe_contract(service: str) -> set[tuple[str, str, int]]:
    if service == "api":
        return {
            ("Startup", "/health", 8000),
            ("Liveness", "/health", 8000),
            ("Readiness", "/ready", 8000),
        }
    return {
        ("Startup", "/health", 3000),
        ("Liveness", "/health", 3000),
        ("Readiness", "/health", 3000),
    }


def _observed_probes(container: dict[str, Any]) -> set[tuple[str, str, int]]:
    observed: set[tuple[str, str, int]] = set()
    for probe in container.get("probes", []):
        http_get = probe.get("httpGet", {})
        observed.add(
            (
                str(probe.get("type", "")),
                str(http_get.get("path", "")),
                int(http_get.get("port", 0)),
            )
        )
    return observed


def revision_status(
    service: str,
    app: dict[str, Any],
    revision: dict[str, Any],
    expected_image: str,
) -> tuple[bool, str]:
    properties = app.get("properties", {})
    latest = str(properties.get("latestRevisionName") or "")
    ready = str(properties.get("latestReadyRevisionName") or "")
    desired_container = _container(properties, service)
    desired_image = str(desired_container.get("image") or "")
    revision_properties = revision.get("properties", {})
    revision_container = _container(revision_properties, service)
    revision_image = str(revision_container.get("image") or "")

    if desired_image == PLACEHOLDER_IMAGE or revision_image == PLACEHOLDER_IMAGE:
        return False, "placeholder image is still configured"
    if desired_image != expected_image:
        return False, f"desired image mismatch: expected {expected_image}, observed {desired_image}"
    if revision_image != expected_image:
        return False, f"revision image mismatch: expected {expected_image}, observed {revision_image}"
    if properties.get("configuration", {}).get("activeRevisionsMode") != "Single":
        return False, "activeRevisionsMode is not Single"
    if not latest or latest != ready:
        return False, f"latest revision {latest or '<none>'} is not ready ({ready or '<none>'})"
    if str(revision.get("name") or "") != latest:
        return False, "revision response does not match the latest revision"
    if revision_properties.get("healthState") != "Healthy":
        return False, f"revision health is {revision_properties.get('healthState')}"
    if revision_properties.get("runningState") != "Running":
        return False, f"revision state is {revision_properties.get('runningState')}"
    if int(revision_properties.get("replicas") or 0) < 1:
        return False, "revision has no ready replica"
    if _observed_probes(revision_container) != _probe_contract(service):
        return False, "health probe contract does not match the service"
    return True, f"{latest} is healthy on {revision_image}"


def _app_state(service: str) -> tuple[dict[str, Any], dict[str, Any]]:
    subscription = required_env("AZURE_SUBSCRIPTION_ID")
    resource_group = required_env("AZURE_RESOURCE_GROUP")
    app_name = required_env(f"SERVICE_{service.upper()}_NAME")
    common = [
        "--name",
        app_name,
        "--resource-group",
        resource_group,
        "--subscription",
        subscription,
        "--output",
        "json",
    ]
    app = run_json([AZ_CLI, "containerapp", "show", *common])
    latest = str(app.get("properties", {}).get("latestRevisionName") or "")
    if not latest:
        return app, {}
    revision = run_json(
        [AZ_CLI, "containerapp", "revision", "show", *common, "--revision", latest]
    )
    return app, revision


def wait_for_revision(
    service: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    load_state: Callable[[str], tuple[dict[str, Any], dict[str, Any]]] = _app_state,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    expected_image = required_env(f"SERVICE_{service.upper()}_IMAGE_NAME")
    deadline = time.monotonic() + timeout_seconds
    detail = "revision has not been observed"
    while time.monotonic() < deadline:
        try:
            app, revision = load_state(service)
            ready, detail = revision_status(service, app, revision, expected_image)
        except (subprocess.CalledProcessError, json.JSONDecodeError, RuntimeError) as exc:
            ready = False
            detail = f"transient state read failed: {exc}"
        if ready:
            print(f"Verified {service} Container App: {detail}.")
            return str(app.get("properties", {}).get("latestReadyRevisionName"))
        sleep(POLL_SECONDS)
    raise RuntimeError(f"{service} Container App did not become ready: {detail}")


def _http_status(url: str) -> int:
    request = Request(url, headers={"User-Agent": "research-assistant-release-gate"})
    with urlopen(request, timeout=30) as response:
        return int(response.status)


def wait_for_http(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    get_status: Callable[[str], int] = _http_status,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    detail = "no response"
    while time.monotonic() < deadline:
        try:
            status = get_status(url)
            if status == 200:
                print(f"Verified HTTP 200: {url}")
                return
            detail = f"HTTP {status}"
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            detail = str(exc)
        sleep(POLL_SECONDS)
    raise RuntimeError(f"HTTP release check failed for {url}: {detail}")


def verify(service: str) -> None:
    timeout = int(os.getenv("RESEARCH_DEPLOY_VERIFY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    wait_for_revision(service, timeout_seconds=timeout)
    if service == "web":
        web_url = required_env("SERVICE_WEB_URI").rstrip("/")
        for path in ("/health", "/api/backend/health", "/api/backend/ready"):
            wait_for_http(f"{web_url}{path}", timeout_seconds=timeout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=("api", "web"))
    args = parser.parse_args()
    verify(args.service)


if __name__ == "__main__":
    main()