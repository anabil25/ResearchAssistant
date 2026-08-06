from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    create_model,
    field_validator,
    model_validator,
)

from .capabilities import CapabilityBinding, template_instance_fingerprint
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


class LenientClaim(Claim):
    """Parse-stage claim that tolerates model output violating the evidence invariant.

    Generated output is schema-conformant but cannot be constrained by the model
    provider across fields, so a model may emit ``supported`` with no evidence.
    Enforcing the invariant at parse time aborts the whole response; instead this
    twin lets the payload land so ``resolve_authorized_evidence`` can downgrade the
    claim to ``unsupported``. The strict invariant is re-applied afterwards.
    """

    @model_validator(mode="after")
    def evidence_matches_support(self) -> LenientClaim:
        return self


class ResearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=40_000)
    tenant_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=256)
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
    search_urls: tuple[str, ...] = ()


def _reject_public_evidence(
    evidence: tuple[EvidenceRef, ...],
) -> tuple[EvidenceRef, ...]:
    if evidence:
        raise ValueError("public requests cannot include caller-supplied evidence")
    return evidence


class PublicLiteratureRequest(ResearchRequest):
    sensitivity: Literal[Sensitivity.PUBLIC] = Sensitivity.PUBLIC
    review_question: str | None = Field(default=None, max_length=8_000)
    authorized_connector_ids: tuple[str, ...] = ()
    public_context: str | None = Field(default=None, max_length=40_000)
    _public_evidence_boundary = field_validator("evidence")(_reject_public_evidence)

    @field_validator("authorized_connector_ids")
    @classmethod
    def authorized_connectors_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("authorized connector identifiers must be unique")
        return value


class PublicLiteratureResponse(LiteratureResponse):
    pass


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
    opportunity_urls: tuple[str, ...] = ()


class PublicGrantRequest(ResearchRequest):
    sensitivity: Literal[Sensitivity.PUBLIC] = Sensitivity.PUBLIC
    opportunity_id: str | None = Field(default=None, max_length=256)
    authorized_connector_ids: tuple[str, ...] = ()
    public_context: str | None = Field(default=None, max_length=40_000)
    _public_evidence_boundary = field_validator("evidence")(_reject_public_evidence)

    @field_validator("authorized_connector_ids")
    @classmethod
    def authorized_connectors_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("authorized connector identifiers must be unique")
        return value


class PublicGrantResponse(GrantResponse):
    pass


class MatchingRequest(ResearchRequest):
    sensitivity: Literal[
        Sensitivity.INTERNAL,
        Sensitivity.CONFIDENTIAL,
        Sensitivity.RESTRICTED,
    ]
    required_facets: tuple[str, ...] = ()


class MatchingResponse(ResearchResponse):
    record_ids: tuple[str, ...] = ()
    lead_record_ids: tuple[str, ...] = ()


class PublicMatchingRequest(ResearchRequest):
    sensitivity: Literal[Sensitivity.PUBLIC] = Sensitivity.PUBLIC
    required_facets: tuple[str, ...] = ()
    authorized_connector_ids: tuple[str, ...] = ()
    public_context: str | None = Field(default=None, max_length=40_000)
    _public_evidence_boundary = field_validator("evidence")(_reject_public_evidence)

    @field_validator("authorized_connector_ids")
    @classmethod
    def authorized_connectors_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("authorized connector identifiers must be unique")
        return value


class PublicMatchingResponse(MatchingResponse):
    pass


class DatasetRequest(ResearchRequest):
    sensitivity: Literal[
        Sensitivity.INTERNAL,
        Sensitivity.CONFIDENTIAL,
        Sensitivity.RESTRICTED,
    ]
    dataset_id: str = Field(min_length=1, max_length=256)
    approved_compute: Literal[False] = Field(
        default=False,
        description=(
            "Deprecated non-authoritative compatibility field; only an exact-bound "
            "approval_decision_id can authorize compute."
        ),
    )
    approval_decision_id: str | None = Field(default=None, min_length=1, max_length=512)
    invocation_id: str | None = Field(default=None, min_length=1, max_length=512)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def approval_context_is_complete(self) -> DatasetRequest:
        supplied = (
            self.approval_decision_id is not None,
            self.invocation_id is not None,
            self.idempotency_key is not None,
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "approval, invocation, and idempotency references must be supplied together"
            )
        return self


class ComputedOutput(BaseModel):
    """Named dataset result value.

    Modelled as a list of named entries rather than an open map because strict
    structured output schemas reject objects with free-form keys.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=256)
    value: str = Field(default="", max_length=8_000)


class DatasetResponse(ResearchResponse):
    code: str | None = None
    computed_outputs: tuple[ComputedOutput, ...] = ()


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
            SpecialistCapability.LITERATURE: (PublicLiteratureRequest if public else LiteratureRequest),
            SpecialistCapability.GRANT: PublicGrantRequest if public else GrantRequest,
            SpecialistCapability.MATCHING: (PublicMatchingRequest if public else MatchingRequest),
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
    (SpecialistCapability.GRANT, "grant-agent"): GrantResponse,
    (SpecialistCapability.MATCHING, "matching-agent"): MatchingResponse,
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
            response_contract = _SPECIALIST_RESPONSE_CONTRACTS[(capability, agent_name)]
        except (KeyError, ValueError) as exc:
            raise ValueError("specialist result does not match a pinned capability and agent identity") from exc
        response = value.get("response")
        if isinstance(response, dict):
            return {
                **value,
                "response": response_contract.model_validate(response),
            }
        return value

    @model_validator(mode="after")
    def exactly_one_result(self) -> SpecialistResult:
        response_contract = _SPECIALIST_RESPONSE_CONTRACTS.get((self.capability, self.agent_name))
        if response_contract is None:
            raise ValueError("specialist result does not match a pinned capability and agent identity")
        if self.response is not None and not isinstance(self.response, response_contract):
            raise ValueError("specialist response does not match its pinned output contract")
        if (self.response is None) == (self.error_code is None):
            raise ValueError("specialist result requires exactly one of response or error_code")
        return self


class CoordinatorResponse(ResearchResponse):
    specialist_results: tuple[SpecialistResult, ...] = ()


_LENIENT_OUTPUT_MODELS: dict[type[ResearchResponse], type[ResearchResponse]] = {}


def lenient_output_model(model: type[ResearchResponse]) -> type[ResearchResponse]:
    """Return the parse-stage twin of a response contract.

    The twin keeps the exact field shape of the governance contract -- so the
    generated JSON schema and the model's instructions are unchanged -- but
    relaxes the cross-field claim invariant that generation cannot guarantee.
    Callers must re-validate against the strict contract after normalization.
    """

    cached = _LENIENT_OUTPUT_MODELS.get(model)
    if cached is not None:
        return cached
    twin = create_model(
        model.__name__,
        __base__=model,
        claims=(tuple[LenientClaim, ...], ()),
    )
    _LENIENT_OUTPUT_MODELS[model] = twin
    return twin


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
                and (not claim.evidence_ids or bool(set(claim.evidence_ids) - authorized_evidence_ids))
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


def bind_contracts(
    manifest: AgentManifest,
    *,
    public: bool = False,
) -> AgentContractBinding:
    input_schema = manifest.public_input_schema if public else manifest.input_schema
    output_schema = manifest.public_output_schema if public else manifest.output_schema
    if input_schema is None or output_schema is None:
        raise ContractError("Agent manifest does not declare a public discovery contract")
    try:
        input_model = INPUT_CONTRACTS[input_schema.schema_id]
        output_model = OUTPUT_CONTRACTS[output_schema.schema_id]
    except KeyError as exc:
        raise ContractError("Agent manifest references an unknown contract schema") from exc
    expected_input = _schema_reference(input_schema.schema_id, input_model)
    expected_output = _schema_reference(output_schema.schema_id, output_model)
    if input_schema != expected_input or output_schema != expected_output:
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


class ReleaseGatePolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    objective_hard_gates: tuple[ObjectiveGate, ...] = _REQUIRED_OBJECTIVE_GATES

    @model_validator(mode="after")
    def objective_gates_are_complete(self) -> ReleaseGatePolicy:
        if set(self.objective_hard_gates) != set(_REQUIRED_OBJECTIVE_GATES):
            raise ValueError("all objective release gates are required")
        return self


class LoopPolicy(BaseModel):
    """Bounds deterministic re-invocation of an agent within one turn.

    ``max_iterations`` is a safety cap, never the stopping rule: the loop halts as
    soon as the deterministic sufficiency predicate is satisfied. A judge may only
    advise continuation -- it can never widen evidence, authority, or scope.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    max_iterations: int = Field(default=3, ge=1, le=10)
    judge_enabled: bool = False
    criteria: tuple[str, ...] = ()

    @model_validator(mode="after")
    def judge_requires_a_loop(self) -> LoopPolicy:
        if self.judge_enabled and not self.enabled:
            raise ValueError("a judge requires the loop to be enabled")
        if self.criteria and not self.enabled:
            raise ValueError("loop criteria require the loop to be enabled")
        return self


class DeploymentScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=256)
    project_id: str = Field(min_length=1, max_length=512)
    @field_validator("tenant_id", "project_id")
    @classmethod
    def reject_template_sentinels(cls, value: str) -> str:
        if value.startswith("provider-discovery://"):
            raise ValueError("deployment scope must be a real application-owned identifier")
        return value


class AgentManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["2.0"] = "2.0"
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$")
    name: str = Field(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
    behavior_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    parent_manifest_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    deployment_scope: DeploymentScope | None = None
    description: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    input_schema: SchemaReference
    output_schema: SchemaReference
    public_input_schema: SchemaReference | None = None
    public_output_schema: SchemaReference | None = None
    capability_bindings: tuple[CapabilityBinding, ...] = ()
    model_policy: ModelPolicy
    discovery_model_policy: ModelPolicy | None = None
    runtime_requirements: RuntimeRequirements
    knowledge_bindings: tuple[KnowledgeBinding, ...]
    evidence_policy: EvidencePolicy
    specialist_policy: SpecialistPolicy | None = None
    artifact_policy: ArtifactPolicy
    workflow_steps: tuple[str, ...] = Field(min_length=1)
    memory: MemoryPolicy
    release_gates: ReleaseGatePolicy
    loop: LoopPolicy = LoopPolicy()

    @model_validator(mode="after")
    def manifest_policy(self) -> AgentManifest:
        capability_operations = [
            (binding.descriptor_ref.id, binding.operation_ref.id) for binding in self.capability_bindings
        ]
        if len(capability_operations) != len(set(capability_operations)):
            raise ValueError("capability bindings must be unique")
        expected_scope = (
            (None, None)
            if self.deployment_scope is None
            else (
                self.deployment_scope.tenant_id,
                self.deployment_scope.project_id,
            )
        )
        binding_scopes = {
            (binding.tenant_scope, binding.project_scope)
            for binding in self.capability_bindings
        }
        if binding_scopes - {expected_scope}:
            raise ValueError(
                "capability binding scopes must exactly match the manifest deployment scope"
            )
        if self.online and "public" not in self.instructions.lower():
            raise ValueError("online manifests must state their public-data boundary")
        if (self.public_input_schema is None) != (self.public_output_schema is None):
            raise ValueError("public input and output schemas must be declared together")
        if self.supports_public_discovery != (self.discovery_model_policy is not None):
            raise ValueError(
                "public discovery contracts require a dedicated discovery model policy"
            )
        if self.discovery_model_policy is not None and (
            self.discovery_model_policy.performance_class != "fast"
            or not self.discovery_model_policy.tool_calling_required
        ):
            raise ValueError(
                "public discovery requires a fast tool-calling model policy"
            )
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
        return tuple(dict.fromkeys(binding.descriptor_ref.id for binding in self.capability_bindings))

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
    def supports_public_discovery(self) -> bool:
        return self.public_input_schema is not None

    @property
    def connector_sources(self) -> tuple[str, ...]:
        return tuple(source for binding in self.knowledge_bindings for source in binding.sources)

    @property
    def enable_web_search(self) -> bool:
        return self.online


def bind_deployment_scope(
    manifest: AgentManifest,
    scope: DeploymentScope,
) -> AgentManifest:
    if manifest.deployment_scope is not None and manifest.deployment_scope != scope:
        raise ContractError("Agent manifest is already bound to a different deployment scope")
    scoped_bindings: list[CapabilityBinding] = []
    for binding in manifest.capability_bindings:
        if (
            binding.tenant_scope is not None
            and (
                binding.tenant_scope != scope.tenant_id
                or binding.project_scope != scope.project_id
            )
        ):
            raise ContractError(
                "Capability binding is already bound to a different deployment scope"
            )
        scoped_binding = binding.model_copy(
            update={
                "tenant_scope": scope.tenant_id,
                "project_scope": scope.project_id,
            }
        )
        scoped_binding = scoped_binding.model_copy(
            update={
                "instance_ref": scoped_binding.instance_ref.model_copy(
                    update={"fingerprint": template_instance_fingerprint(scoped_binding)}
                )
            }
        )
        scoped_bindings.append(scoped_binding)
    return AgentManifest.model_validate(
        {
            **manifest.model_dump(mode="python"),
            "deployment_scope": scope,
            "capability_bindings": tuple(scoped_bindings),
        }
    )
