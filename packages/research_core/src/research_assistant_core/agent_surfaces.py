"""Single source of truth for what an agent surface is.

Every list that used to answer "which capabilities chat?", "which agent serves
this capability?", "what copy does the studio show?" derives from the rows
below, so adding an agent is a row rather than an edit in eight places.

Nothing here changes behaviour: the rows encode exactly what the scattered
literals encoded, and a parity test pins them to it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from research_assistant_core.models import Capability, CapabilitySpec

#: How much of a retrieved chunk a surface's contract can accept.
#:
#: ``citations`` sends identifiers only; ``full_text`` adds the chunk body for
#: agents that must read it; ``none`` skips retrieval entirely.
EvidenceProjection = Literal["none", "citations", "full_text"]


class AgentEndpoint(BaseModel):
    """One deployed agent a researcher can talk to.

    Everything the runtime needs is declared here. Nothing infers behaviour from
    the agent's name.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    label: str
    description: str
    #: Reaches allowlisted public sources through the governed connector surface.
    web_access: bool = False
    #: How much of a retrieved chunk this agent's contract accepts. Contracts
    #: that reach public sources refuse caller-supplied evidence outright.
    evidence: EvidenceProjection = "none"


class AgentSurface(BaseModel):
    """One studio, and the agents behind it."""

    model_config = ConfigDict(frozen=True)

    capability: Capability
    agents: tuple[AgentEndpoint, ...]

    #: Renders the conversational surface rather than a bespoke studio.
    chat: bool = False
    #: A studio capability runs through ``ResearchService``. A chat-only
    #: capability has no blueprint and must be refused there rather than
    #: reaching a map that would raise ``KeyError``.
    studio: bool = True

    title: str
    short_title: str
    description: str
    example_prompt: str
    accent: str

    #: Chat header copy. Icons cannot cross the wire, so the client keeps one
    #: name-to-component map and this carries the name.
    icon: str | None = None
    eyebrow: str | None = None
    chat_title: str | None = None
    chat_description: str | None = None
    suggestions: tuple[str, ...] = ()

    @property
    def primary(self) -> AgentEndpoint:
        return self.agents[0]

    def endpoint(self, agent_name: str) -> AgentEndpoint | None:
        return next((item for item in self.agents if item.name == agent_name), None)

    @property
    def spec(self) -> CapabilitySpec:
        return CapabilitySpec(
            id=self.capability,
            title=self.title,
            short_title=self.short_title,
            description=self.description,
            example_prompt=self.example_prompt,
            accent=self.accent,
        )


def _library_and_web(stem: str) -> tuple[AgentEndpoint, ...]:
    """The standard pair: answer from the project library, or from public sources."""
    return (
        AgentEndpoint(
            name=f"{stem}-agent",
            label="Authorized evidence",
            description="Answers only from the sources stored in this project's library.",
            evidence="citations",
        ),
        AgentEndpoint(
            name=f"{stem}-online-agent",
            label="Public research",
            description="Also searches allowlisted public metadata sources.",
            web_access=True,
        ),
    )


AGENT_SURFACES: tuple[AgentSurface, ...] = (
    AgentSurface(
        capability=Capability.LITERATURE,
        agents=_library_and_web("literature"),
        chat=True,
        title="Literature review synthesis",
        short_title="Synthesize literature",
        description=(
            "Compare methods, findings, limitations, and open questions with claim-level evidence."
        ),
        example_prompt=(
            "Compare recent approaches to auditable retrieval-augmented research synthesis."
        ),
        accent="sage",
        icon="BookOpen",
        eyebrow="Evidence review",
        chat_title="Literature Studio",
        chat_description=(
            "Ask for a synthesis, a screening decision, or an extraction. Attach the "
            "papers you want it to work from."
        ),
        suggestions=(
            "Compare the methods used across the papers I attached and flag where they disagree.",
            "Screen these abstracts against an inclusion criterion of randomised trials since 2020.",
            "Build an extraction matrix of population, method, outcome, and limitation.",
        ),
    ),
    AgentSurface(
        capability=Capability.GRANT,
        agents=_library_and_web("grant"),
        chat=True,
        title="Grant application studio",
        short_title="Draft a grant",
        description=(
            "Extract requirements, shape specific aims, and flag unsupported or missing content."
        ),
        example_prompt="Draft three specific aims for an open research infrastructure project.",
        accent="amber",
        icon="FileText",
        eyebrow="Application lifecycle",
        chat_title="Grant Studio",
        chat_description=(
            "Attach the funding notice and your project facts, then ask for a requirement "
            "matrix, a draft, or a red-team review."
        ),
        suggestions=(
            "Turn the attached notice into a requirement matrix with owners and evidence gaps.",
            "Draft the specific aims section from the attached project facts.",
            "Red-team this draft against the sponsor's review criteria.",
        ),
    ),
    AgentSurface(
        capability=Capability.MATCHING,
        agents=_library_and_web("matching"),
        chat=True,
        title="PI and resource matching",
        short_title="Find collaborators",
        description=(
            "Match experts, cores, equipment, methods, and templates with transparent rationale."
        ),
        example_prompt=(
            "Find genomics expertise, sequencing equipment, and data-management resources."
        ),
        accent="indigo",
        icon="Users",
        eyebrow="Discovery",
        chat_title="Matching Explorer",
        chat_description=(
            "Describe the eligibility bar and what you need. Attach a roster or facility "
            "list to search within it."
        ),
        suggestions=(
            "Shortlist investigators with wet-lab capacity and prior NIH funding.",
            "Resolve duplicate entries in the attached roster before ranking.",
            "Explain which stored factors drove the top three matches.",
        ),
    ),
    AgentSurface(
        capability=Capability.DATASET,
        agents=(
            AgentEndpoint(
                name="dataset-agent",
                label="Authorized evidence",
                description="Analyses only the files attached to this session.",
            ),
        ),
        chat=True,
        title="Dataset and notebook summary",
        short_title="Summarize a dataset",
        description=(
            "Compute deterministic profiles before explaining metrics, quality, and next analyses."
        ),
        example_prompt=(
            "Summarize the sample outcome dataset and identify the strongest data-quality caveat."
        ),
        accent="blue",
        icon="FlaskConical",
        eyebrow="Data analysis",
        chat_title="Dataset Lab",
        chat_description=(
            "Attach a CSV or notebook output and ask what you want computed. Compute "
            "stays inside the approved sandbox."
        ),
        suggestions=(
            "Profile the attached CSV: schema, missingness, and obvious quality problems.",
            "Propose an analysis plan for the outcome column and say what it cannot support.",
            "Compute descriptive statistics per group and show the code you ran.",
        ),
    ),
    AgentSurface(
        capability=Capability.SCREENING,
        agents=(
            AgentEndpoint(
                name="screening-agent",
                label="Authorized evidence",
                description="Screens the papers stored in this project's library.",
                evidence="full_text",
            ),
        ),
        chat=True,
        # Conversational only: there is no ResearchService blueprint behind it.
        studio=False,
        title="Systematic review screening",
        short_title="Screen papers",
        description=(
            "Apply inclusion and exclusion criteria to the library and record one decision per paper."
        ),
        example_prompt=(
            "Screen for randomised trials of AI triage in adult emergency care."
        ),
        accent="sage",
        icon="ClipboardCheck",
        eyebrow="Evidence review",
        chat_title="Screening Studio",
        chat_description=(
            "State the inclusion and exclusion criteria. Every paper gets one decision, "
            "and `unclear` is a real answer."
        ),
        suggestions=(
            "Screen for randomised trials in adults, excluding editorials and protocols.",
            "Apply my criteria and tell me which papers you could not settle.",
            "Re-screen the unclear papers and explain what the abstracts are missing.",
        ),
    ),
    AgentSurface(
        capability=Capability.INSTITUTIONAL_QA,
        agents=(
            AgentEndpoint(
                name="institution-agent",
                label="Authorized evidence",
                description="Answers from stored institutional policy with versioned citations.",
            ),
        ),
        title="Institution-grounded Q&A",
        short_title="Ask institution",
        description=(
            "Answer from policies and guidance with versioned page and section citations."
        ),
        example_prompt="When must AI use be disclosed in an IRB protocol?",
        accent="rose",
    ),
    AgentSurface(
        capability=Capability.ORCHESTRATION,
        agents=(
            AgentEndpoint(
                name="research-coordinator",
                label="Authorized evidence",
                description="Routes work to pinned specialist agents.",
            ),
        ),
        title="Research workflow orchestration",
        short_title="Run a workflow",
        description="Plan, queue, retry, approve, cancel, and audit repeatable research pipelines.",
        example_prompt="Plan an ingest, compare, review, and export workflow for a new paper set.",
        accent="slate",
    ),
)

_BY_CAPABILITY = {surface.capability: surface for surface in AGENT_SURFACES}
_BY_AGENT = {
    endpoint.name: endpoint for surface in AGENT_SURFACES for endpoint in surface.agents
}


def agent_surfaces() -> tuple[AgentSurface, ...]:
    return AGENT_SURFACES


def surface_for(capability: Capability) -> AgentSurface:
    return _BY_CAPABILITY[capability]


def find_surface(capability: Capability) -> AgentSurface | None:
    return _BY_CAPABILITY.get(capability)


def capability_specs() -> tuple[CapabilitySpec, ...]:
    return tuple(surface.spec for surface in AGENT_SURFACES)


def offline_agents() -> dict[Capability, str]:
    return {surface.capability: surface.primary.name for surface in AGENT_SURFACES}


def online_agents() -> dict[Capability, str]:
    return {
        surface.capability: endpoint.name
        for surface in AGENT_SURFACES
        for endpoint in surface.agents
        if endpoint.web_access
    }


def endpoint_for(agent_name: str) -> AgentEndpoint | None:
    """Resolve a deployed agent by name, so nothing has to parse its suffix."""
    return _BY_AGENT.get(agent_name)


def chat_capabilities() -> tuple[Capability, ...]:
    return tuple(surface.capability for surface in AGENT_SURFACES if surface.chat)


def evidence_grounded_capabilities() -> tuple[Capability, ...]:
    return tuple(
        surface.capability
        for surface in AGENT_SURFACES
        if any(endpoint.evidence != "none" for endpoint in surface.agents)
    )


def studio_capabilities() -> tuple[Capability, ...]:
    return tuple(surface.capability for surface in AGENT_SURFACES if surface.studio)
