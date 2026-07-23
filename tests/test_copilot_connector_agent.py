from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_copilot_cloud_agent_has_connector_only_boundary() -> None:
    agent = (
        ROOT / ".github" / "agents" / "connector-builder.agent.md"
    ).read_text(encoding="utf-8")

    assert "target: github-copilot" in agent
    assert "disable-model-invocation: true" in agent
    assert "packages/research_connectors/" in agent
    assert "services/connector_adapter/" in agent
    assert "Do not edit APIM policy" in agent
    assert "Do not approve, merge, deploy" in agent
    assert "SSRF" in agent


def test_copilot_setup_steps_use_the_required_job_and_least_privilege() -> None:
    workflow = yaml.safe_load(
        (
            ROOT / ".github" / "workflows" / "copilot-setup-steps.yml"
        ).read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["copilot-setup-steps"]

    assert job["permissions"] == {"contents": "read"}
    assert job["runs-on"] == "ubuntu-latest"
    commands = "\n".join(
        str(step.get("run", "")) for step in job["steps"]
    )
    assert "uv sync --all-packages --frozen" in commands
    assert "npm ci" in commands
    assert "az login" not in commands


def test_connector_request_requires_policy_and_validation_inputs() -> None:
    issue = yaml.safe_load(
        (
            ROOT
            / ".github"
            / "ISSUE_TEMPLATE"
            / "connector_request.yml"
        ).read_text(encoding="utf-8")
    )
    fields = {item.get("id"): item for item in issue["body"] if item.get("id")}

    assert {"provider", "purpose", "documentation", "terms", "allowlist", "authentication", "sample"}.issubset(fields)
    assert all(
        fields[field]["validations"]["required"]
        for field in (
            "provider",
            "purpose",
            "documentation",
            "terms",
            "allowlist",
            "authentication",
            "sample",
        )
    )
