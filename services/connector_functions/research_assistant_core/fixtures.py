from __future__ import annotations

from hashlib import sha256

from pydantic import HttpUrl

from research_assistant_core.models import EvidenceChunk, SourceKind


def _chunk(
    *,
    chunk_id: str,
    source_id: str,
    kind: SourceKind,
    title: str,
    content: str,
    section: str,
    page: int,
    url: str,
    authors: list[str] | None = None,
    year: int | None = None,
    metadata: dict[str, object] | None = None,
    allowed_tenants: list[str] | None = None,
) -> EvidenceChunk:
    digest = sha256(f"{source_id}\0{section}\0{content}".encode()).hexdigest()
    return EvidenceChunk(
        id=chunk_id,
        source_id=source_id,
        source_kind=kind,
        title=title,
        content=content,
        section=section,
        page_start=page,
        page_end=page,
        canonical_url=HttpUrl(url),
        identifier=f"urn:research-assistant:sample:{source_id}",
        authors=authors or [],
        year=year,
        checksum=digest,
        metadata={
            "fixture": True,
            "provider": (
                "PubMed"
                if kind == SourceKind.PAPER
                else "Grants.gov"
                if kind == SourceKind.GRANT
                else "Institutional Library"
            ),
            **(metadata or {}),
        },
        allowed_tenants=allowed_tenants or ["demo"],
    )


SAMPLE_EVIDENCE: tuple[EvidenceChunk, ...] = (
    _chunk(
        chunk_id="paper-rag-method",
        source_id="paper-rag",
        kind=SourceKind.PAPER,
        title="Provenance-first retrieval for research synthesis",
        content=(
            "The benchmark compared keyword, vector, and hybrid retrieval. "
            "Hybrid retrieval with reranking improved source recall while preserving "
            "exact matches for identifiers and named methods."
        ),
        section="Methods",
        page=4,
        url="https://example.edu/research/papers/provenance-rag",
        authors=["Maya Chen", "Oliver Smith"],
        year=2025,
        metadata={"method": "Controlled retrieval benchmark", "retracted": False},
    ),
    _chunk(
        chunk_id="paper-rag-findings",
        source_id="paper-rag",
        kind=SourceKind.PAPER,
        title="Provenance-first retrieval for research synthesis",
        content=(
            "Claim-level citation validation reduced unsupported references in the "
            "synthetic evaluation set. The authors caution that citation validity "
            "does not establish scientific correctness."
        ),
        section="Results and limitations",
        page=9,
        url="https://example.edu/research/papers/provenance-rag",
        authors=["Maya Chen", "Oliver Smith"],
        year=2025,
        metadata={"finding": "Citation validation improves reference integrity"},
    ),
    _chunk(
        chunk_id="paper-workflow-method",
        source_id="paper-workflow",
        kind=SourceKind.PAPER,
        title="Auditable workflows for multi-step evidence review",
        content=(
            "The workflow externalized plans, evidence decisions, and verification "
            "states into a typed run ledger. Human approval was required before any "
            "external write or paid compute action."
        ),
        section="System design",
        page=3,
        url="https://example.edu/research/papers/auditable-workflows",
        authors=["Amina Patel", "Jordan Lee"],
        year=2024,
        metadata={"method": "Workflow case study", "retracted": False},
    ),
    _chunk(
        chunk_id="paper-workflow-limits",
        source_id="paper-workflow",
        kind=SourceKind.PAPER,
        title="Auditable workflows for multi-step evidence review",
        content=(
            "The study used synthetic institutional documents and did not measure "
            "long-term adoption. Results should not be generalized to regulated "
            "clinical research without additional evaluation."
        ),
        section="Limitations",
        page=11,
        url="https://example.edu/research/papers/auditable-workflows",
        authors=["Amina Patel", "Jordan Lee"],
        year=2024,
        metadata={"limitation": "Synthetic corpus and short evaluation"},
    ),
    _chunk(
        chunk_id="paper-eval-results",
        source_id="paper-eval",
        kind=SourceKind.PAPER,
        title="Evaluating grounded assistants beyond answer fluency",
        content=(
            "The evaluation separated retrieval recall, citation entailment, citation "
            "completeness, abstention, latency, and cost. A single aggregate quality "
            "score concealed materially different failure modes."
        ),
        section="Evaluation",
        page=6,
        url="https://example.edu/research/papers/grounded-evaluation",
        authors=["Elena Garcia"],
        year=2026,
        metadata={"method": "Multi-metric evaluation", "retracted": False},
    ),
    _chunk(
        chunk_id="policy-irb-ai",
        source_id="policy-irb",
        kind=SourceKind.POLICY,
        title="Example University IRB guidance for AI-assisted research",
        content=(
            "Researchers must describe material use of generative AI in the protocol "
            "when it processes identifiable participant data or influences eligibility, "
            "risk, intervention, or outcome decisions. Public, de-identified literature "
            "search does not by itself trigger this disclosure requirement."
        ),
        section="4.2 Disclosure",
        page=7,
        url="https://example.edu/policies/irb-ai-guidance",
        year=2026,
        metadata={"effective_date": "2026-01-15", "policy_version": "3.1"},
    ),
    _chunk(
        chunk_id="policy-data-retention",
        source_id="policy-data",
        kind=SourceKind.POLICY,
        title="Example University research data retention standard",
        content=(
            "Final research records must be retained for seven years after project "
            "close unless a sponsor, legal hold, or discipline-specific rule requires "
            "a longer period. Working copies should be removed when no longer needed."
        ),
        section="3.1 Retention",
        page=4,
        url="https://example.edu/policies/research-data",
        year=2026,
        metadata={"effective_date": "2026-02-01", "policy_version": "2.0"},
    ),
    _chunk(
        chunk_id="grant-opportunity",
        source_id="grant-open-science",
        kind=SourceKind.GRANT,
        title="Synthetic Open Research Infrastructure Opportunity",
        content=(
            "Applications must include a two-page project summary, three specific aims, "
            "a data-management and sharing plan, an evaluation plan with measurable "
            "outcomes, a sustainability section, and a budget justification. The "
            "project period is limited to 36 months."
        ),
        section="Application requirements",
        page=12,
        url="https://example.edu/funding/open-research-infrastructure",
        year=2026,
        metadata={"deadline": "2026-11-30", "fixture": True},
    ),
    _chunk(
        chunk_id="person-chen",
        source_id="person-maya-chen",
        kind=SourceKind.PERSON,
        title="Dr. Maya Chen — Computational Biology",
        content=(
            "Maya Chen studies reproducible genomics, multimodal data integration, "
            "and machine-learning evaluation. Her group has recent collaborations "
            "with the Advanced Imaging Core."
        ),
        section="Expertise profile",
        page=1,
        url="https://example.edu/directory/maya-chen",
        metadata={
            "name": "Dr. Maya Chen",
            "tags": ["genomics", "machine learning", "reproducibility"],
            "freshness": "Updated 2026-06-20",
        },
    ),
    _chunk(
        chunk_id="facility-imaging",
        source_id="facility-imaging",
        kind=SourceKind.FACILITY,
        title="Advanced Imaging Core",
        content=(
            "The core provides confocal and light-sheet microscopy, image-analysis "
            "consulting, sample preparation, and instrument training for university "
            "research teams."
        ),
        section="Capabilities",
        page=1,
        url="https://example.edu/cores/advanced-imaging",
        metadata={
            "name": "Advanced Imaging Core",
            "tags": ["microscopy", "image analysis", "training"],
            "freshness": "Verified 2026-07-01",
        },
    ),
    _chunk(
        chunk_id="equipment-sequencer",
        source_id="equipment-sequencer",
        kind=SourceKind.EQUIPMENT,
        title="NovaSeq X Plus sequencing system",
        content=(
            "The Genomics Core operates a NovaSeq X Plus for high-throughput short-read "
            "sequencing and offers library quality control and experimental-design review."
        ),
        section="Equipment catalog",
        page=1,
        url="https://example.edu/cores/genomics/equipment",
        metadata={
            "name": "NovaSeq X Plus",
            "tags": ["sequencing", "genomics", "quality control"],
            "freshness": "Verified 2026-06-12",
        },
    ),
    _chunk(
        chunk_id="template-dmp",
        source_id="template-dmp",
        kind=SourceKind.TEMPLATE,
        title="Research data-management plan template",
        content=(
            "The template covers data types, metadata standards, access controls, "
            "preservation, sharing, responsibilities, and retention."
        ),
        section="Template overview",
        page=1,
        url="https://example.edu/research/templates/data-management-plan",
        metadata={
            "name": "Data-management plan template",
            "tags": ["data management", "sharing", "retention"],
            "freshness": "Version 4.0, 2026-05-08",
        },
    ),
    _chunk(
        chunk_id="private-policy",
        source_id="policy-private",
        kind=SourceKind.POLICY,
        title="Restricted tenant policy fixture",
        content="This fixture must never be returned to the demo tenant.",
        section="Restricted",
        page=1,
        url="https://example.edu/restricted/policy",
        metadata={"fixture": True},
        allowed_tenants=["restricted-tenant"],
    ),
)


SAMPLE_CSV = """participant_group,score,completion_minutes
control,72,48
control,75,51
control,70,49
intervention,84,42
intervention,88,39
intervention,86,41
"""
