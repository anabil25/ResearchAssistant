from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import uuid4

from research_assistant_core.dataset import profile_csv
from research_assistant_core.fixtures import SAMPLE_CSV, SAMPLE_EVIDENCE
from research_assistant_core.models import (
    ArtifactSection,
    Capability,
    CapabilitySpec,
    Citation,
    EvidenceChunk,
    MatchItem,
    MatchScoreFactor,
    Metric,
    ProvenanceManifest,
    ResearchRequest,
    ResearchResult,
    RunRecord,
    RunStatus,
    SourceKind,
)
from research_assistant_core.repositories import (
    EvidenceRepository,
    InMemoryEvidenceRepository,
)
from research_assistant_core.agent_surfaces import capability_specs
from research_assistant_core.security import enforce_safe_source

CAPABILITIES: tuple[CapabilitySpec, ...] = capability_specs()


class CitationValidationError(ValueError):
    pass


class ResearchService:
    def __init__(
        self,
        repository: EvidenceRepository | None = None,
        *,
        mode: str = "mock",
        model_deployment: str = "deterministic-fixture-model",
    ) -> None:
        self.repository = repository or InMemoryEvidenceRepository(SAMPLE_EVIDENCE)
        self.mode = mode
        self.model_deployment = model_deployment

    @property
    def capabilities(self) -> tuple[CapabilitySpec, ...]:
        return CAPABILITIES

    def run(self, capability: Capability, request: ResearchRequest) -> ResearchResult:
        handlers = {
            Capability.LITERATURE: self._literature,
            Capability.GRANT: self._grant,
            Capability.MATCHING: self._matching,
            Capability.DATASET: self._dataset,
            Capability.INSTITUTIONAL_QA: self._institutional_qa,
            Capability.ORCHESTRATION: self._orchestration,
        }
        handler = handlers.get(capability)
        if handler is None:
            raise ValueError(f"{capability} is a conversational capability with no studio workflow")
        result = handler(request)
        self._validate_result(result)
        return result

    def _new_run(
        self,
        capability: Capability,
        request: ResearchRequest,
        *,
        title: str,
        steps: list[str],
    ) -> RunRecord:
        now = datetime.now(UTC)
        return RunRecord(
            id=f"run-{uuid4().hex[:12]}",
            project_id=request.project_id,
            tenant_id=request.tenant_id,
            capability=capability,
            title=title,
            status=RunStatus.COMPLETED,
            started_at=now,
            completed_at=now,
            progress=100,
            current_step=steps[-1],
            steps=steps,
        )

    def _citation(self, chunk: EvidenceChunk) -> Citation:
        enforce_safe_source(chunk.content)
        return Citation(
            id=f"cite-{chunk.id}",
            source_id=chunk.source_id,
            chunk_id=chunk.id,
            title=chunk.title,
            canonical_url=chunk.canonical_url,
            identifier=chunk.identifier,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section=chunk.section,
            quote=chunk.content,
            checksum=chunk.checksum,
            license=chunk.license,
        )

    def _provenance(
        self,
        run: RunRecord,
        chunks: list[EvidenceChunk],
        *,
        caveats: list[str] | None = None,
    ) -> ProvenanceManifest:
        return ProvenanceManifest(
            run_id=run.id,
            capability=run.capability,
            mode=self.mode,
            source_ids=sorted({chunk.source_id for chunk in chunks}),
            source_checksums={chunk.source_id: chunk.checksum for chunk in chunks},
            model_deployment=self.model_deployment,
            verification="citation references resolved",
            caveats=caveats or [],
        )

    def _literature(self, request: ResearchRequest) -> ResearchResult:
        requested_sources = request.context.get("sources")
        sources = [str(source) for source in requested_sources] if isinstance(requested_sources, list) else None
        chunks = (
            []
            if sources == []
            else self.repository.search(
                request.query,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                group_ids=request.group_ids,
                kinds=[SourceKind.PAPER],
                year_from=(int(request.context["date_from"]) if "date_from" in request.context else None),
                year_to=(int(request.context["date_to"]) if "date_to" in request.context else None),
                sources=sources,
                limit=8,
            )
        )
        run = self._new_run(
            Capability.LITERATURE,
            request,
            title="Literature synthesis",
            steps=["Plan review", "Retrieve evidence", "Compare papers", "Verify citations"],
        )
        citations = [self._citation(chunk) for chunk in chunks]
        if not chunks:
            return ResearchResult(
                run=run,
                eyebrow="Evidence synthesis",
                title="No studies matched the review protocol",
                summary=(
                    "The authorized corpus contained no records matching the selected providers and publication window."
                ),
                sections=[],
                citations=[],
                warnings=["Broaden the protocol or ingest matching licensed sources."],
                metrics=[
                    Metric(label="Sources compared", value="0"),
                    Metric(label="Evidence passages", value="0"),
                ],
                metadata={"comparison": []},
                provenance=self._provenance(
                    run,
                    [],
                    caveats=["No evidence matched the protocol filters"],
                ),
            )
        grouped: dict[str, list[EvidenceChunk]] = defaultdict(list)
        for chunk in chunks:
            grouped[chunk.source_id].append(chunk)

        comparison_rows: list[dict[str, Any]] = []
        for source_chunks in grouped.values():
            first = source_chunks[0]
            comparison_rows.append(
                {
                    "title": first.title,
                    "year": first.year,
                    "authors": first.authors,
                    "method": next(
                        (chunk.metadata.get("method") for chunk in source_chunks if chunk.metadata.get("method")),
                        "Not stated in the fixture",
                    ),
                    "evidence": [f"cite-{chunk.id}" for chunk in source_chunks],
                }
            )

        sections = [
            ArtifactSection(
                heading="What the evidence supports",
                body=(
                    "The sample studies converge on two design choices: combine exact and "
                    "semantic retrieval, and make provenance plus verification explicit "
                    "workflow state rather than relying on fluent generation."
                ),
                citation_ids=[citation.id for citation in citations[:3]],
            ),
            ArtifactSection(
                heading="Where the studies disagree or remain incomplete",
                body=(
                    "The evidence evaluates source integrity and workflow auditability, "
                    "but it does not establish long-term researcher adoption or scientific "
                    "correctness across disciplines."
                ),
                citation_ids=[citation.id for citation in citations if "limits" in citation.chunk_id],
                tone="caution",
            ),
            ArtifactSection(
                heading="Recommended next study",
                body=(
                    "Run a discipline-stratified evaluation that reports retrieval recall, "
                    "citation entailment, completeness, abstention, latency, and cost as "
                    "separate outcomes."
                ),
                citation_ids=[citation.id for citation in citations if "eval" in citation.chunk_id],
            ),
        ]
        sections = [section for section in sections if section.citation_ids]
        return ResearchResult(
            run=run,
            eyebrow="Evidence synthesis",
            title="Auditable research synthesis favors hybrid retrieval and explicit verification",
            summary=(
                f"Compared {len(grouped)} synthetic benchmark papers across methods, "
                "findings, and limitations. Every conclusion below resolves to stored evidence."
            ),
            sections=sections,
            citations=citations,
            warnings=[
                "The included papers are synthetic CC0 fixtures for testing; "
                "replace them with licensed primary sources."
            ],
            metrics=[
                Metric(label="Sources compared", value=str(len(grouped))),
                Metric(label="Evidence passages", value=str(len(chunks))),
                Metric(label="Citation integrity", value="100%", detail="All fixture references resolved"),
            ],
            metadata={"comparison": comparison_rows},
            provenance=self._provenance(
                run,
                chunks,
                caveats=["Synthetic corpus; no claim of external scientific validity"],
            ),
        )

    def _grant(self, request: ResearchRequest) -> ResearchResult:
        chunks = self.repository.search(
            request.query,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            group_ids=request.group_ids,
            kinds=[SourceKind.GRANT, SourceKind.TEMPLATE],
            limit=4,
        )
        run = self._new_run(
            Capability.GRANT,
            request,
            title="Grant draft",
            steps=["Extract requirements", "Map supplied facts", "Draft aims", "Check evidence"],
        )
        citations = [self._citation(chunk) for chunk in chunks]
        grant_citations = [item.id for item in citations if item.source_id == "grant-open-science"]
        supplied_facts = request.context.get("project_facts", [])
        warnings = []
        if not supplied_facts:
            warnings.append(
                "No project-specific facts were supplied; placeholders remain and the draft is not ready for review."
            )
        sections = [
            ArtifactSection(
                heading="Requirement matrix",
                body=(
                    "Required: two-page summary, three specific aims, data-management and "
                    "sharing plan, measurable evaluation plan, sustainability section, "
                    "budget justification, and a project period no longer than 36 months."
                ),
                citation_ids=grant_citations,
            ),
            ArtifactSection(
                heading="Specific aims",
                body=(
                    "Aim 1 — establish an evidence-governed research workspace. "
                    "Aim 2 — evaluate grounded workflows with measurable quality and access outcomes. "
                    "Aim 3 — create a sustainable institutional adoption and training model. "
                    "TODO markers must be replaced with verified project facts before review."
                ),
                citation_ids=grant_citations,
            ),
            ArtifactSection(
                heading="Compliance check",
                body=(
                    "The structure covers every requirement in the fixture opportunity. "
                    "Budget values, institutional commitments, preliminary results, and team "
                    "qualifications are intentionally not invented."
                ),
                citation_ids=grant_citations,
                tone="caution",
            ),
        ]
        return ResearchResult(
            run=run,
            eyebrow="Grant studio",
            title="Evidence-bounded specific aims and requirement map",
            summary="A reviewable scaffold was created without fabricating institutional commitments or results.",
            sections=sections,
            citations=citations,
            warnings=warnings,
            metrics=[
                Metric(label="Required sections", value="7"),
                Metric(label="Requirements mapped", value="7"),
                Metric(label="Unsupported facts added", value="0"),
            ],
            metadata={"review_status": "needs_project_facts", "supplied_facts": supplied_facts},
            provenance=self._provenance(run, chunks, caveats=warnings),
        )

    def _matching(self, request: ResearchRequest) -> ResearchResult:
        chunks = self.repository.search(
            request.query,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            group_ids=request.group_ids,
            kinds=[
                SourceKind.PERSON,
                SourceKind.FACILITY,
                SourceKind.EQUIPMENT,
                SourceKind.METHOD,
                SourceKind.TEMPLATE,
            ],
            limit=6,
        )
        run = self._new_run(
            Capability.MATCHING,
            request,
            title="Resource matches",
            steps=["Apply eligibility filters", "Rank records", "Explain matches", "Verify entities"],
        )
        citations = [self._citation(chunk) for chunk in chunks]
        query_terms = self._match_terms(request.query)
        matches: list[MatchItem] = []
        for chunk in chunks:
            tags = [str(tag) for tag in chunk.metadata.get("tags", [])]
            searchable = " ".join([chunk.title, chunk.content, *tags]).lower()
            matched_terms = sorted(term for term in query_terms if term in searchable)
            expertise_match = min(1.0, len(matched_terms) / min(4, len(query_terms))) if query_terms else 0.0
            kind_match = 1.0
            freshness_text = str(chunk.metadata.get("freshness", "")).lower()
            freshness_match = (
                1.0
                if any(token in freshness_text for token in ("2026", "current", "verified"))
                else 0.75
                if "2025" in freshness_text
                else 0.4
            )
            factors = [
                MatchScoreFactor(
                    id="expertise",
                    label="Criteria evidence",
                    weight=0.65,
                    match=expertise_match,
                    contribution=0.65 * expertise_match,
                    evidence_id=f"cite-{chunk.id}",
                ),
                MatchScoreFactor(
                    id="record-type",
                    label="Eligible record type",
                    weight=0.2,
                    match=kind_match,
                    contribution=0.2 * kind_match,
                    evidence_id=f"cite-{chunk.id}",
                ),
                MatchScoreFactor(
                    id="freshness",
                    label="Record freshness",
                    weight=0.15,
                    match=freshness_match,
                    contribution=0.15 * freshness_match,
                    evidence_id=f"cite-{chunk.id}",
                ),
            ]
            score = round(sum(factor.contribution for factor in factors) * 100, 1)
            matches.append(
                MatchItem(
                    id=chunk.source_id,
                    name=str(chunk.metadata.get("name", chunk.title)),
                    kind=chunk.source_kind,
                    score=score,
                    rationale=(
                        f"Matched criteria: {', '.join(matched_terms) or 'eligible record type only'}. "
                        "The score is the weighted sum of stored criteria evidence, "
                        "record eligibility, and freshness."
                    ),
                    citation_ids=[f"cite-{chunk.id}"],
                    freshness=str(chunk.metadata.get("freshness", "Freshness not supplied")),
                    tags=tags,
                    score_factors=factors,
                )
            )
        matches.sort(key=lambda item: (-item.score, item.name.casefold()))
        return ResearchResult(
            run=run,
            eyebrow="Collaboration and resources",
            title="Transparent matches for expertise, facilities, equipment, and templates",
            summary=f"Ranked {len(matches)} verified sample records and preserved the factors behind each match.",
            sections=[
                ArtifactSection(
                    heading="How ranking works",
                    body=(
                        "Eligibility and typed facets are applied before semantic relevance. "
                        "The model may explain stored score factors but cannot create a person, "
                        "facility, capability, or contact detail."
                    ),
                    citation_ids=[citation.id for citation in citations],
                )
            ],
            citations=citations,
            matches=matches,
            metrics=[
                Metric(label="Verified matches", value=str(len(matches))),
                Metric(label="Fabricated entities", value="0"),
            ],
            provenance=self._provenance(run, chunks),
        )

    def _dataset(self, request: ResearchRequest) -> ResearchResult:
        filename = str(request.context.get("filename", "sample-outcomes.csv"))
        sample_names = {"sample-outcomes.csv", "pilot-outcomes.csv"}
        if "csv_text" not in request.context and filename not in sample_names:
            estimated_bytes = int(request.context.get("estimated_bytes", 0))
            run = self._new_run(
                Capability.DATASET,
                request,
                title="Dataset processing estimate",
                steps=[
                    "Resolve asset",
                    "Estimate compute",
                    "Wait for approval",
                ],
            )
            run.status = RunStatus.WAITING_FOR_APPROVAL if estimated_bytes > 5_000_000 else RunStatus.BLOCKED
            run.progress = 15
            run.current_step = "Wait for approval" if run.status == RunStatus.WAITING_FOR_APPROVAL else "Resolve asset"
            return ResearchResult(
                run=run,
                eyebrow="Dataset studio",
                title="The selected asset has not been profiled",
                summary=("No raw data was read and no schema, quality, statistical, or causal result was calculated."),
                sections=[],
                citations=[],
                warnings=[
                    "Profile fields are unavailable until compute completes.",
                    (
                        "Resolve the governed asset, estimate cost and duration, "
                        "obtain approval when required, submit with an "
                        "idempotency key, and collect verified outputs before "
                        "interpretation."
                    ),
                ],
                metrics=[
                    Metric(
                        label="Estimated bytes",
                        value=f"{estimated_bytes:,}",
                    ),
                    Metric(label="Rows computed", value="0"),
                ],
                metadata={
                    "profile": None,
                    "external_compute": {
                        "configured": False,
                        "estimated_bytes": estimated_bytes,
                    },
                },
                provenance=self._provenance(
                    run,
                    [],
                    caveats=["No raw dataset processing occurred"],
                ),
            )

        csv_text = str(request.context.get("csv_text", SAMPLE_CSV))
        profile = profile_csv(csv_text)
        run = self._new_run(
            Capability.DATASET,
            request,
            title="Dataset profile",
            steps=["Validate input", "Compute profile", "Summarize metrics", "Record lineage"],
        )
        checksum = sha256(csv_text.encode()).hexdigest()
        dataset_chunk = EvidenceChunk(
            id="dataset-profile",
            source_id="dataset-upload",
            source_kind=SourceKind.DATASET,
            title=filename,
            content=(
                f"Deterministic profile: {profile['rows']} rows, {profile['columns']} columns. "
                "Column statistics were computed with Polars before narrative generation."
            ),
            section="Computed profile",
            canonical_url=None,
            checksum=checksum,
            metadata={"profile": profile},
        )
        citation = self._citation(dataset_chunk)
        numeric_columns = [item for item in profile["column_profiles"] if item.get("mean") is not None]
        metrics = [
            Metric(label="Rows", value=f"{profile['rows']:,}"),
            Metric(label="Columns", value=f"{profile['columns']:,}"),
            Metric(
                label="Numeric measures",
                value=str(len(numeric_columns)),
                detail="Computed before model summarization",
            ),
        ]
        return ResearchResult(
            run=run,
            eyebrow="Dataset studio",
            title="Deterministic profile with an evidence-bounded interpretation",
            summary=(
                "The sample contains two participant groups and complete score/time fields. "
                "This descriptive profile does not establish statistical significance or causality."
            ),
            sections=[
                ArtifactSection(
                    heading="Computed structure and quality",
                    body=(
                        f"The file contains {profile['rows']} rows and {profile['columns']} columns. "
                        "Null counts, unique counts, ranges, and means are available per column."
                    ),
                    citation_ids=[citation.id],
                ),
                ArtifactSection(
                    heading="Interpretation boundary",
                    body=(
                        "The intervention rows have higher observed scores and lower observed "
                        "completion times in this tiny fixture. No inferential test, randomization "
                        "check, or causal claim has been performed."
                    ),
                    citation_ids=[citation.id],
                    tone="caution",
                ),
                ArtifactSection(
                    heading="Large-scale processing",
                    body=(
                        "Files beyond the inline POC limit require estimate, approval, submit, "
                        "poll, and collect through a configured external compute adapter."
                    ),
                    citation_ids=[citation.id],
                ),
            ],
            citations=[citation],
            metrics=metrics,
            metadata={"profile": profile, "external_compute": {"configured": False}},
            provenance=self._provenance(run, [dataset_chunk]),
        )

    def _institutional_qa(self, request: ResearchRequest) -> ResearchResult:
        chunks = self.repository.search(
            request.query,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            group_ids=request.group_ids,
            kinds=[SourceKind.POLICY],
            limit=4,
        )
        relevant = [chunk for chunk in chunks if self._query_overlap(request.query, chunk.content)]
        run = self._new_run(
            Capability.INSTITUTIONAL_QA,
            request,
            title="Institutional answer",
            steps=["Apply ACL", "Retrieve policy", "Check versions", "Ground answer"],
        )
        if not relevant:
            return ResearchResult(
                run=run,
                eyebrow="Institutional guidance",
                title="The sample corpus does not support an answer",
                summary=(
                    "No sufficiently relevant authorized policy passage was found. "
                    "The assistant abstained rather than generating a policy answer."
                ),
                sections=[],
                citations=[],
                warnings=["Ask an institutional owner or add the governing document."],
                provenance=self._provenance(
                    run,
                    [],
                    caveats=["Abstained because retrieval did not meet the relevance threshold"],
                ),
            )
        citations = [self._citation(chunk) for chunk in relevant]
        answer = (
            "The synthetic IRB guidance requires disclosure when generative AI "
            "processes identifiable participant data or materially influences "
            "eligibility, risk, intervention, or outcome decisions."
            if any(chunk.source_id == "policy-irb" for chunk in relevant)
            else (
                "The synthetic policy requires final research records to be retained "
                "for seven years after project close, subject to longer sponsor or legal requirements."
            )
        )
        return ResearchResult(
            run=run,
            eyebrow="Institutional guidance",
            title="Grounded answer with policy version context",
            summary=answer,
            sections=[
                ArtifactSection(
                    heading="Policy basis",
                    body=answer,
                    citation_ids=[citation.id for citation in citations],
                ),
                ArtifactSection(
                    heading="Scope",
                    body=(
                        "This answer reflects only the authorized synthetic corpus and is not "
                        "legal, compliance, or IRB approval."
                    ),
                    citation_ids=[citation.id for citation in citations],
                    tone="caution",
                ),
            ],
            citations=citations,
            metrics=[
                Metric(label="Authorized sources", value=str(len({item.source_id for item in citations}))),
                Metric(label="Policy versions", value=str(len(relevant))),
            ],
            provenance=self._provenance(run, relevant),
        )

    def _orchestration(self, request: ResearchRequest) -> ResearchResult:
        chunks = self.repository.search(
            request.query,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            group_ids=request.group_ids,
            kinds=[SourceKind.PAPER, SourceKind.POLICY],
            limit=3,
        )
        run = self._new_run(
            Capability.ORCHESTRATION,
            request,
            title="Research workflow plan",
            steps=[
                "Ingest and verify",
                "Retrieve in parallel",
                "Synthesize",
                "Wait for review",
                "Export artifacts",
            ],
        )
        citations = [self._citation(chunk) for chunk in chunks]
        pipeline = [
            {"id": "ingest", "label": "Ingest and verify sources", "state": "completed"},
            {"id": "retrieve", "label": "Retrieve authorized evidence", "state": "completed"},
            {"id": "synthesize", "label": "Synthesize cited artifact", "state": "completed"},
            {"id": "approval", "label": "Human review", "state": "waiting_for_approval"},
            {"id": "export", "label": "Export approved artifact", "state": "blocked"},
        ]
        run.status = RunStatus.WAITING_FOR_APPROVAL
        run.progress = 80
        run.current_step = "Human review"
        return ResearchResult(
            run=run,
            eyebrow="Durable workflow",
            title="A resumable evidence workflow is ready for review",
            summary=(
                "The workflow externalizes state, retries bounded activities, and blocks "
                "export or paid compute until an explicit approval event is recorded."
            ),
            sections=[
                ArtifactSection(
                    heading="Execution policy",
                    body=(
                        "Large payloads remain in Blob Storage and activities exchange immutable "
                        "references. Each external action uses an idempotency key and bounded retry."
                    ),
                    citation_ids=[citation.id for citation in citations],
                )
            ],
            citations=citations,
            warnings=["Export remains blocked until a reviewer approves this run."],
            metrics=[
                Metric(label="Completed steps", value="3 / 5"),
                Metric(label="Approval gates", value="1"),
                Metric(label="External writes", value="0"),
            ],
            metadata={"pipeline": pipeline, "runtime": "durable-task-compatible"},
            provenance=self._provenance(run, chunks),
        )

    @staticmethod
    def _query_overlap(query: str, content: str) -> bool:
        stop = {"what", "when", "where", "which", "with", "from", "must", "does", "this", "that"}
        query_terms = {
            token.strip(".,?!").lower()
            for token in query.split()
            if len(token.strip(".,?!")) > 3 and token.strip(".,?!").lower() not in stop
        }
        content_lower = content.lower()
        return any(term in content_lower for term in query_terms)

    @staticmethod
    def _match_terms(query: str) -> set[str]:
        stop = {
            "find",
            "with",
            "that",
            "this",
            "from",
            "into",
            "need",
            "resource",
            "resources",
            "expert",
            "experts",
        }
        return {
            token for raw in query.split() if len(token := raw.strip(".,?!:;()[]").lower()) > 3 and token not in stop
        }

    @staticmethod
    def _validate_result(result: ResearchResult) -> None:
        citation_ids = {citation.id for citation in result.citations}
        for section in result.sections:
            missing = set(section.citation_ids).difference(citation_ids)
            if missing:
                raise CitationValidationError(
                    f"Section '{section.heading}' references unknown citations: {sorted(missing)}"
                )
            if section.body and not section.citation_ids:
                raise CitationValidationError(f"Section '{section.heading}' contains an uncited claim")
