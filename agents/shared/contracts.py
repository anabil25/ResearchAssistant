from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .capabilities import CapabilityBinding
from .errors import ContractError


class Sensitivity(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class SupportStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONFLICTING = "conflicting"


class EvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    evidence_id: str = Field(min_length=1, max_length=256)
    source_uri: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=512)
    version: str | None = Field(default=None, max_length=128)


class Claim(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1, max_length=20_000)
    support: SupportStatus
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def evidence_matches_support(self) -> Claim:
        if self.support in {SupportStatus.SUPPORTED, SupportStatus.CONFLICTING} and not self.evidence_ids:
            raise ValueError("supported and conflicting claims require at least one evidence_id")
        if self.support == SupportStatus.UNSUPPORTED and self.evidence_ids:
            raise ValueError("unsupported claims cannot cite evidence")
        return self


class ResearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=40_000)
    tenant_id: str = Field(min_length=1, max_length=128)
    principal_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    sensitivity: Sensitivity
    evidence: tuple[EvidenceRef, ...] = ()


class ResearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(min_length=1, max_length=40_000)
    claims: tuple[Claim, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def citations_resolve(self) -> ResearchResponse:
        evidence_ids = {item.evidence_id for item in self.evidence}
        normalized = tuple(
            (
                claim.model_copy(
                    update={
                        "support": SupportStatus.UNSUPPORTED,
                        "evidence_ids": (),
                    }
                )
                if set(claim.evidence_ids) - evidence_ids
                else claim
            )
            for claim in self.claims
        )
        if normalized != self.claims:
            object.__setattr__(self, "claims", normalized)
        return self


class LiteratureRequest(ResearchRequest):
    sensitivity: Literal[
        Sensitivity.INTERNAL,
        Sensitivity.CONFIDENTIAL,
        Sensitivity.RESTRICTED,
    ]
    review_question: str | None = Field(default=None, max_length=8_000)


class LiteratureResponse(ResearchResponse):
    consensus: tuple[str, ...] = ()
    disagreements: tuple[str, ...] = ()


def _reject_public_evidence(
    evidence: tuple[EvidenceRef, ...],
) -> tuple[EvidenceRef, ...]:
    if evidence:
        raise ValueError("public requests cannot include caller-supplied evidence")
    return evidence


class PublicLiteratureRequest(ResearchRequest):
    sensitivity: Literal[Sensitivity.PUBLIC] = Sensitivity.PUBLIC
    review_question: str | None = Field(default=None, max_length=8_000)
    _public_evidence_boundary = field_validator("evidence")(_reject_public_evidence)


class PublicLiteratureResponse(LiteratureResponse):
    search_urls: tuple[str, ...] = ()


class GrantRequest(ResearchRequest):
    sensitivity: Literal[
        Sensitivity.INTERNAL,
        Sensitivity.CONFIDENTIAL,
        Sensitivity.RESTRICTED,
    ]
    opportunity_id: str | None = Field(default=None, max_length=256)


class GrantResponse(ResearchResponse):
    requirements: tuple[str, ...] = ()
    ready_for_review: bool = False


class PublicGrantRequest(ResearchRequest):
    sensitivity: Literal[Sensitivity.PUBLIC] = Sensitivity.PUBLIC
    opportunity_id: str | None = Field(default=None, max_length=256)
    _public_evidence_boundary = field_validator("evidence")(_reject_public_evidence)


class PublicGrantResponse(GrantResponse):
    opportunity_urls: tuple[str, ...] = ()


class MatchingRequest(ResearchRequest):
    sensitivity: Literal[
        Sensitivity.INTERNAL,
        Sensitivity.CONFIDENTIAL,
        Sensitivity.RESTRICTED,
    ]
    required_facets: tuple[str, ...] = ()


class MatchingResponse(ResearchResponse):
    record_ids: tuple[str, ...] = ()


class PublicMatchingRequest(ResearchRequest):
    sensitivity: Literal[Sensitivity.PUBLIC] = Sensitivity.PUBLIC
    required_facets: tuple[str, ...] = ()
    _public_evidence_boundary = field_validator("evidence")(_reject_public_evidence)


class PublicMatchingResponse(MatchingResponse):
    lead_record_ids: tuple[str, ...] = ()


class DatasetRequest(ResearchRequest):
    sensitivity: Literal[
        Sensitivity.INTERNAL,
        Sensitivity.CONFIDENTIAL,
        Sensitivity.RESTRICTED,
    ]
    dataset_id: str = Field(min_length=1, max_length=256)
    approved_compute: bool = False
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def approved_compute_has_stable_key(self) -> DatasetRequest:
        if self.approved_compute and self.idempotency_key is None:
            raise ValueError("approved compute requires a stable idempotency key")
        return self


class DatasetResponse(ResearchResponse):
    code: str | None = None
    computed_outputs: dict[str, str | int | float | bool | None] = {}


class InstitutionRequest(ResearchRequest):
    sensitivity: Literal[
        Sensitivity.INTERNAL,
        Sensitivity.CONFIDENTIAL,
        Sensitivity.RESTRICTED,
    ]
    policy_scope: str | None = Field(default=None, max_length=512)


class InstitutionResponse(ResearchResponse):
    effective_dates: tuple[str, ...] = ()


class SpecialistCapability(StrEnum):
    LITERATURE = "literature"
    GRANT = "grant"
    MATCHING = "matching"
    DATASET = "dataset"
    INSTITUTION = "institutional_qa"


class CoordinatorRequest(ResearchRequest):
    requested_capabilities: tuple[SpecialistCapability, ...] = Field(min_length=1)
    specialist_inputs: dict[
        SpecialistCapability,
        dict[str, str | int | float | bool | list[str] | None],
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def capabilities_are_unique(self) -> CoordinatorRequest:
        if len(self.requested_capabilities) != len(set(self.requested_capabilities)):
            raise ValueError("requested specialist capabilities must be unique")
        if set(self.specialist_inputs) - set(self.requested_capabilities):
            raise ValueError("specialist inputs must target a requested capability")
        return self


type SpecialistRequestPayload = (
    PublicLiteratureRequest
    | LiteratureRequest
    | PublicGrantRequest
    | GrantRequest
    | PublicMatchingRequest
    | MatchingRequest
    | DatasetRequest
    | InstitutionRequest
)


class SpecialistRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1, max_length=256)
    capability: SpecialistCapability
    request: SpecialistRequestPayload
    target_agent: str

    @model_validator(mode="before")
    @classmethod
    def parse_pinned_request_contract(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        request = value.get("request")
        if not isinstance(request, dict):
            return value
        raw_capability = value.get("capability")
        raw_sensitivity = request.get("sensitivity")
        if not isinstance(raw_capability, str) or not isinstance(raw_sensitivity, str):
            return value
        try:
            capability = SpecialistCapability(raw_capability)
            sensitivity = Sensitivity(raw_sensitivity)
        except ValueError:
            return value
        public = sensitivity == Sensitivity.PUBLIC
        contracts: dict[SpecialistCapability, type[ResearchRequest]] = {
            SpecialistCapability.LITERATURE: (
                PublicLiteratureRequest if public else LiteratureRequest
            ),
            SpecialistCapability.GRANT: PublicGrantRequest if public else GrantRequest,
            SpecialistCapability.MATCHING: (
                PublicMatchingRequest if public else MatchingRequest
            ),
            SpecialistCapability.DATASET: DatasetRequest,
            SpecialistCapability.INSTITUTION: InstitutionRequest,
        }
        parsed = contracts[capability].model_validate(request)
        return {**value, "request": parsed}

    @model_validator(mode="after")
    def capability_matches_request(self) -> SpecialistRequest:
        expected: dict[SpecialistCapability, tuple[type[ResearchRequest], ...]] = {
            SpecialistCapability.LITERATURE: (
                PublicLiteratureRequest,
                LiteratureRequest,
            ),
            SpecialistCapability.GRANT: (PublicGrantRequest, GrantRequest),
            SpecialistCapability.MATCHING: (
                PublicMatchingRequest,
                MatchingRequest,
            ),
            SpecialistCapability.DATASET: (DatasetRequest,),
            SpecialistCapability.INSTITUTION: (InstitutionRequest,),
        }
        if not isinstance(self.request, expected[self.capability]):
            raise ValueError("specialist capability does not match its typed request")
        return self


type SpecialistResponse = (
    PublicLiteratureResponse
    | LiteratureResponse
    | PublicGrantResponse
    | GrantResponse
    | PublicMatchingResponse
    | MatchingResponse
    | DatasetResponse
    | InstitutionResponse
)


_SPECIALIST_RESPONSE_CONTRACTS: dict[
    tuple[SpecialistCapability, str],
    type[ResearchResponse],
] = {
    (SpecialistCapability.LITERATURE, "literature-agent"): LiteratureResponse,
    (
        SpecialistCapability.LITERATURE,
        "literature-online-agent",
    ): PublicLiteratureResponse,
    (SpecialistCapability.GRANT, "grant-agent"): GrantResponse,
    (SpecialistCapability.GRANT, "grant-online-agent"): PublicGrantResponse,
    (SpecialistCapability.MATCHING, "matching-agent"): MatchingResponse,
    (
        SpecialistCapability.MATCHING,
        "matching-online-agent",
    ): PublicMatchingResponse,
    (SpecialistCapability.DATASET, "dataset-agent"): DatasetResponse,
    (SpecialistCapability.INSTITUTION, "institution-agent"): InstitutionResponse,
}


class SpecialistResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str
    capability: SpecialistCapability
    agent_name: str
    response: SpecialistResponse | None = None
    error_code: str | None = None

    @model_validator(mode="before")
    @classmethod
    def parse_pinned_response_contract(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        raw_capability = value.get("capability")
        agent_name = value.get("agent_name")
        if not isinstance(raw_capability, str) or not isinstance(agent_name, str):
            return value
        try:
            capability = SpecialistCapability(raw_capability)
            response_contract = _SPECIALIST_RESPONSE_CONTRACTS[
                (capability, agent_name)
            ]
        except (KeyError, ValueError) as exc:
            raise ValueError(
                "specialist result does not match a pinned capability and agent identity"
            ) from exc
        response = value.get("response")
        if isinstance(response, dict):
            return {
                **value,
                "response": response_contract.model_validate(response),
            }
        return value

    @model_validator(mode="after")
    def exactly_one_result(self) -> SpecialistResult:
        response_contract = _SPECIALIST_RESPONSE_CONTRACTS.get(
            (self.capability, self.agent_name)
        )
        if response_contract is None:
            raise ValueError(
                "specialist result does not match a pinned capability and agent identity"
            )
        if self.response is not None and type(self.response) is not response_contract:
            raise ValueError(
                "specialist response does not match its pinned output contract"
            )
        if (self.response is None) == (self.error_code is None):
            raise ValueError("specialist result requires exactly one of response or error_code")
        return self


class CoordinatorResponse(ResearchResponse):
    specialist_results: tuple[SpecialistResult, ...] = ()


def resolve_authorized_evidence(
    response: ResearchResponse,
    authorized_evidence: tuple[EvidenceRef, ...],
) -> ResearchResponse:
    authorized_by_id = {item.evidence_id: item for item in authorized_evidence}
    authorized_evidence_ids = frozenset(authorized_by_id)
    output_ids = {item.evidence_id for item in response.evidence} | {
        evidence_id for claim in response.claims for evidence_id in claim.evidence_ids
    }
    evidence = tuple(item for item in authorized_evidence if item.evidence_id in output_ids)
    claims = tuple(
        (
            claim.model_copy(
                update={
                    "support": SupportStatus.UNSUPPORTED,
                    "evidence_ids": (),
                }
            )
            if (
                claim.support in {SupportStatus.SUPPORTED, SupportStatus.CONFLICTING}
                and (
                    not claim.evidence_ids
                    or bool(set(claim.evidence_ids) - authorized_evidence_ids)
                )
            )
            else claim
        )
        for claim in response.claims
    )
    updates: dict[str, object] = {"claims": claims, "evidence": evidence}
    if isinstance(response, CoordinatorResponse):
        updates["specialist_results"] = tuple(
            (
                item.model_copy(
                    update={
                        "response": resolve_authorized_evidence(
                            item.response,
                            authorized_evidence,
                        )
                    }
                )
                if item.response is not None
                else item
            )
            for item in response.specialist_results
        )
    return response.model_copy(update=updates)


INPUT_CONTRACTS: dict[str, type[ResearchRequest]] = {
    "CoordinatorRequestV2": CoordinatorRequest,
    "LiteratureRequestV2": LiteratureRequest,
    "PublicLiteratureRequestV2": PublicLiteratureRequest,
    "GrantRequestV2": GrantRequest,
    "PublicGrantRequestV2": PublicGrantRequest,
    "MatchingRequestV2": MatchingRequest,
    "PublicMatchingRequestV2": PublicMatchingRequest,
    "DatasetRequestV2": DatasetRequest,
    "InstitutionRequestV2": InstitutionRequest,
}

OUTPUT_CONTRACTS: dict[str, type[ResearchResponse]] = {
    "CoordinatorDecisionV2": CoordinatorResponse,
    "LiteratureSynthesisV2": LiteratureResponse,
    "PublicLiteratureResearchV2": PublicLiteratureResponse,
    "GrantPackageV2": GrantResponse,
    "PublicGrantResearchV2": PublicGrantResponse,
    "MatchingShortlistV2": MatchingResponse,
    "PublicMatchingResearchV2": PublicMatchingResponse,
    "DatasetAnalysisV2": DatasetResponse,
    "InstitutionalAnswerV2": InstitutionResponse,
}

AUXILIARY_CONTRACTS: dict[str, type[BaseModel]] = {
    "EvidenceRefV2": EvidenceRef,
    "SpecialistRequestV2": SpecialistRequest,
    "SpecialistResultV2": SpecialistResult,
}


class SchemaReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: str = Field(min_length=1, max_length=128)
    uri: str = Field(pattern=r"^urn:research-assistant:schema:[A-Za-z0-9._-]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _schema_reference(schema_id: str, model: type[BaseModel]) -> SchemaReference:
    return SchemaReference(
        schema_id=schema_id,
        uri=f"urn:research-assistant:schema:{schema_id}",
        sha256=canonical_digest(model.model_json_schema()),
    )


SCHEMA_REFERENCES: dict[str, SchemaReference] = {
    schema_id: _schema_reference(schema_id, model)
    for schema_id, model in {
        **INPUT_CONTRACTS,
        **OUTPUT_CONTRACTS,
        **AUXILIARY_CONTRACTS,
    }.items()
}


@dataclass(frozen=True, slots=True)
class AgentContractBinding:
    input_model: type[ResearchRequest]
    output_model: type[ResearchResponse]


def bind_contracts(manifest: AgentManifest) -> AgentContractBinding:
    try:
        input_model = INPUT_CONTRACTS[manifest.input_schema.schema_id]
        output_model = OUTPUT_CONTRACTS[manifest.output_schema.schema_id]
    except KeyError as exc:
        raise ContractError("Agent manifest references an unknown contract schema") from exc
    expected_input = _schema_reference(manifest.input_schema.schema_id, input_model)
    expected_output = _schema_reference(manifest.output_schema.schema_id, output_model)
    if manifest.input_schema != expected_input or manifest.output_schema != expected_output:
        raise ContractError("Agent manifest contract schema digest does not match runtime binding")
    return AgentContractBinding(input_model=input_model, output_model=output_model)


class MemoryScope(StrEnum):
    CONVERSATION = "conversation"
    USER = "user"
    PROJECT = "project"
    PRIVATE_AGENT = "private_agent"


class ModelPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_deployment_ref: str = Field(pattern=r"^foundry://project/deployments/[A-Za-z0-9._-]+$")
    pinned_model_version: str = Field(min_length=1, max_length=128)
    provider: Literal["microsoft_foundry"] = "microsoft_foundry"
    api: Literal["responses"] = "responses"
    minimum_context_tokens: int = Field(default=128_000, ge=8_000)
    structured_outputs_required: bool = True
    tool_calling_required: bool = False
    performance_class: Literal["fast", "primary"]

    @property
    def deployment_name(self) -> str:
        return self.selected_deployment_ref.rsplit("/", 1)[-1]


class RuntimeKind(StrEnum):
    MANAGED = "managed"
    CUSTOM = "custom"


class RuntimeRequirements(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    custom_middleware: bool = True
    custom_code: bool = True
    workflow_checkpointing: bool = False
    private_in_process_specialists: bool = False
    session_files: bool = False
    protocols: tuple[Literal["responses"], ...] = ("responses",)
    responses_protocol_version: Literal["2.0.0"] = "2.0.0"

    @property
    def selected_runtime(self) -> RuntimeKind:
        if (
            self.custom_middleware
            or self.custom_code
            or self.workflow_checkpointing
            or self.private_in_process_specialists
            or self.session_files
        ):
            return RuntimeKind.CUSTOM
        return RuntimeKind.MANAGED


class KnowledgeBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    binding_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    kind: Literal["authorized_evidence", "foundry_toolbox"]
    source_ref: str = Field(min_length=1, max_length=512)
    connection_ref: str = Field(min_length=1, max_length=512)
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    pinned_version: str = Field(min_length=1, max_length=128)
    access: Literal["public", "internal"]
    sources: tuple[str, ...] = ()


class EvidencePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id_resolution_required: Literal[True] = True
    unresolved_claim_behavior: Literal["mark_unsupported"] = "mark_unsupported"
    allowed_evidence_kinds: tuple[str, ...] = ()
    citation_schema: SchemaReference
    output_schema: SchemaReference


class PinnedSpecialist(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: SpecialistCapability
    sensitivity: Sensitivity
    registry_agent_ref: str = Field(pattern=r"^foundry://project/agents/[A-Za-z0-9._-]+$")
    agent_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    private_specialist_ref: str | None = Field(default=None, max_length=512)
    request_schema: SchemaReference
    response_schema: SchemaReference

    @property
    def agent_name(self) -> str:
        return self.registry_agent_ref.rsplit("/", 1)[-1]


class SpecialistPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    specialists: tuple[PinnedSpecialist, ...]
    delegation_mode: Literal["parallel_deterministic"] = "parallel_deterministic"
    shared_memory_scopes: tuple[MemoryScope, ...] = ()
    memory_writeback: Literal[False] = False
    max_turns: int = Field(default=1, ge=1, le=5)
    max_parallelism: int = Field(default=3, ge=1, le=16)
    deadline_seconds: float = Field(default=120, gt=0, le=600)
    budget_units: int = Field(default=5, ge=1, le=32)
    fallback: Literal["fail_closed"] = "fail_closed"

    @model_validator(mode="after")
    def specialists_are_unique(self) -> SpecialistPolicy:
        keys = [(specialist.capability, specialist.sensitivity) for specialist in self.specialists]
        if len(keys) != len(set(keys)):
            raise ValueError("specialist capability and sensitivity bindings must be unique")
        return self


class ArtifactPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    output_schema: SchemaReference
    template_ref: str | None = Field(default=None, max_length=512)
    forked_from_manifest_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    provenance_required: Literal[True] = True


class MemoryScopePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: MemoryScope
    enabled: bool = False
    persistent: bool = False
    provider_ref: str | None = Field(default=None, pattern=r"^app://[A-Za-z0-9._/-]+$")
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    ttl_seconds: int | None = Field(default=None, ge=60, le=315_360_000)
    read_roles: tuple[str, ...] = ()
    write_roles: tuple[str, ...] = ()
    user_can_inspect: bool = True
    user_can_correct: bool = True
    user_can_forget: bool = True
    user_can_export: bool = True
    provenance_required: Literal[True] = True

    @model_validator(mode="after")
    def persistence_is_application_owned(self) -> MemoryScopePolicy:
        if self.persistent and (not self.enabled or self.provider_ref is None):
            raise ValueError("persistent memory requires an enabled application-owned provider")
        if self.persistent and (
            self.retention_days is None or self.ttl_seconds is None or not self.read_roles or not self.write_roles
        ):
            raise ValueError("persistent memory requires retention, TTL, and ACL policy")
        if not self.persistent and (self.retention_days is not None or self.ttl_seconds is not None):
            raise ValueError("non-persistent memory cannot declare retention or TTL")
        return self


def _default_memory_scopes() -> tuple[MemoryScopePolicy, ...]:
    return (
        MemoryScopePolicy(scope=MemoryScope.CONVERSATION, enabled=True),
        MemoryScopePolicy(scope=MemoryScope.USER),
        MemoryScopePolicy(scope=MemoryScope.PROJECT),
        MemoryScopePolicy(scope=MemoryScope.PRIVATE_AGENT),
    )


class MemoryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    scopes: tuple[MemoryScopePolicy, ...] = Field(default_factory=_default_memory_scopes)
    max_records: int = Field(default=100, ge=1, le=10_000)
    allowed_sensitivities: tuple[Sensitivity, ...] = (
        Sensitivity.PUBLIC,
        Sensitivity.INTERNAL,
    )

    @model_validator(mode="after")
    def scopes_are_complete(self) -> MemoryPolicy:
        configured = [item.scope for item in self.scopes]
        if len(configured) != len(set(configured)):
            raise ValueError("memory scopes must be unique")
        if set(configured) != set(MemoryScope):
            raise ValueError("memory policy must explicitly define every supported scope")
        return self

    def for_scope(self, scope: MemoryScope) -> MemoryScopePolicy:
        return next(item for item in self.scopes if item.scope == scope)


class ObjectiveGate(StrEnum):
    MANIFEST_SCHEMA = "manifest_schema"
    BUILD = "build"
    TEST = "test"
    AUTHORIZATION_POLICY = "authorization_policy"
    SECURITY = "security"
    SMOKE = "smoke"
    EVIDENCE_POLICY = "evidence_policy"


_REQUIRED_OBJECTIVE_GATES = tuple(ObjectiveGate)


class EvaluationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    suite: str
    minimum_score: float = Field(default=0.8, ge=0, le=1)
    evaluator_results_advisory: Literal[True] = True
    objective_hard_gates: tuple[ObjectiveGate, ...] = _REQUIRED_OBJECTIVE_GATES

    @model_validator(mode="after")
    def objective_gates_are_complete(self) -> EvaluationPolicy:
        if set(self.objective_hard_gates) != set(_REQUIRED_OBJECTIVE_GATES):
            raise ValueError("all objective release gates are required")
        return self


class AgentManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    name: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
    behavior_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    parent_manifest_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    description: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    input_schema: SchemaReference
    output_schema: SchemaReference
    capability_bindings: tuple[CapabilityBinding, ...] = ()
    model_policy: ModelPolicy
    runtime_requirements: RuntimeRequirements
    knowledge_bindings: tuple[KnowledgeBinding, ...]
    evidence_policy: EvidencePolicy
    specialist_policy: SpecialistPolicy | None = None
    artifact_policy: ArtifactPolicy
    workflow_steps: tuple[str, ...] = Field(min_length=1)
    memory: MemoryPolicy
    evaluation: EvaluationPolicy

    @model_validator(mode="after")
    def manifest_policy(self) -> AgentManifest:
        capability_operations = [
            (binding.capability_id, binding.operation_id)
            for binding in self.capability_bindings
        ]
        if len(capability_operations) != len(set(capability_operations)):
            raise ValueError("capability bindings must be unique")
        if self.online and "public" not in self.instructions.lower():
            raise ValueError("online manifests must state their public-data boundary")
        if self.specialist_policy is not None and self.id != "coordinator":
            raise ValueError("specialist policy is only valid for the coordinator")
        if (
            self.artifact_policy.output_schema != self.output_schema
            or self.evidence_policy.output_schema != self.output_schema
        ):
            raise ValueError("artifact and evidence output schemas must match the agent output")
        return self

    @property
    def version(self) -> str:
        return self.behavior_version

    @property
    def input_contract(self) -> str:
        return self.input_schema.schema_id

    @property
    def output_contract(self) -> str:
        return self.output_schema.schema_id

    @property
    def capability_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(binding.capability_id for binding in self.capability_bindings))

    @property
    def model_tier(self) -> Literal["fast", "primary"]:
        return self.model_policy.performance_class

    @property
    def evidence_kinds(self) -> tuple[str, ...]:
        return self.evidence_policy.allowed_evidence_kinds

    @property
    def online(self) -> bool:
        return any(binding.access == "public" for binding in self.knowledge_bindings)

    @property
    def connector_sources(self) -> tuple[str, ...]:
        return tuple(source for binding in self.knowledge_bindings for source in binding.sources)

    @property
    def enable_web_search(self) -> bool:
        return self.online
