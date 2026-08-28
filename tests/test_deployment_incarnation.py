from __future__ import annotations

from scripts.deployment_incarnation import (
    DeletionTarget,
    DeploymentIdentity,
    deployment_identity,
    ensure_deployment_identity,
    rotate_after_verified_down,
    rotate_deployment_identity,
    wait_for_down_completion,
)


def test_deployment_identity_produces_valid_incarnation_bound_names() -> None:
    identity = deployment_identity(
        "Research_Assistant.Environment.With.A.Very.Long.Name",
        "a1b2c3d4e5f6",
    )

    assert identity == DeploymentIdentity(
        incarnation="a1b2c3d4e5f6",
        foundry_account_name="cog-research-assistant-environment-with-a-ver-a1b2c3d4e5f6",
        foundry_project_name="research-assistant-en-a1b2c3d4",
    )
    assert len(identity.foundry_account_name) <= 64
    assert len(identity.foundry_project_name) <= 32


def test_existing_legacy_deployment_keeps_unsalted_identity_until_down() -> None:
    values = {
        "AZURE_ENV_NAME": "research",
        "AZURE_AI_ACCOUNT_NAME": "cog-existing",
        "AZURE_AI_PROJECT_NAME": "research-v2",
        "FOUNDRY_ACCOUNT_NAME": "cog-existing",
        "FOUNDRY_PROJECT_NAME": "research-v2",
    }
    writes: list[tuple[str, str]] = []

    identity = ensure_deployment_identity(
        values,
        set_value=lambda key, value: writes.append((key, value)),
        token_factory=lambda: "must-not-be-used",
    )

    assert identity is None
    assert writes == []


def test_new_environment_gets_one_persisted_deployment_identity() -> None:
    values = {"AZURE_ENV_NAME": "research"}
    writes: list[tuple[str, str]] = []

    identity = ensure_deployment_identity(
        values,
        set_value=lambda key, value: writes.append((key, value)),
        token_factory=lambda: "111122223333",
    )

    assert identity == deployment_identity("research", "111122223333")
    assert writes == [
        ("AZURE_DEPLOYMENT_INCARNATION", "111122223333"),
        ("FOUNDRY_ACCOUNT_NAME", identity.foundry_account_name),
        ("FOUNDRY_PROJECT_NAME", identity.foundry_project_name),
    ]


def test_successful_down_rotates_salt_account_and_project_together() -> None:
    values = {
        "AZURE_ENV_NAME": "research",
        "AZURE_DEPLOYMENT_INCARNATION": "111122223333",
        "FOUNDRY_ACCOUNT_NAME": "cog-old",
        "FOUNDRY_PROJECT_NAME": "old-project",
    }
    writes: list[tuple[str, str]] = []

    identity = rotate_deployment_identity(
        values,
        set_value=lambda key, value: writes.append((key, value)),
        token_factory=lambda: "aaaabbbbcccc",
    )

    assert identity == deployment_identity("research", "aaaabbbbcccc")
    assert writes == [
        ("AZURE_DEPLOYMENT_INCARNATION", "aaaabbbbcccc"),
        ("FOUNDRY_ACCOUNT_NAME", identity.foundry_account_name),
        ("FOUNDRY_PROJECT_NAME", identity.foundry_project_name),
    ]


def test_ensure_repairs_names_from_the_last_committed_incarnation() -> None:
    values = {
        "AZURE_ENV_NAME": "research",
        "AZURE_DEPLOYMENT_INCARNATION": "111122223333",
        "FOUNDRY_ACCOUNT_NAME": "cog-partial-next-rotation",
        "FOUNDRY_PROJECT_NAME": "partial-next-project",
    }
    writes: list[tuple[str, str]] = []

    identity = ensure_deployment_identity(
        values,
        set_value=lambda key, value: writes.append((key, value)),
    )

    assert identity == deployment_identity("research", "111122223333")
    assert writes == [
        ("FOUNDRY_ACCOUNT_NAME", identity.foundry_account_name),
        ("FOUNDRY_PROJECT_NAME", identity.foundry_project_name),
    ]


def test_legacy_identity_rejects_a_contradictory_override() -> None:
    values = {
        "AZURE_ENV_NAME": "research",
        "AZURE_AI_ACCOUNT_NAME": "cog-authoritative",
        "AZURE_AI_PROJECT_NAME": "project-authoritative",
        "FOUNDRY_ACCOUNT_NAME": "cog-different",
        "FOUNDRY_PROJECT_NAME": "project-authoritative",
    }

    try:
        ensure_deployment_identity(values, set_value=lambda _key, _value: None)
    except RuntimeError as exc:
        assert "contradicts authoritative" in str(exc)
    else:
        raise AssertionError("Contradictory legacy identity was accepted")


def test_interrupted_identity_writes_repair_from_committed_incarnation() -> None:
    for fail_after in (1, 2, 3):
        values: dict[str, str] = {"AZURE_ENV_NAME": "research"}
        state = {"writes": 0}

        def interrupted_set(
            key: str,
            value: str,
            *,
            target: dict[str, str] = values,
            counter: dict[str, int] = state,
            threshold: int = fail_after,
        ) -> None:
            target[key] = value
            counter["writes"] += 1
            if counter["writes"] == threshold:
                raise RuntimeError("interrupted write")

        try:
            ensure_deployment_identity(
                values,
                set_value=interrupted_set,
                token_factory=lambda: "111122223333",
            )
        except RuntimeError as exc:
            assert str(exc) == "interrupted write"

        repaired: list[tuple[str, str]] = []

        def repair_set(
            key: str,
            value: str,
            *,
            target: dict[str, str] = values,
            observed: list[tuple[str, str]] = repaired,
        ) -> None:
            target[key] = value
            observed.append((key, value))

        identity = ensure_deployment_identity(
            values,
            set_value=repair_set,
        )
        assert identity == deployment_identity("research", "111122223333")
        assert values["FOUNDRY_ACCOUNT_NAME"] == identity.foundry_account_name
        assert values["FOUNDRY_PROJECT_NAME"] == identity.foundry_project_name


def test_down_completion_waits_for_group_deletion_and_foundry_purge() -> None:
    target = DeletionTarget(
        subscription_id="subscription-1",
        resource_group="rg-research",
        foundry_account_name="cog-research",
    )
    states = iter(
        [(True, True), (False, True), (False, False), (False, False), (False, False)]
    )
    sleeps: list[float] = []

    observed = wait_for_down_completion(
        {
            "AZURE_SUBSCRIPTION_ID": target.subscription_id,
            "AZURE_RESOURCE_GROUP": target.resource_group,
            "AZURE_AI_ACCOUNT_NAME": target.foundry_account_name,
        },
        read_state=lambda actual: next(states) if actual == target else (True, True),
        attempts=5,
        delay_seconds=0.25,
        sleep=sleeps.append,
    )

    assert observed == target
    assert sleeps == [0.25, 0.25, 0.25, 0.25]


def test_down_completion_resets_stable_absences_when_soft_delete_reappears() -> None:
    states = iter(
        [(False, False), (False, True), (False, False), (False, False), (False, False)]
    )
    sleeps: list[float] = []

    wait_for_down_completion(
        {
            "AZURE_SUBSCRIPTION_ID": "subscription-1",
            "AZURE_RESOURCE_GROUP": "rg-research",
            "FOUNDRY_ACCOUNT_NAME": "cog-research",
        },
        read_state=lambda _target: next(states),
        attempts=5,
        delay_seconds=0.25,
        sleep=sleeps.append,
    )

    assert sleeps == [0.25, 0.25, 0.25, 0.25]


def test_down_completion_prefers_current_incarnation_over_stale_output() -> None:
    target = DeletionTarget(
        subscription_id="subscription-1",
        resource_group="rg-research",
        foundry_account_name="cog-current-incarnation",
    )

    observed = wait_for_down_completion(
        {
            "AZURE_SUBSCRIPTION_ID": target.subscription_id,
            "AZURE_RESOURCE_GROUP": target.resource_group,
            "FOUNDRY_ACCOUNT_NAME": target.foundry_account_name,
            "AZURE_AI_ACCOUNT_NAME": "cog-stale-previous-output",
        },
        read_state=lambda actual: (False, False) if actual == target else (True, True),
        attempts=1,
        delay_seconds=0,
        stable_absences=1,
        sleep=lambda _delay: None,
    )

    assert observed == target


def test_down_completion_uses_environment_name_after_azd_clears_group_output() -> None:
    target = DeletionTarget(
        subscription_id="subscription-1",
        resource_group="rg-research",
        foundry_account_name="cog-current-incarnation",
    )

    observed = wait_for_down_completion(
        {
            "AZURE_ENV_NAME": target.resource_group,
            "AZURE_SUBSCRIPTION_ID": target.subscription_id,
            "FOUNDRY_ACCOUNT_NAME": target.foundry_account_name,
        },
        read_state=lambda actual: (False, False) if actual == target else (True, True),
        attempts=1,
        delay_seconds=0,
        stable_absences=1,
        sleep=lambda _delay: None,
    )

    assert observed == target


def test_failed_down_verification_never_rotates_identity() -> None:
    writes: list[tuple[str, str]] = []

    try:
        rotate_after_verified_down(
            {
                "AZURE_ENV_NAME": "research",
                "AZURE_SUBSCRIPTION_ID": "subscription-1",
                "AZURE_RESOURCE_GROUP": "rg-research",
                "AZURE_AI_ACCOUNT_NAME": "cog-research",
            },
            set_value=lambda key, value: writes.append((key, value)),
            read_state=lambda _target: (False, True),
            attempts=1,
            delay_seconds=0,
            sleep=lambda _delay: None,
        )
    except RuntimeError as exc:
        assert "did not complete" in str(exc)
    else:
        raise AssertionError("Identity rotated before Foundry purge completed")

    assert writes == []