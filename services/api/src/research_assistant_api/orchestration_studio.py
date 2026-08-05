from __future__ import annotations

import json
from hashlib import sha256
from uuid import uuid4

from research_assistant_core.models import Capability, RunStatus
from research_assistant_core.studio_models import (
    AutomationStep,
    AutomationStudioResult,
    StudioRun,
    StudioRunRequest,
)


class OrchestrationStudioService:
    def run(
        self,
        request: StudioRunRequest,
        *,
        owner: str,
    ) -> AutomationStudioResult:
        raw_steps = request.inputs.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("Workflow validation requires at least one submitted step.")
        steps = [AutomationStep.model_validate(item) for item in raw_steps]

        raw_template_id = request.inputs.get("template_id")
        if not isinstance(raw_template_id, str) or not raw_template_id.strip():
            raise ValueError("Workflow validation requires a template identifier.")
        template_id = raw_template_id.strip()

        raw_trigger = request.inputs.get("trigger")
        if not isinstance(raw_trigger, str) or not raw_trigger.strip():
            raise ValueError("Workflow validation requires a trigger.")
        trigger = raw_trigger.strip()

        errors = self._validation_errors(steps, trigger)
        canonical_graph = {
            "template_id": template_id,
            "trigger": trigger,
            "steps": [
                step.model_dump(mode="json")
                for step in sorted(steps, key=lambda item: item.id)
            ],
        }
        graph_hash = sha256(
            json.dumps(
                canonical_graph,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        run_id = f"run-{uuid4().hex[:12]}"
        blocked = bool(errors)
        return AutomationStudioResult(
            run=StudioRun(
                id=run_id,
                durable_instance_id=f"research-{run_id}",
                capability=Capability.ORCHESTRATION,
                title="Workflow graph validation",
                status=RunStatus.BLOCKED if blocked else RunStatus.WAITING_FOR_APPROVAL,
                current_stage="Validate graph" if blocked else "Activate automation",
                progress=35 if blocked else 80,
                owner=owner,
            ),
            template_id=template_id,
            trigger=trigger,
            steps=steps,
            validation_errors=errors,
            dry_run_status="blocked" if blocked else "passed",
            graph_version="2.0",
            graph_hash=graph_hash,
            citations=[],
            insight=None,
        )

    @staticmethod
    def _validation_errors(
        steps: list[AutomationStep],
        trigger: str,
    ) -> list[str]:
        errors: list[str] = []
        ids = {step.id for step in steps}
        if len(ids) != len(steps):
            errors.append("Workflow step IDs must be unique")
        errors.extend(
            f"{step.id} depends on unknown step {dependency}"
            for step in steps
            for dependency in step.depends_on
            if dependency not in ids
        )
        if trigger not in {"Manual", "Library upload", "Schedule", "API event"}:
            errors.append(f"Unsupported workflow trigger: {trigger}")

        approval_gates = [step for step in steps if step.approval_required]
        if len(approval_gates) > 1:
            errors.append("V2 automation graphs support one exact activation gate")
        for step in steps:
            if step.id in step.depends_on:
                errors.append(f"{step.id} cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {step.id: step for step in steps}

        def visit(step_id: str) -> None:
            if step_id in visited or step_id not in by_id:
                return
            if step_id in visiting:
                errors.append(f"Workflow graph contains a cycle at {step_id}")
                return
            visiting.add(step_id)
            for dependency in by_id[step_id].depends_on:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)

        for step in steps:
            visit(step.id)

        def has_approval_ancestor(
            step_id: str,
            checked: set[str] | None = None,
        ) -> bool:
            checked = set() if checked is None else checked
            if step_id in checked or step_id not in by_id:
                return False
            checked.add(step_id)
            step = by_id[step_id]
            return any(
                by_id[dependency].approval_required
                or has_approval_ancestor(dependency, checked)
                for dependency in step.depends_on
                if dependency in by_id
            )

        errors.extend(
            f"{step.id} external actions require an approval ancestor"
            for step in steps
            if step.kind == "external_action"
            and not step.approval_required
            and not has_approval_ancestor(step.id)
        )
        return list(dict.fromkeys(errors))
