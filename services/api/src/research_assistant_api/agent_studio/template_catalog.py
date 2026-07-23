"""Governed task template catalog.

Unlike ``capability_registry``/``model_discovery`` (which proxy an *external*
system this backend does not own, and therefore must never fabricate a seed
catalog outside tests), templates are first-party, platform-authored
reference content: platform owners curate and version starter blueprints
the same way they version system agents (see module docstring convention in
``models.AgentTemplate``). A small, explicit, versioned built-in catalog is
therefore legitimate production content here, not a stand-in for an
unimplemented external integration.

The catalog is still expressed behind a ``TemplateCatalog`` protocol so a
future platform-owner-authored (e.g. Cosmos-backed) catalog can be swapped in
without any router/service change.
"""

from __future__ import annotations

from typing import Protocol

from research_assistant_api.agent_studio.models import (
    AgentTemplate,
    AgentTemplateSeed,
    CitationPolicy,
    RuntimeRequirements,
    TemplateReadiness,
)


class TemplateCatalog(Protocol):
    def list_templates(self) -> tuple[AgentTemplate, ...]: ...

    def get_template(self, template_id: str, version: str | None = None) -> AgentTemplate | None: ...


class StaticTemplateCatalog:
    """Catalog backed by a fixed, explicitly-versioned tuple of templates.

    ``get_template`` with ``version=None`` returns the highest-versioned
    entry for that ``template_id`` (lexicographic on the ``version`` string,
    matching this module's ``vN`` convention); an explicit ``version``
    returns that exact pin or ``None`` if it does not exist -- never a
    silent fallback to a different version.
    """

    def __init__(self, templates: tuple[AgentTemplate, ...]) -> None:
        self._templates = templates

    def list_templates(self) -> tuple[AgentTemplate, ...]:
        return self._templates

    def get_template(self, template_id: str, version: str | None = None) -> AgentTemplate | None:
        candidates = [template for template in self._templates if template.template_id == template_id]
        if version is not None:
            for template in candidates:
                if template.version == version:
                    return template
            return None
        if not candidates:
            return None
        return max(candidates, key=lambda template: template.version)


_BUILT_IN_TEMPLATES: tuple[AgentTemplate, ...] = (
    AgentTemplate(
        template_id="template-research-qa",
        version="v1",
        readiness=TemplateReadiness.GA,
        display_name="Research Q&A Assistant",
        description=(
            "Answers questions strictly from attached knowledge/citation "
            "sources; refuses to answer when no supporting evidence is "
            "found rather than guessing."
        ),
        category="research",
        tags=("qa", "citations", "research"),
        seed=AgentTemplateSeed(
            instructions=(
                "You are a research assistant. Answer only using the "
                "attached knowledge sources and cite every claim. If the "
                "attached sources do not contain the answer, say so "
                "explicitly instead of guessing."
            ),
            citation_policy=CitationPolicy(require_citations=True),
            tags=("qa", "citations"),
        ),
    ),
    AgentTemplate(
        template_id="template-doc-summarizer",
        version="v1",
        readiness=TemplateReadiness.GA,
        display_name="Document Summarizer",
        description="Produces a structured, faithful summary of an attached document without adding new claims.",
        category="productivity",
        tags=("summarization", "documents"),
        seed=AgentTemplateSeed(
            instructions=(
                "Summarize the attached document faithfully and concisely. "
                "Preserve section structure where useful. Do not introduce "
                "facts that are not present in the source document."
            ),
            tags=("summarization",),
        ),
    ),
    AgentTemplate(
        template_id="template-custom-hosted-starter",
        version="v1",
        readiness=TemplateReadiness.PREVIEW,
        display_name="Custom Hosted Starter",
        description=(
            "Minimal starting point for an agent that requires Custom "
            "Hosted runtime capabilities (e.g. non-Foundry-native tool "
            "code). Marked preview: the custom-hosted authoring experience "
            "is still evolving."
        ),
        category="advanced",
        tags=("custom-hosted", "advanced"),
        seed=AgentTemplateSeed(
            instructions="You are a custom-hosted agent. Replace these instructions with your agent's behavior.",
            runtime_requirements=RuntimeRequirements(requires_custom_code=True),
            tags=("custom-hosted",),
        ),
    ),
)


def default_template_catalog() -> StaticTemplateCatalog:
    """The platform-owned built-in template catalog.

    Safe to use in production: unlike capability/model discovery seeds,
    this is genuinely first-party governed content, not a substitute for an
    un-integrated external system.
    """

    return StaticTemplateCatalog(_BUILT_IN_TEMPLATES)
