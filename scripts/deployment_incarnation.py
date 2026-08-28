from __future__ import annotations

import argparse
import json
import re
import secrets
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

AZ_CLI = "az.cmd" if __import__("os").name == "nt" else "az"
INCARNATION_PATTERN = re.compile(r"^[a-f0-9]{12}$")
LEGACY_DEPLOYMENT_OUTPUTS = (
    "AZURE_AI_ACCOUNT_NAME",
    "AZURE_AI_PROJECT_NAME",
    "FOUNDRY_PROJECT_ENDPOINT",
)
DELETION_VERIFY_ATTEMPTS = 20
DELETION_VERIFY_DELAY_SECONDS = 15.0
DELETION_STABLE_ABSENCES = 3


@dataclass(frozen=True, slots=True)
class DeploymentIdentity:
    incarnation: str
    foundry_account_name: str
    foundry_project_name: str


@dataclass(frozen=True, slots=True)
class DeletionTarget:
    subscription_id: str
    resource_group: str
    foundry_account_name: str


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized or "research"


def _validated_incarnation(value: str) -> str:
    if not INCARNATION_PATTERN.fullmatch(value):
        raise RuntimeError("AZURE_DEPLOYMENT_INCARNATION must be 12 lowercase hex characters.")
    return value


def deployment_identity(environment_name: str, incarnation: str) -> DeploymentIdentity:
    incarnation = _validated_incarnation(incarnation)
    slug = _slug(environment_name)
    account_base = slug[:41].rstrip("-") or "research"
    project_base = slug[:21].rstrip("-") or "research"
    return DeploymentIdentity(
        incarnation=incarnation,
        foundry_account_name=f"cog-{account_base}-{incarnation}",
        foundry_project_name=f"{project_base}-{incarnation[:8]}",
    )


def _normalized_values(values: Mapping[str, object]) -> dict[str, str]:
    return {
        str(key).upper(): str(value)
        for key, value in values.items()
        if value is not None and str(value)
    }


def _fresh_incarnation(token_factory: Callable[[], str]) -> str:
    return _validated_incarnation(token_factory())


def _write_identity(
    identity: DeploymentIdentity,
    set_value: Callable[[str, str], None],
) -> None:
    set_value("AZURE_DEPLOYMENT_INCARNATION", identity.incarnation)
    set_value("FOUNDRY_ACCOUNT_NAME", identity.foundry_account_name)
    set_value("FOUNDRY_PROJECT_NAME", identity.foundry_project_name)


def _legacy_name(
    normalized: Mapping[str, str],
    *,
    output_key: str,
    override_key: str,
    set_value: Callable[[str, str], None],
) -> str:
    output = normalized.get(output_key)
    override = normalized.get(override_key)
    if not output:
        raise RuntimeError(
            f"Existing deployment is missing authoritative output {output_key}."
        )
    if override and override != output:
        raise RuntimeError(
            f"{override_key} contradicts authoritative deployment output {output_key}."
        )
    if not override:
        set_value(override_key, output)
    return output


def ensure_deployment_identity(
    values: Mapping[str, object],
    *,
    set_value: Callable[[str, str], None],
    token_factory: Callable[[], str] = lambda: secrets.token_hex(6),
) -> DeploymentIdentity | None:
    normalized = _normalized_values(values)
    environment_name = normalized.get("AZURE_ENV_NAME")
    if not environment_name:
        raise RuntimeError("AZURE_ENV_NAME must be set before deployment identity initialization.")
    incarnation = normalized.get("AZURE_DEPLOYMENT_INCARNATION")
    if incarnation is None and any(normalized.get(key) for key in LEGACY_DEPLOYMENT_OUTPUTS):
        _legacy_name(
            normalized,
            output_key="AZURE_AI_ACCOUNT_NAME",
            override_key="FOUNDRY_ACCOUNT_NAME",
            set_value=set_value,
        )
        _legacy_name(
            normalized,
            output_key="AZURE_AI_PROJECT_NAME",
            override_key="FOUNDRY_PROJECT_NAME",
            set_value=set_value,
        )
        return None

    if incarnation is None:
        identity = deployment_identity(
            environment_name,
            _fresh_incarnation(token_factory),
        )
        _write_identity(identity, set_value)
        return identity

    identity = deployment_identity(environment_name, incarnation)
    if normalized.get("FOUNDRY_ACCOUNT_NAME") != identity.foundry_account_name:
        set_value("FOUNDRY_ACCOUNT_NAME", identity.foundry_account_name)
    if normalized.get("FOUNDRY_PROJECT_NAME") != identity.foundry_project_name:
        set_value("FOUNDRY_PROJECT_NAME", identity.foundry_project_name)
    return identity


def rotate_deployment_identity(
    values: Mapping[str, object],
    *,
    set_value: Callable[[str, str], None],
    token_factory: Callable[[], str] = lambda: secrets.token_hex(6),
) -> DeploymentIdentity:
    normalized = _normalized_values(values)
    environment_name = normalized.get("AZURE_ENV_NAME")
    if not environment_name:
        raise RuntimeError("AZURE_ENV_NAME must be set before deployment identity rotation.")
    previous = normalized.get("AZURE_DEPLOYMENT_INCARNATION")
    incarnation = _fresh_incarnation(token_factory)
    if incarnation == previous:
        raise RuntimeError("The deployment incarnation did not rotate.")
    identity = deployment_identity(environment_name, incarnation)
    _write_identity(identity, set_value)
    return identity


def _deletion_target(values: Mapping[str, object]) -> DeletionTarget:
    normalized = _normalized_values(values)
    required = {
        "AZURE_SUBSCRIPTION_ID": normalized.get("AZURE_SUBSCRIPTION_ID"),
        "AZURE_RESOURCE_GROUP": normalized.get("AZURE_RESOURCE_GROUP"),
        "AZURE_AI_ACCOUNT_NAME": normalized.get("FOUNDRY_ACCOUNT_NAME")
        or normalized.get("AZURE_AI_ACCOUNT_NAME"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "Cannot verify down completion without " + ", ".join(missing) + "."
        )
    return DeletionTarget(
        subscription_id=str(required["AZURE_SUBSCRIPTION_ID"]),
        resource_group=str(required["AZURE_RESOURCE_GROUP"]),
        foundry_account_name=str(required["AZURE_AI_ACCOUNT_NAME"]),
    )


def _azure_deletion_state(target: DeletionTarget) -> tuple[bool, bool]:
    group = subprocess.run(
        [
            AZ_CLI,
            "group",
            "exists",
            "--name",
            target.resource_group,
            "--subscription",
            target.subscription_id,
            "--output",
            "tsv",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    deleted = subprocess.run(
        [
            AZ_CLI,
            "cognitiveservices",
            "account",
            "list-deleted",
            "--subscription",
            target.subscription_id,
            "--output",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    deleted_accounts = json.loads(deleted.stdout)
    if not isinstance(deleted_accounts, list):
        raise RuntimeError("Azure returned an invalid deleted Foundry account list.")
    account_deleted = any(
        isinstance(item, dict)
        and str(item.get("name") or "").casefold()
        == target.foundry_account_name.casefold()
        for item in deleted_accounts
    )
    return group.stdout.strip().casefold() == "true", account_deleted


def wait_for_down_completion(
    values: Mapping[str, object],
    *,
    read_state: Callable[[DeletionTarget], tuple[bool, bool]] = _azure_deletion_state,
    attempts: int = DELETION_VERIFY_ATTEMPTS,
    delay_seconds: float = DELETION_VERIFY_DELAY_SECONDS,
    stable_absences: int = DELETION_STABLE_ABSENCES,
    sleep: Callable[[float], None] = time.sleep,
) -> DeletionTarget:
    if stable_absences < 1:
        raise ValueError("stable_absences must be at least one")
    target = _deletion_target(values)
    state = "deletion has not been observed"
    consecutive_absences = 0
    for attempt in range(1, attempts + 1):
        group_exists, account_deleted = read_state(target)
        if not group_exists and not account_deleted:
            consecutive_absences += 1
            if consecutive_absences >= stable_absences:
                return target
        else:
            consecutive_absences = 0
        state = (
            f"resource_group_exists={group_exists}, "
            f"foundry_account_soft_deleted={account_deleted}, "
            f"stable_absences={consecutive_absences}/{stable_absences}"
        )
        if attempt < attempts:
            sleep(delay_seconds)
    raise RuntimeError(
        f"Azure down/purge did not complete for {target.resource_group}: {state}."
    )


def rotate_after_verified_down(
    values: Mapping[str, object],
    *,
    set_value: Callable[[str, str], None],
    token_factory: Callable[[], str] = lambda: secrets.token_hex(6),
    read_state: Callable[[DeletionTarget], tuple[bool, bool]] = _azure_deletion_state,
    attempts: int = DELETION_VERIFY_ATTEMPTS,
    delay_seconds: float = DELETION_VERIFY_DELAY_SECONDS,
    stable_absences: int = DELETION_STABLE_ABSENCES,
    sleep: Callable[[float], None] = time.sleep,
) -> DeploymentIdentity:
    wait_for_down_completion(
        values,
        read_state=read_state,
        attempts=attempts,
        delay_seconds=delay_seconds,
        stable_absences=stable_absences,
        sleep=sleep,
    )
    return rotate_deployment_identity(
        values,
        set_value=set_value,
        token_factory=token_factory,
    )


def _azd_values() -> dict[str, Any]:
    completed = subprocess.run(
        ["azd", "env", "get-values", "--output", "json"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    values = json.loads(completed.stdout)
    if not isinstance(values, dict):
        raise RuntimeError("azd returned an invalid environment value collection.")
    return values


def _set_azd_value(key: str, value: str) -> None:
    subprocess.run(
        ["azd", "env", "set", key, value],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("ensure", "rotate"))
    args = parser.parse_args()
    values = _azd_values()
    if args.action == "ensure":
        identity = ensure_deployment_identity(values, set_value=_set_azd_value)
        if identity is None:
            print("Preserved the existing unsalted deployment identity until a successful down.")
            return
        print(f"Deployment incarnation {identity.incarnation} is ready.")
        return
    identity = rotate_after_verified_down(values, set_value=_set_azd_value)
    print(
        f"Rotated deployment incarnation to {identity.incarnation}; the next up will use "
        f"Foundry account {identity.foundry_account_name} and project "
        f"{identity.foundry_project_name}."
    )


if __name__ == "__main__":
    main()