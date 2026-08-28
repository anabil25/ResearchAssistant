from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "apps" / "web"
GRANTS_GOV_LOOKUP_URL = "https://api.grants.gov/v1/api/fetchOpportunity"
DEFAULT_GRANT_ID = "357744"
BROWSER_NPM_CI_TIMEOUT_SECONDS = 900
BROWSER_INSTALL_TIMEOUT_SECONDS = 900
BROWSER_TEST_TIMEOUT_SECONDS = 720
DEFAULT_HTTP_TIMEOUT_SECONDS = 60
DEFAULT_AGENT_TIMEOUT_SECONDS = 480
MAX_SSE_BYTES = 2_000_000
ACR_PULL_ROLE_ID = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
EXPECTED_AGENTS = (
    "research-coordinator",
    "literature-agent",
    "grant-agent",
    "matching-agent",
    "dataset-agent",
    "institution-agent",
    "screening-agent",
)


class ReleaseVerificationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GrantOracle:
    grants_gov_id: str
    opportunity_number: str
    title: str
    agency: str
    status: str
    posted_date: str | None
    close_date: str | None
    archive_date: str | None
    canonical_url: str


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    event: str
    data: dict[str, Any]


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _enum_text(value: object) -> str:
    nested = getattr(value, "value", value)
    return str(nested or "").casefold()


def validate_deployment_environment(values: Mapping[str, str]) -> None:
    account_name = values.get("AZURE_AI_ACCOUNT_NAME")
    project_name = values.get("AZURE_AI_PROJECT_NAME")
    account_override = values.get("FOUNDRY_ACCOUNT_NAME")
    project_override = values.get("FOUNDRY_PROJECT_NAME")
    endpoint = values.get("FOUNDRY_PROJECT_ENDPOINT")
    required = {
        "AZURE_AI_ACCOUNT_NAME": account_name,
        "AZURE_AI_PROJECT_NAME": project_name,
        "FOUNDRY_ACCOUNT_NAME": account_override,
        "FOUNDRY_PROJECT_NAME": project_override,
        "FOUNDRY_PROJECT_ENDPOINT": endpoint,
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ReleaseVerificationError(
            "Deployment identity is missing " + ", ".join(missing) + "."
        )
    if account_override != account_name or project_override != project_name:
        raise ReleaseVerificationError(
            "Foundry overrides do not match the deployed account and project outputs."
        )
    incarnation = values.get("AZURE_DEPLOYMENT_INCARNATION")
    if incarnation:
        from scripts.deployment_incarnation import deployment_identity

        expected = deployment_identity(values.get("AZURE_ENV_NAME", ""), incarnation)
        if (
            account_name != expected.foundry_account_name
            or project_name != expected.foundry_project_name
        ):
            raise ReleaseVerificationError(
                "Deployed Foundry names do not match the committed incarnation."
            )
    parsed = urlsplit(str(endpoint))
    if (
        parsed.scheme != "https"
        or parsed.path.rstrip("/").rsplit("/", 1)[-1] != project_name
        or not parsed.hostname
        or not parsed.hostname.casefold().startswith(f"{account_name}.".casefold())
    ):
        raise ReleaseVerificationError(
            "Foundry endpoint does not match the deployed account and project."
        )


def expected_agent_versions(values: Mapping[str, str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for name in EXPECTED_AGENTS:
        key = f"AGENT_{name.replace('-', '_').upper()}_VERSION"
        version = values.get(key)
        if not version:
            raise ReleaseVerificationError(f"Deployment identity is missing {key}.")
        versions[name] = version
    return versions


def validate_agent_inventory(
    versions: Iterable[object],
    *,
    expected_versions: Mapping[str, str],
    source_tree_digest: str,
) -> dict[str, object]:
    observed: dict[str, object] = {}
    for version in versions:
        name = str(_field(version, "name") or "")
        if not name or name in observed:
            raise ReleaseVerificationError("Hosted Agent inventory contains an invalid duplicate.")
        observed[name] = version
    if set(observed) != set(expected_versions):
        raise ReleaseVerificationError(
            "Hosted Agent inventory does not match the expected seven agents "
            f"(missing={sorted(set(expected_versions) - set(observed))}, "
            f"unexpected={sorted(set(observed) - set(expected_versions))})."
        )
    for name, expected_version in expected_versions.items():
        version = observed[name]
        if str(_field(version, "version") or "") != expected_version:
            raise ReleaseVerificationError(
                f"Hosted Agent {name} is not running expected version {expected_version}."
            )
        if _enum_text(_field(version, "status")) != "active":
            raise ReleaseVerificationError(f"Hosted Agent {name} is not active.")
        definition = _field(version, "definition")
        environment = _field(definition, "environment_variables")
        if (
            not isinstance(environment, Mapping)
            or environment.get("AGENT_SOURCE_TREE_DIGEST") != source_tree_digest
        ):
            raise ReleaseVerificationError(
                f"Hosted Agent {name} does not attest the release source-tree digest."
            )
        protocols = _field(definition, "protocol_versions")
        if not isinstance(protocols, list) or not any(
            _enum_text(_field(protocol, "protocol")) == "responses"
            and str(_field(protocol, "version") or "") == "2.0.0"
            for protocol in protocols
        ):
            raise ReleaseVerificationError(
                f"Hosted Agent {name} does not expose Responses protocol 2.0.0."
            )
    return observed


def validate_connection_inventory(
    payload: object,
    *,
    expected: Mapping[str, tuple[str, str]],
) -> None:
    values = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise ReleaseVerificationError("Foundry project connection inventory is invalid.")
    observed: dict[str, dict[str, object]] = {}
    for value in values:
        if not isinstance(value, dict):
            raise ReleaseVerificationError("Foundry project connection inventory is invalid.")
        name = str(value.get("name") or "").rsplit("/", 1)[-1]
        if not name or name in observed:
            raise ReleaseVerificationError(
                "Foundry project connection inventory contains an invalid duplicate."
            )
        observed[name] = value
    if set(observed) != set(expected):
        raise ReleaseVerificationError(
            "Foundry project connection inventory does not match the governed release "
            f"(missing={sorted(set(expected) - set(observed))}, "
            f"unexpected={sorted(set(observed) - set(expected))})."
        )
    for name, (category, target) in expected.items():
        properties = observed[name].get("properties")
        if not isinstance(properties, dict):
            raise ReleaseVerificationError(f"Foundry connection {name} has no properties.")
        if properties.get("category") != category or properties.get("target") != target:
            raise ReleaseVerificationError(
                f"Foundry connection {name} does not match its governed category and target."
            )


def _run_json(command: list[str]) -> object:
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(completed.stdout)


def _release_source_digest(values: Mapping[str, str]) -> str:
    from scripts.build_agent_source_tree import (
        build_source_tree_manifest,
        validate_worktree_matches_commit,
    )

    validate_worktree_matches_commit(ROOT)
    manifest = build_source_tree_manifest(ROOT)
    declared = values.get("AGENT_SOURCE_TREE_DIGEST")
    if declared != manifest.source_tree_digest:
        raise ReleaseVerificationError(
            "The azd source-tree digest does not match the committed agent source."
        )
    return manifest.source_tree_digest


def _require_role(principal_id: str, scope: str, role_id: str) -> None:
    executable = "az.cmd" if os.name == "nt" else "az"
    assignments = _run_json(
        [
            executable,
            "role",
            "assignment",
            "list",
            "--assignee-object-id",
            principal_id,
            "--scope",
            scope,
            "--query",
            "[].roleDefinitionId",
            "--output",
            "json",
        ]
    )
    if not isinstance(assignments, list) or not any(
        str(assignment).casefold().endswith(role_id.casefold())
        for assignment in assignments
    ):
        raise ReleaseVerificationError(
            f"Required role {role_id} is missing for principal {principal_id}."
        )


def _expected_connections(values: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    from scripts.postprovision import connector_mcp_targets
    from scripts.provider_onboarding import connector_project_connection_ids

    project_id = values.get("AZURE_AI_PROJECT_ID")
    serialized_targets = values.get("AZURE_CONNECTOR_MCP_URLS")
    acr_name = values.get("AZURE_AI_PROJECT_ACR_CONNECTION_NAME")
    acr_target = values.get("AZURE_CONTAINER_REGISTRY_ENDPOINT")
    if not all((project_id, serialized_targets, acr_name, acr_target)):
        raise ReleaseVerificationError(
            "Deployment outputs are incomplete for the project connection inventory."
        )
    ids = connector_project_connection_ids(str(project_id))
    targets = connector_mcp_targets(str(serialized_targets))
    expected = {
        ids[connector_id]: ("RemoteTool", target)
        for connector_id, target in targets.items()
    }
    expected[str(acr_name)] = ("ContainerRegistry", str(acr_target))
    return expected


def verify_platform_release() -> dict[str, str]:
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential

    from scripts.azd_env import sync_canonical_azd_outputs
    from scripts.configure_agent_rbac import (
        agent_instance_principal_id,
        wait_for_role_assignment,
    )
    from scripts.postprovision import (
        FOUNDRY_CONNECTION_API_VERSION,
        _resource_manager_endpoint,
        _shared_toolbox_tool_names,
        expected_shared_tool_names,
    )
    from scripts.verify_deployment import verify as verify_container

    values = sync_canonical_azd_outputs()
    validate_deployment_environment(values)
    source_digest = _release_source_digest(values)
    expected_versions = expected_agent_versions(values)
    endpoint = values["FOUNDRY_PROJECT_ENDPOINT"]
    credential = AzureCliCredential()
    project = AIProjectClient(
        endpoint=endpoint,
        credential=credential,
        allow_preview=True,
    )
    try:
        summaries = list(project.agents.list())
        summary_by_name = {
            str(_field(summary, "name") or ""): summary for summary in summaries
        }
        if set(summary_by_name) != set(expected_versions):
            raise ReleaseVerificationError(
                "Hosted Agent catalog does not match the expected seven agents."
            )
        versions: list[object] = []
        for name, expected_version in expected_versions.items():
            summary = summary_by_name[name]
            latest = _field(_field(summary, "versions"), "latest")
            if (
                str(_field(latest, "version") or "") != expected_version
                or _enum_text(_field(latest, "status")) != "active"
            ):
                raise ReleaseVerificationError(
                    f"Hosted Agent {name} latest version is not active version "
                    f"{expected_version}."
                )
            versions.append(project.agents.get_version(name, expected_version))
        inventory = validate_agent_inventory(
            versions,
            expected_versions=expected_versions,
            source_tree_digest=source_digest,
        )
        project_scope = values["AZURE_AI_PROJECT_ID"]
        for name, version in inventory.items():
            principal_id = agent_instance_principal_id(version)
            wait_for_role_assignment(principal_id, project_scope)
            print(
                f"Verified Hosted Agent {name} version {expected_versions[name]} "
                "active with source and RBAC attestation."
            )

        expected_connections = _expected_connections(values)
        resource_manager_endpoint = _resource_manager_endpoint()
        executable = "az.cmd" if os.name == "nt" else "az"
        connection_payload = _run_json(
            [
                executable,
                "rest",
                "--method",
                "get",
                "--url",
                (
                    f"{resource_manager_endpoint}{project_scope}/connections"
                    f"?api-version={FOUNDRY_CONNECTION_API_VERSION}"
                ),
                "--output",
                "json",
            ]
        )
        validate_connection_inventory(
            connection_payload,
            expected=expected_connections,
        )
        if len(expected_connections) != 13:
            raise ReleaseVerificationError(
                f"Expected 13 governed project connections, found {len(expected_connections)}."
            )
        print("Verified exact 13-connection Foundry project inventory.")

        toolbox_names = _shared_toolbox_tool_names(
            credential,
            project_endpoint=endpoint,
            version=None,
        )
        expected_tools = expected_shared_tool_names()
        if toolbox_names != expected_tools:
            raise ReleaseVerificationError(
                "Promoted shared Toolbox inventory does not match the governed tool set."
            )
        print(f"Verified promoted shared Toolbox inventory ({len(toolbox_names)} tools).")

        acr_scope = values["AZURE_CONTAINER_REGISTRY_RESOURCE_ID"]
        _require_role(
            values["AZURE_FOUNDRY_PROJECT_PRINCIPAL_ID"],
            acr_scope,
            ACR_PULL_ROLE_ID,
        )
        _require_role(
            values["AZURE_MANAGED_IDENTITY_PRINCIPAL_ID"],
            acr_scope,
            ACR_PULL_ROLE_ID,
        )
        print("Verified Foundry project and Container Apps AcrPull assignments.")
    finally:
        project.close()
        credential.close()

    verify_container("api")
    verify_container("web")
    return values


def parse_sse_events(lines: Iterable[bytes | str]) -> list[ServerSentEvent]:
    events: list[ServerSentEvent] = []
    event_name = "message"
    data_lines: list[str] = []
    consumed = 0

    def dispatch() -> None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = "message"
            return
        raw_data = "\n".join(data_lines)
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise ReleaseVerificationError("The release stream emitted invalid JSON data.") from exc
        if not isinstance(data, dict):
            raise ReleaseVerificationError("The release stream emitted a non-object event.")
        events.append(ServerSentEvent(event=event_name, data=data))
        event_name = "message"
        data_lines = []

    for raw_line in lines:
        if isinstance(raw_line, bytes):
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReleaseVerificationError("The release stream was not valid UTF-8.") from exc
        else:
            line = raw_line
        consumed += len(line.encode("utf-8"))
        if consumed > MAX_SSE_BYTES:
            raise ReleaseVerificationError("The release stream exceeded its bounded size.")
        line = line.rstrip("\r\n")
        if not line:
            dispatch()
            continue
        if line.startswith(":"):
            continue
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    dispatch()
    return events


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


def _validated_grant_message(
    message: object,
    oracle: GrantOracle,
) -> dict[str, Any]:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ReleaseVerificationError("The release stream did not return an assistant message.")
    content = message.get("content")
    if not isinstance(content, str):
        raise ReleaseVerificationError("The release assistant message has invalid content.")
    normalized_message = json.dumps(
        message,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).casefold()
    private_markers = (
        "authorized_connector_ids",
        "principal_id",
        "project_id",
        "selected_opportunities",
        "sensitivity",
        "session_files",
        "session_id",
        "tenant_id",
        "your reply did not match",
    )
    if any(marker in normalized_message for marker in private_markers):
        raise ReleaseVerificationError("The release assistant exposed internal request data.")
    if content.lstrip().startswith(("{", "[")):
        raise ReleaseVerificationError("The release assistant exposed raw JSON content.")

    opportunities = message.get("opportunities")
    if not isinstance(opportunities, list) or len(opportunities) != 1:
        raise ReleaseVerificationError(
            "The release assistant must return exactly one verified opportunity."
        )
    opportunity = opportunities[0]
    if not isinstance(opportunity, dict):
        raise ReleaseVerificationError("The release opportunity is not an object.")
    expected = {
        "grants_gov_id": oracle.grants_gov_id,
        "opportunity_number": oracle.opportunity_number,
        "title": oracle.title,
        "agency": oracle.agency,
        "status": oracle.status,
        "posted_date": oracle.posted_date,
        "close_date": oracle.close_date,
        "archive_date": oracle.archive_date,
        "canonical_url": oracle.canonical_url,
    }
    for field, value in expected.items():
        if opportunity.get(field) != value:
            raise ReleaseVerificationError(
                f"The release opportunity {field} does not match Grants.gov."
            )
    if not opportunity.get("verified_at"):
        raise ReleaseVerificationError("The release opportunity has no verification timestamp.")
    return message


def validate_grant_completion(
    events: list[ServerSentEvent],
    oracle: GrantOracle,
) -> dict[str, Any]:
    if not events:
        raise ReleaseVerificationError("The release stream emitted no started event.")
    completed: ServerSentEvent | None = None
    for index, event in enumerate(events):
        event_type = event.data.get("type")
        if (
            event.event == "text_delta"
            or event_type == "text_delta"
            or _contains_key(event.data, "delta")
        ):
            raise ReleaseVerificationError(
                "The release stream emitted a forbidden text delta."
            )
        if event.event == "error" or event_type == "error":
            detail = str(event.data.get("detail") or "unknown Hosted Agent error")
            raise ReleaseVerificationError(f"The release stream failed: {detail}")
        if event_type != event.event:
            raise ReleaseVerificationError(
                "The release stream event name and payload type do not match."
            )
        if event.event == "started":
            if index != 0 or set(event.data) != {
                "type",
                "message_id",
                "agent_name",
                "created_at",
            }:
                raise ReleaseVerificationError(
                    "The release stream emitted an invalid started event."
                )
            if not all(
                isinstance(event.data.get(key), str) and event.data[key]
                for key in ("message_id", "agent_name", "created_at")
            ):
                raise ReleaseVerificationError(
                    "The release stream emitted an invalid started event."
                )
            continue
        if event.event == "activity":
            if index == 0 or completed is not None or set(event.data) != {
                "type",
                "activity_id",
                "activity",
            }:
                raise ReleaseVerificationError(
                    "The release stream emitted an invalid activity event."
                )
            activity = event.data.get("activity")
            if (
                not isinstance(event.data.get("activity_id"), str)
                or not event.data["activity_id"]
                or not isinstance(activity, dict)
                or set(activity) != {"kind", "label", "status", "detail"}
                or activity.get("kind") not in {"approach", "tool"}
                or not isinstance(activity.get("label"), str)
                or not activity["label"]
                or activity.get("status")
                not in {"in_progress", "running", "completed"}
                or activity.get("detail") is not None
            ):
                raise ReleaseVerificationError(
                    "The release stream emitted an invalid activity event."
                )
            continue
        if event.event == "completed":
            if (
                index == 0
                or index != len(events) - 1
                or completed is not None
                or set(event.data) != {"type", "message"}
            ):
                raise ReleaseVerificationError(
                    "The release stream must emit one final completed event."
                )
            completed = event
            continue
        raise ReleaseVerificationError(
            f"The release stream emitted unsupported event {event.event!r}."
        )
    if completed is None:
        raise ReleaseVerificationError(
            "The release stream must emit one final completed event."
        )
    return _validated_grant_message(completed.data.get("message"), oracle)


def _request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: object | None = None,
    timeout: int = DEFAULT_HTTP_TIMEOUT_SECONDS,
) -> object:
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "research-assistant-release-gate",
        **(headers or {}),
    }
    body: bytes | None = None
    if method != "GET" or payload is not None:
        body = json.dumps({} if payload is None else payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read(2_000).decode("utf-8", errors="replace")
        raise ReleaseVerificationError(
            f"Release request failed with HTTP {exc.code} for {url}: {detail}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ReleaseVerificationError(f"Release request failed for {url}: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseVerificationError(f"Release request returned invalid JSON for {url}.") from exc


def fetch_grants_gov_oracle(grants_gov_id: str) -> GrantOracle:
    if not grants_gov_id.isdigit() or len(grants_gov_id) > 12:
        raise ReleaseVerificationError("The release Grants.gov ID must contain 1-12 digits.")
    payload = _request_json(
        GRANTS_GOV_LOOKUP_URL,
        method="POST",
        payload={"opportunityId": int(grants_gov_id)},
    )
    if not isinstance(payload, dict) or payload.get("errorcode") != 0:
        raise ReleaseVerificationError("The independent Grants.gov lookup failed.")
    data = payload.get("data")
    if not isinstance(data, dict) or str(data.get("id")) != grants_gov_id:
        raise ReleaseVerificationError("Grants.gov returned a different opportunity ID.")
    errors = data.get("errorMessages")
    if isinstance(errors, list) and errors:
        raise ReleaseVerificationError("Grants.gov returned errors for the release opportunity.")
    agency_details = data.get("agencyDetails")
    synopsis = data.get("synopsis")
    agency = agency_details if isinstance(agency_details, dict) else {}
    synopsis_data = synopsis if isinstance(synopsis, dict) else {}
    values = {
        "opportunity_number": str(data.get("opportunityNumber") or "").strip(),
        "title": str(data.get("opportunityTitle") or "").strip(),
        "agency": str(agency.get("agencyName") or synopsis_data.get("agencyName") or "").strip(),
        "status": str(data.get("ost") or "").strip().casefold(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ReleaseVerificationError(
            f"Grants.gov omitted required release fields: {', '.join(missing)}."
        )
    return GrantOracle(
        grants_gov_id=grants_gov_id,
        opportunity_number=values["opportunity_number"],
        title=values["title"],
        agency=values["agency"],
        status=values["status"],
        posted_date=_grants_gov_date(synopsis_data.get("postingDateStr")),
        close_date=_grants_gov_date(synopsis_data.get("responseDateStr")),
        archive_date=_grants_gov_date(synopsis_data.get("archiveDateStr")),
        canonical_url=f"https://www.grants.gov/search-results-detail/{grants_gov_id}",
    )


def _grants_gov_date(value: object) -> str | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:-|$)", str(value or "").strip())
    return match.group(1) if match else None


def _active_project_id(api_base_url: str) -> str:
    projects = _request_json(f"{api_base_url}/api/projects")
    if not isinstance(projects, list):
        raise ReleaseVerificationError("The deployment returned an invalid project catalog.")
    active: object = next(
        (item for item in projects if isinstance(item, dict) and item.get("is_active")),
        None,
    )
    if active is None:
        active = _request_json(
            f"{api_base_url}/api/projects",
            method="POST",
            payload={
                "name": "Deployment verification workspace",
                "description": "Private workspace for deterministic release verification.",
            },
        )
    if not isinstance(active, dict) or not str(active.get("id") or "").strip():
        raise ReleaseVerificationError("The deployment has no usable active project.")
    return str(active["id"])


def _verify_grants_gov_connector(api_base_url: str, headers: dict[str, str]) -> None:
    connector = _request_json(
        f"{api_base_url}/api/connectors/grants_gov/test",
        method="POST",
        headers=headers,
    )
    if not isinstance(connector, dict):
        raise ReleaseVerificationError("The Grants.gov connector probe returned invalid data.")
    if (
        connector.get("required") is not True
        or connector.get("enabled") is not True
        or "grant" not in (connector.get("assigned_agents") or [])
        or connector.get("test_status") not in {"ready", "ready_with_key"}
    ):
        raise ReleaseVerificationError(
            "The required Grants.gov connector did not pass its persisted readiness probe."
        )


def grant_turn_prompts(grants_gov_id: str) -> tuple[str, str]:
    return (
        (
            f"Look up Grants.gov opportunity ID {grants_gov_id}. Return exactly this "
            "one opportunity only after the exact Grants.gov lookup succeeds; do not "
            "substitute another opportunity."
        ),
        (
            "In this same conversation, re-check the opportunity from my immediately "
            "preceding request against Grants.gov and return exactly that one verified "
            "opportunity again. Use the conversation context; do not substitute a "
            "different opportunity."
        ),
    )


def _request_sse(
    url: str,
    *,
    headers: dict[str, str],
    payload: object,
) -> list[ServerSentEvent]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "research-assistant-release-gate",
            **headers,
        },
    )
    try:
        with urlopen(request, timeout=DEFAULT_AGENT_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type()
            if content_type != "text/event-stream":
                raise ReleaseVerificationError(
                    f"The release endpoint returned {content_type}, not text/event-stream."
                )
            return parse_sse_events(response)
    except HTTPError as exc:
        detail = exc.read(2_000).decode("utf-8", errors="replace")
        raise ReleaseVerificationError(
            f"Release stream failed with HTTP {exc.code}: {detail}"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ReleaseVerificationError(f"Release stream request failed: {exc}") from exc


def verify_api_release(
    api_base_url: str,
    grants_gov_id: str = DEFAULT_GRANT_ID,
) -> GrantOracle:
    api_base_url = api_base_url.rstrip("/")
    oracle = fetch_grants_gov_oracle(grants_gov_id)
    project_id = _active_project_id(api_base_url)
    headers = {"X-Research-Project-ID": project_id}
    _verify_grants_gov_connector(api_base_url, headers)

    opened = _request_json(
        f"{api_base_url}/api/agent-chat/threads",
        method="POST",
        headers=headers,
        payload={"capability": "grant", "agent_name": "grant-agent"},
    )
    if not isinstance(opened, dict) or not str(opened.get("id") or "").strip():
        raise ReleaseVerificationError("The grant agent did not open a release thread.")
    serialized_thread = json.dumps(opened).casefold()
    if "conversation" in serialized_thread or "session" in serialized_thread:
        raise ReleaseVerificationError("The release thread exposed a platform identifier.")
    thread_id = quote(str(opened["id"]), safe="")
    stream_url = (
        f"{api_base_url}/api/agent-chat/threads/{thread_id}/messages/stream"
    )
    completed_messages: list[dict[str, Any]] = []
    for turn_number, prompt in enumerate(grant_turn_prompts(grants_gov_id), start=1):
        events = _request_sse(
            stream_url,
            headers=headers,
            payload={
                "text": prompt,
                "client_message_id": f"release-{turn_number}-{uuid4().hex}",
            },
        )
        completed_messages.append(validate_grant_completion(events, oracle))
        print(
            f"Verified live grant conversation turn {turn_number} for "
            f"{oracle.grants_gov_id}."
        )

    persisted = _request_json(
        f"{api_base_url}/api/agent-chat/threads/{thread_id}",
        headers=headers,
    )
    messages = persisted.get("messages") if isinstance(persisted, dict) else None
    if (
        not isinstance(messages, list)
        or len(messages) != 4
        or [item.get("role") if isinstance(item, dict) else None for item in messages]
        != ["user", "assistant", "user", "assistant"]
    ):
        raise ReleaseVerificationError(
            "The release thread did not persist exactly two complete conversation turns."
        )
    for completed_message in completed_messages:
        assistant_id = completed_message.get("id")
        persisted_assistant = next(
            (
                item
                for item in messages
                if isinstance(item, dict)
                and item.get("role") == "assistant"
                and item.get("id") == assistant_id
            ),
            None,
        )
        _validated_grant_message(persisted_assistant, oracle)
    print(
        "Verified two-turn live grant SSE conversation and persisted opportunity "
        f"{oracle.grants_gov_id} ({oracle.opportunity_number})."
    )
    return oracle


def run_browser_release_gate(web_url: str, grants_gov_id: str) -> None:
    executable = "npm.cmd" if os.name == "nt" else "npm"
    node = "node.exe" if os.name == "nt" else "node"
    playwright_cli = WEB_ROOT / "node_modules" / "playwright" / "cli.js"
    environment = os.environ.copy()
    environment["PLAYWRIGHT_BASE_URL"] = web_url.rstrip("/")
    try:
        subprocess.run(
            [executable, "ci", "--no-audit", "--no-fund"],
            cwd=WEB_ROOT,
            check=True,
            timeout=BROWSER_NPM_CI_TIMEOUT_SECONDS,
        )
        subprocess.run(
            [node, str(playwright_cli), "install", "chromium"],
            cwd=WEB_ROOT,
            check=True,
            timeout=BROWSER_INSTALL_TIMEOUT_SECONDS,
        )
        subprocess.run(
            [executable, "run", "test:release"],
            cwd=WEB_ROOT,
            env=environment,
            check=True,
            timeout=BROWSER_TEST_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseVerificationError(
            "The deployed grant browser bootstrap or gate failed."
        ) from exc


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ReleaseVerificationError(f"Missing azd environment value: {name}")
    return value


def main() -> None:
    values = verify_platform_release()
    web_url = values["SERVICE_WEB_URI"].rstrip("/")
    api_base_url = f"{web_url}/api/backend"
    oracle = verify_api_release(api_base_url, DEFAULT_GRANT_ID)
    run_browser_release_gate(web_url, oracle.grants_gov_id)
    print(f"Verified deployed browser grant table for Grants.gov {oracle.grants_gov_id}.")


if __name__ == "__main__":
    main()