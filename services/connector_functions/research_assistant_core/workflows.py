from __future__ import annotations

from dataclasses import dataclass

from research_assistant_core.models import Capability


@dataclass(frozen=True, slots=True)
class WorkflowStage:
    id: str
    label: str
    description: str
    owner: str
    human_checkpoint: bool = False


@dataclass(frozen=True, slots=True)
class WorkflowBlueprint:
    capability: Capability
    title: str
    purpose: str
    stages: tuple[WorkflowStage, ...]
    primary_artifact: str
    online_research_policy: str


WORKFLOW_BLUEPRINTS: dict[Capability, WorkflowBlueprint] = {
    Capability.LITERATURE: WorkflowBlueprint(
        capability=Capability.LITERATURE,
        title="Evidence review protocol",
        purpose="Build a reproducible review from search strategy through claim audit.",
        stages=(
            WorkflowStage("protocol", "Define protocol", "Question, scope, dates, and inclusion rules.", "researcher"),
            WorkflowStage(
                "search",
                "Search sources",
                "Query scholarly connectors and approved current web sources.",
                "literature-agent",
            ),
            WorkflowStage(
                "screen",
                "Screen & deduplicate",
                "Normalize records and record inclusion decisions.",
                "researcher",
                True,
            ),
            WorkflowStage(
                "extract",
                "Extract study data",
                "Capture methods, populations, outcomes, and limitations.",
                "literature-agent",
            ),
            WorkflowStage(
                "synthesize", "Synthesize evidence", "Compare consensus, disagreements, and gaps.", "literature-agent"
            ),
            WorkflowStage(
                "audit", "Audit claims", "Resolve every claim to stored passages and identifiers.", "citation-validator"
            ),
        ),
        primary_artifact="Review synthesis and evidence matrix",
        online_research_policy="optional-public-only",
    ),
    Capability.GRANT: WorkflowBlueprint(
        capability=Capability.GRANT,
        title="Application development lifecycle",
        purpose="Move from a funding notice and verified project facts to a review-ready package.",
        stages=(
            WorkflowStage(
                "opportunity",
                "Lock opportunity",
                "Capture sponsor, notice, deadline, and required format.",
                "researcher",
            ),
            WorkflowStage(
                "requirements",
                "Parse requirements",
                "Build a compliance matrix from the authoritative notice.",
                "grant-agent",
            ),
            WorkflowStage(
                "facts",
                "Inventory project facts",
                "Separate verified facts from missing inputs and placeholders.",
                "researcher",
                True,
            ),
            WorkflowStage(
                "aims",
                "Shape specific aims",
                "Create the argument, outcomes, and measurable objectives.",
                "grant-agent",
            ),
            WorkflowStage(
                "sections", "Draft sections", "Draft against the matrix without inventing commitments.", "grant-agent"
            ),
            WorkflowStage(
                "compliance",
                "Compliance check",
                "Check page, section, attachment, and sponsor requirements.",
                "compliance-validator",
            ),
            WorkflowStage(
                "red-team",
                "Red-team review",
                "Challenge significance, feasibility, evidence, and reviewer objections.",
                "grant-agent",
            ),
            WorkflowStage(
                "approval",
                "Review & export",
                "Approve the exact package and destination before export.",
                "reviewer",
                True,
            ),
        ),
        primary_artifact="Grant application package",
        online_research_policy="opportunity-only",
    ),
    Capability.MATCHING: WorkflowBlueprint(
        capability=Capability.MATCHING,
        title="Transparent matching pipeline",
        purpose="Create an evidence-backed shortlist without inventing people, resources, or availability.",
        stages=(
            WorkflowStage(
                "criteria",
                "Define criteria",
                "Capture expertise, methods, location, role, and resource needs.",
                "researcher",
            ),
            WorkflowStage(
                "filters",
                "Apply hard filters",
                "Exclude records that fail explicit eligibility rules.",
                "matching-engine",
            ),
            WorkflowStage(
                "resolve",
                "Resolve entities",
                "Merge public leads with authoritative institutional records.",
                "entity-resolver",
            ),
            WorkflowStage(
                "score",
                "Score evidence",
                "Calculate weighted score components from stored attributes.",
                "matching-engine",
            ),
            WorkflowStage(
                "compare",
                "Compare shortlist",
                "Explain fit, gaps, freshness, and evidence side by side.",
                "matching-agent",
            ),
            WorkflowStage(
                "confirm", "Confirm outreach", "Verify availability and approve any contact action.", "researcher", True
            ),
        ),
        primary_artifact="Verified collaborator and resource shortlist",
        online_research_policy="public-metadata-only",
    ),
    Capability.DATASET: WorkflowBlueprint(
        capability=Capability.DATASET,
        title="Dataset analysis lab",
        purpose="Separate deterministic computation from model interpretation and scale-out approval.",
        stages=(
            WorkflowStage("asset", "Select assets", "Choose datasets, notebooks, outputs, and lineage.", "researcher"),
            WorkflowStage(
                "validate", "Validate schema", "Check types, size, missingness, and safety limits.", "dataset-profiler"
            ),
            WorkflowStage(
                "profile", "Profile data", "Compute distributions, ranges, quality, and outliers.", "dataset-profiler"
            ),
            WorkflowStage(
                "plan", "Draft analysis plan", "Choose questions, metrics, comparisons, and tests.", "researcher", True
            ),
            WorkflowStage(
                "compute",
                "Compute metrics",
                "Run deterministic local or approved external calculations.",
                "compute-adapter",
            ),
            WorkflowStage(
                "interpret",
                "Interpret outputs",
                "Explain only computed results and explicit uncertainty.",
                "dataset-agent",
            ),
            WorkflowStage(
                "scale",
                "Approve scale-out",
                "Review cost, duration, destination, and idempotency key.",
                "reviewer",
                True,
            ),
        ),
        primary_artifact="Dataset profile, analysis plan, and verified summary",
        online_research_policy="metadata-only",
    ),
    Capability.INSTITUTIONAL_QA: WorkflowBlueprint(
        capability=Capability.INSTITUTIONAL_QA,
        title="Institutional answer workflow",
        purpose="Answer from authorized, versioned institutional evidence or abstain.",
        stages=(
            WorkflowStage(
                "scope", "Classify scope", "Identify policy domain and responsible corpus.", "scope-classifier"
            ),
            WorkflowStage(
                "acl",
                "Apply access controls",
                "Resolve tenant, group, and document access before retrieval.",
                "authorization",
            ),
            WorkflowStage(
                "versions",
                "Resolve versions",
                "Select effective versions and identify superseded records.",
                "policy-resolver",
            ),
            WorkflowStage(
                "conflicts", "Detect conflicts", "Surface contradictory passages or effective dates.", "policy-resolver"
            ),
            WorkflowStage(
                "retrieve", "Retrieve passages", "Retrieve authorized page and section evidence.", "institution-agent"
            ),
            WorkflowStage(
                "answer",
                "Answer or abstain",
                "Return scoped guidance, uncertainty, citations, or an answer gap.",
                "institution-agent",
            ),
        ),
        primary_artifact="Version-aware institutional answer",
        online_research_policy="forbidden",
    ),
    Capability.ORCHESTRATION: WorkflowBlueprint(
        capability=Capability.ORCHESTRATION,
        title="Workflow automation builder",
        purpose="Configure repeatable pipelines with explicit triggers, dependencies, and approvals.",
        stages=(
            WorkflowStage(
                "template", "Choose template", "Start from ingest, review, grant, or dataset templates.", "researcher"
            ),
            WorkflowStage(
                "triggers", "Configure triggers", "Define manual, upload, schedule, or API triggers.", "researcher"
            ),
            WorkflowStage(
                "dag",
                "Validate graph",
                "Check typed inputs, dependencies, retries, and compensation.",
                "workflow-validator",
            ),
            WorkflowStage(
                "gates", "Set approval gates", "Bind risky actions to named approvers and evidence.", "researcher", True
            ),
            WorkflowStage(
                "dry-run", "Dry run", "Execute with fixtures and no external side effects.", "durable-worker"
            ),
            WorkflowStage(
                "activate", "Activate automation", "Approve the exact graph and enable its trigger.", "reviewer", True
            ),
        ),
        primary_artifact="Versioned workflow definition",
        online_research_policy="per-step",
    ),
}


def workflow_for(capability: Capability) -> WorkflowBlueprint:
    return WORKFLOW_BLUEPRINTS[capability]
