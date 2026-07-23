from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any

from research_assistant_core.models import (
    Capability,
    Citation,
    ResearchRequest,
    ResearchResult,
    RunStatus,
)
from research_assistant_core.service import ResearchService
from research_assistant_core.studio_models import (
    AgentInsight,
    AnalysisStep,
    AutomationStep,
    AutomationStudioResult,
    ComputeProposal,
    DatasetFieldProfile,
    DatasetStudioResult,
    DraftSection,
    EvidenceExtraction,
    EvidenceState,
    GrantOpportunity,
    GrantRequirement,
    GrantStudioResult,
    InstitutionalStudioResult,
    LiteratureStudioResult,
    MatchCriterion,
    MatchingStudioResult,
    PolicyConflict,
    PolicyVersion,
    ProjectFactGap,
    RankedEntity,
    ReviewProtocol,
    ScoreComponent,
    ScreeningDecision,
    StudioRun,
    StudioRunRequest,
)
from research_assistant_core.workflows import workflow_for

_SOURCE_TOKEN = re.compile(
    r"(?:source(?:_id)?|citation(?:_id)?)\s*[:=]\s*[`\"']?([a-z0-9][a-z0-9_-]{2,})",
    re.IGNORECASE,
)


def validate_agent_insight(
    *,
    agent_name: str,
    content: str,
    allowed_source_ids: set[str],
    online_research_used: bool,
) -> AgentInsight:
    normalized = content.strip()[:8000]
    referenced = sorted(source_id for source_id in allowed_source_ids if source_id in normalized)
    declared = {match.group(1) for match in _SOURCE_TOKEN.finditer(normalized)}
    unresolved = sorted(declared.difference(allowed_source_ids))
    return AgentInsight(
        agent_name=agent_name,
        content=normalized,
        evidence_state=(EvidenceState.MODEL_ANALYSIS if normalized else EvidenceState.UNSUPPORTED),
        referenced_source_ids=referenced,
        unresolved_source_ids=unresolved,
        online_research_used=online_research_used,
    )


class StudioService:
    def __init__(self, research: ResearchService) -> None:
        self._research = research

    def run(
        self,
        capability: Capability,
        request: StudioRunRequest,
        *,
        tenant_id: str,
        project_id: str,
        group_ids: list[str],
        owner: str,
        hosted_content: str | None = None,
        hosted_agent_name: str | None = None,
        generic: ResearchResult | None = None,
        dataset_compute_authorized: bool = False,
    ) -> (
        LiteratureStudioResult
        | GrantStudioResult
        | MatchingStudioResult
        | DatasetStudioResult
        | InstitutionalStudioResult
        | AutomationStudioResult
    ):
        generic = generic or self._research.run(
            capability,
            ResearchRequest(
                query=request.objective,
                project_id=project_id,
                tenant_id=tenant_id,
                group_ids=group_ids,
                context=request.inputs,
            ),
        )
        insight = (
            validate_agent_insight(
                agent_name=hosted_agent_name or "unknown-agent",
                content=hosted_content,
                allowed_source_ids={citation.source_id for citation in generic.citations},
                online_research_used=request.online_research,
            )
            if hosted_content is not None
            else None
        )
        if capability == Capability.LITERATURE:
            return self._literature(generic, request, owner, insight)
        if capability == Capability.GRANT:
            return self._grant(generic, request, owner, insight)
        if capability == Capability.MATCHING:
            return self._matching(generic, request, owner, insight)
        if capability == Capability.DATASET:
            return self._dataset(
                generic,
                request,
                owner,
                insight,
                compute_authorized=dataset_compute_authorized,
            )
        if capability == Capability.INSTITUTIONAL_QA:
            return self._institutional(generic, request, owner, insight)
        return self._automation(generic, request, owner, insight)

    @staticmethod
    def _studio_run(
        generic: ResearchResult,
        owner: str,
        *,
        status: RunStatus = RunStatus.COMPLETED,
        progress: int = 100,
        current_stage: str | None = None,
    ) -> StudioRun:
        blueprint = workflow_for(generic.run.capability)
        return StudioRun(
            id=generic.run.id,
            durable_instance_id=f"research-{generic.run.id}",
            capability=generic.run.capability,
            title=generic.title,
            status=status,
            current_stage=current_stage or blueprint.stages[-1].label,
            progress=progress,
            started_at=generic.run.started_at,
            owner=owner,
        )

    @staticmethod
    def _literature(
        generic: ResearchResult,
        request: StudioRunRequest,
        owner: str,
        insight: AgentInsight | None,
    ) -> LiteratureStudioResult:
        try:
            start_year = int(request.inputs.get("date_from", 2020))
            end_year = int(request.inputs.get("date_to", 2026))
        except (TypeError, ValueError) as exc:
            raise ValueError("Literature publication years must be integers.") from exc
        if not 1000 <= start_year <= end_year <= 2100:
            raise ValueError("Literature publication window is invalid.")
        raw_sources = request.inputs.get(
            "sources",
            ["PubMed", "Europe PMC", "Crossref", "OpenAlex"],
        )
        if not isinstance(raw_sources, list) or not all(isinstance(source, str) for source in raw_sources):
            raise ValueError("Literature sources must be a list of provider names.")
        sources = list(dict.fromkeys(raw_sources))
        grouped: dict[str, list[Citation]] = {}
        for citation in generic.citations:
            grouped.setdefault(citation.source_id, []).append(citation)
        screening = [
            ScreeningDecision(
                source_id=source_id,
                title=citations[0].title,
                decision="include",
                reason="Matches the protocol topic and provides extractable methods or limitations.",
                duplicate_group=None,
            )
            for source_id, citations in grouped.items()
        ]
        extraction = [
            EvidenceExtraction(
                source_id=source_id,
                method=(
                    "Hybrid retrieval evaluation"
                    if any("retrieval" in citation.quote.lower() for citation in citations)
                    else "Evidence workflow evaluation"
                ),
                population="Synthetic benchmark research corpus",
                outcome=" ".join(citation.quote for citation in citations),
                limitation="Fixture evidence; external scientific validity is not established.",
                citation_ids=[citation.id for citation in citations],
            )
            for source_id, citations in grouped.items()
        ]
        return LiteratureStudioResult(
            run=StudioService._studio_run(generic, owner),
            protocol=ReviewProtocol(
                research_question=request.objective,
                date_from=start_year,
                date_to=end_year,
                sources=sources,
                inclusion_criteria=list(
                    request.inputs.get(
                        "inclusion_criteria",
                        ["Primary or benchmark study", "Methods and limitations available"],
                    )
                ),
                exclusion_criteria=list(
                    request.inputs.get(
                        "exclusion_criteria",
                        ["No extractable evidence", "Duplicate record"],
                    )
                ),
            ),
            search_queries=[
                request.objective,
                f"({request.objective}) AND (provenance OR reproducibility)",
            ],
            candidate_count=len(screening),
            screening=screening,
            extraction_matrix=extraction,
            synthesis=[section.body for section in generic.sections],
            citations=generic.citations,
            insight=insight,
        )

    @staticmethod
    def _grant(
        generic: ResearchResult,
        request: StudioRunRequest,
        owner: str,
        insight: AgentInsight | None,
    ) -> GrantStudioResult:
        facts = list(request.inputs.get("project_facts", []))
        grant_evidence = [citation.id for citation in generic.citations]
        requirements = [
            ("summary", "Two-page project summary", "Narrative"),
            ("aims", "Three specific aims", "Narrative"),
            ("dmp", "Data-management and sharing plan", "Attachment"),
            ("evaluation", "Measurable evaluation plan", "Narrative"),
            ("sustainability", "Sustainability section", "Narrative"),
            ("budget", "Budget justification", "Budget"),
            ("period", "Project period no longer than 36 months", "Eligibility"),
        ]
        fact_gaps = [
            ProjectFactGap(
                id="institutional-commitment",
                label="Institutional commitments",
                status="verified" if facts else "missing",
                guidance="Add a named, owner-verified commitment or retain the TODO marker.",
            ),
            ProjectFactGap(
                id="budget",
                label="Approved budget figures",
                status="missing",
                guidance="Budget values require the finance owner and cannot be model-generated.",
            ),
        ]
        return GrantStudioResult(
            run=StudioService._studio_run(
                generic,
                owner,
                status=RunStatus.WAITING_FOR_APPROVAL,
                progress=86,
                current_stage="Review & export",
            ),
            opportunity=GrantOpportunity(
                identifier=str(request.inputs.get("opportunity_id", "SORI-2026-01")),
                sponsor=str(request.inputs.get("sponsor", "Example Federal Research Office")),
                title="Open Research Infrastructure Opportunity",
                deadline=str(request.inputs.get("deadline", "2026-10-15")),
                status="Open",
                canonical_url="https://www.grants.gov/",
            ),
            requirements=[
                GrantRequirement(
                    id=item[0],
                    text=item[1],
                    category=item[2],
                    status="mapped" if item[0] not in {"budget"} else "needs_input",
                    evidence_ids=grant_evidence,
                )
                for item in requirements
            ],
            fact_gaps=fact_gaps,
            specific_aims=[
                "Establish an evidence-governed research workspace.",
                "Evaluate grounded workflows with measurable quality and access outcomes.",
                "Create a sustainable institutional adoption and training model.",
            ],
            sections=[
                DraftSection(
                    id=section.heading.lower().replace(" ", "-"),
                    title=section.heading,
                    status="draft",
                    word_count=len(section.body.split()),
                    body=section.body,
                    evidence_ids=section.citation_ids,
                )
                for section in generic.sections
            ],
            readiness=86 if facts else 72,
            blockers=[gap.label for gap in fact_gaps if gap.status == "missing"],
            citations=generic.citations,
            insight=insight,
        )

    @staticmethod
    def _matching(
        generic: ResearchResult,
        request: StudioRunRequest,
        owner: str,
        insight: AgentInsight | None,
    ) -> MatchingStudioResult:
        criteria = [
            MatchCriterion(
                id="expertise",
                label="Expertise and methods",
                value=request.objective,
                kind="weighted",
                weight=0.65,
            ),
            MatchCriterion(
                id="record-type",
                label="Eligible record type",
                value="Person, facility, equipment, method, or template",
                kind="hard_filter",
                weight=0.2,
            ),
            MatchCriterion(
                id="freshness",
                label="Record freshness",
                value="Prefer current verified records",
                kind="weighted",
                weight=0.15,
            ),
        ]
        matches = [
            RankedEntity(
                id=item.id,
                name=item.name,
                kind=item.kind.value,
                hard_filters_passed=True,
                score=item.score,
                components=[
                    ScoreComponent(
                        criterion_id=factor.id,
                        label=factor.label,
                        weight=factor.weight,
                        match=factor.match,
                        contribution=round(factor.contribution * 100, 1),
                        evidence_id=factor.evidence_id,
                    )
                    for factor in item.score_factors
                ],
                strengths=item.tags[:3] or [item.rationale],
                gaps=["Availability requires owner confirmation"],
                freshness=item.freshness,
            )
            for item in generic.matches
        ]
        return MatchingStudioResult(
            run=StudioService._studio_run(generic, owner),
            criteria=criteria,
            matches=matches,
            shortlist_ids=[item.id for item in matches[:3]],
            citations=generic.citations,
            insight=insight,
        )

    @staticmethod
    def _dataset(
        generic: ResearchResult,
        request: StudioRunRequest,
        owner: str,
        insight: AgentInsight | None,
        *,
        compute_authorized: bool,
    ) -> DatasetStudioResult:
        if request.inputs.get("csv_text") and not compute_authorized:
            raise ValueError("Trusted dataset compute approval context is required.")
        profile = generic.metadata["profile"]
        estimated_bytes = int(request.inputs.get("estimated_bytes", 0))
        requires_scale_out = estimated_bytes > 5_000_000
        adapter_configured = bool(request.inputs.get("compute_adapter_configured", False))
        requires_approval = requires_scale_out and adapter_configured
        fields = []
        if profile is not None:
            for item in profile["column_profiles"]:
                range_or_values = (
                    f"{item['minimum']} to {item['maximum']}"
                    if item.get("minimum") is not None
                    else f"{item['unique_count']} distinct values"
                )
                fields.append(
                    DatasetFieldProfile(
                        name=item["name"],
                        data_type=item["dtype"],
                        missing=item["null_count"],
                        unique=item["unique_count"],
                        range_or_values=range_or_values,
                    )
                )
        return DatasetStudioResult(
            run=StudioService._studio_run(
                generic,
                owner,
                status=(
                    RunStatus.WAITING_FOR_APPROVAL
                    if requires_approval
                    else RunStatus.BLOCKED
                    if profile is None or requires_scale_out
                    else RunStatus.COMPLETED
                ),
                progress=(72 if requires_approval else 15 if profile is None or requires_scale_out else 100),
                current_stage=(
                    "Approve scale-out"
                    if requires_approval
                    else "Compute metrics"
                    if requires_scale_out
                    else "Select assets"
                    if profile is None
                    else "Interpret outputs"
                ),
            ),
            asset_name=str(request.inputs.get("filename", "sample-outcomes.csv")),
            profile_status="estimate_only" if profile is None else "computed",
            profile_note=(
                "No raw data was read; fields remain unavailable."
                if profile is None
                else "Profile computed deterministically from the selected asset."
            ),
            row_count=profile["rows"] if profile is not None else 0,
            column_count=profile["columns"] if profile is not None else 0,
            fields=fields,
            quality_findings=(
                [
                    "No missing values were detected in the supplied fixture.",
                    "The sample is too small for an inferential or causal conclusion.",
                ]
                if profile is not None
                else ["Quality findings are unavailable until verified compute completes."]
            ),
            analysis_plan=[
                AnalysisStep(
                    id="profile",
                    question="What are the structure, ranges, and missingness?",
                    method="Deterministic Polars profile",
                    status="complete" if profile is not None else "blocked",
                    deterministic=True,
                ),
                AnalysisStep(
                    id="compare",
                    question="How do observed outcomes differ by group?",
                    method="Descriptive grouped comparison",
                    status="planned" if profile is not None else "blocked",
                    deterministic=True,
                ),
            ],
            compute_proposal=ComputeProposal(
                adapter=("Foundry Code Interpreter" if adapter_configured else "Not configured"),
                estimated_bytes=estimated_bytes,
                estimated_cost_usd=None,
                estimated_minutes=None,
                approval_required=requires_scale_out,
                stages=["estimate", "approve", "submit", "poll", "collect"],
            ),
            interpretation=[section.body for section in generic.sections],
            citations=generic.citations,
            insight=insight,
        )

    @staticmethod
    def _institutional(
        generic: ResearchResult,
        request: StudioRunRequest,
        owner: str,
        insight: AgentInsight | None,
    ) -> InstitutionalStudioResult:
        versions = [
            PolicyVersion(
                source_id=citation.source_id,
                title=citation.title,
                version="2026.1",
                effective_date="2026-01-15",
                status="effective",
            )
            for citation in generic.citations
        ]
        conflicts: list[PolicyConflict] = []
        if request.inputs.get("include_conflict_fixture"):
            conflicts.append(
                PolicyConflict(
                    topic="AI disclosure threshold",
                    source_a="policy-irb",
                    source_b="policy-irb-superseded",
                    description="The superseded version uses a broader disclosure threshold.",
                    severity="warning",
                )
            )
        abstained = not bool(generic.citations)
        return InstitutionalStudioResult(
            run=StudioService._studio_run(
                generic,
                owner,
                status=RunStatus.BLOCKED if abstained else RunStatus.COMPLETED,
                current_stage="Answer or abstain",
            ),
            scope=str(request.inputs.get("scope", "IRB and research compliance")),
            answer=None if abstained else generic.summary,
            abstained=abstained,
            versions=versions,
            conflicts=conflicts,
            citations=generic.citations,
            escalation=("Route this answer gap to the Research Compliance Office." if abstained else None),
            insight=insight,
        )

    @staticmethod
    def _automation(
        generic: ResearchResult,
        request: StudioRunRequest,
        owner: str,
        insight: AgentInsight | None,
    ) -> AutomationStudioResult:
        steps_data: list[dict[str, Any]] = list(
            request.inputs.get(
                "steps",
                [
                    {
                        "id": "ingest",
                        "label": "Ingest & verify",
                        "kind": "activity",
                        "depends_on": [],
                        "retry_limit": 3,
                        "approval_required": False,
                    },
                    {
                        "id": "retrieve",
                        "label": "Retrieve evidence",
                        "kind": "fan_out",
                        "depends_on": ["ingest"],
                        "retry_limit": 2,
                        "approval_required": False,
                    },
                    {
                        "id": "synthesize",
                        "label": "Synthesize artifact",
                        "kind": "agent",
                        "depends_on": ["retrieve"],
                        "retry_limit": 1,
                        "approval_required": False,
                    },
                    {
                        "id": "review",
                        "label": "Human review",
                        "kind": "approval",
                        "depends_on": ["synthesize"],
                        "retry_limit": 0,
                        "approval_required": True,
                    },
                    {
                        "id": "export",
                        "label": "Export approved artifact",
                        "kind": "external_action",
                        "depends_on": ["review"],
                        "retry_limit": 2,
                        "approval_required": False,
                    },
                ],
            )
        )
        steps = [AutomationStep.model_validate(item) for item in steps_data]
        ids = {step.id for step in steps}
        errors = [
            f"{step.id} depends on unknown step {dependency}"
            for step in steps
            for dependency in step.depends_on
            if dependency not in ids
        ]
        if not steps:
            errors.append("Workflow graph must contain at least one step")
        if len(ids) != len(steps):
            errors.append("Workflow step IDs must be unique")
        trigger = str(request.inputs.get("trigger", "Manual"))
        if trigger not in {
            "Manual",
            "Library upload",
            "Schedule",
            "API event",
        }:
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
                by_id[dependency].approval_required or has_approval_ancestor(dependency, checked)
                for dependency in step.depends_on
                if dependency in by_id
            )

        for step in steps:
            if step.kind == "external_action" and not step.approval_required and not has_approval_ancestor(step.id):
                errors.append(f"{step.id} external actions require an approval ancestor")

        template_id = str(request.inputs.get("template_id", "evidence-review-v2"))
        canonical_graph = {
            "template_id": template_id,
            "trigger": trigger,
            "steps": [step.model_dump(mode="json") for step in sorted(steps, key=lambda item: item.id)],
        }
        graph_hash = sha256(
            json.dumps(
                canonical_graph,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return AutomationStudioResult(
            run=StudioService._studio_run(
                generic,
                owner,
                status=(RunStatus.BLOCKED if errors else RunStatus.WAITING_FOR_APPROVAL),
                progress=35 if errors else 80,
                current_stage=("Validate graph" if errors else "Activate automation"),
            ),
            template_id=template_id,
            trigger=trigger,
            steps=steps,
            validation_errors=errors,
            dry_run_status="blocked" if errors else "passed",
            graph_version="2.0",
            graph_hash=graph_hash,
            citations=generic.citations,
            insight=insight,
        )
