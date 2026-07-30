from __future__ import annotations

import pytest

from research_assistant_api.config import Settings
from research_assistant_api.cosmos_workspace import (
    InMemoryWorkspaceProjectProvider,
    WorkspaceProjectUnavailableError,
)
from research_assistant_api.identity import IdentityContext
from research_assistant_api.workspace import (
    PersonalProjectCreate,
    PersonalProjectUpdate,
    WorkspaceStore,
)


def _identity(user_id: str) -> IdentityContext:
    return IdentityContext(
        user_id=user_id,
        display_name=user_id,
        tenant_id="demo",
        groups=("researchers",),
        source="test",
    )


def test_clean_workspace_has_policy_defaults_without_demo_operational_data() -> None:
    store = WorkspaceStore(
        tenant_id="tenant-a",
        project_id="project-0123456789abcdef0123456789abcdef",
        project_name="Cancer outcomes review",
        project_description="A private workspace for a bounded evidence review.",
        seed_demo_data=False,
    )

    assert store.summary().project.name == "Cancer outcomes review"
    assert store.summary().project.online_research_default is False
    assert store.library() == []
    assert store.runs() == []
    assert store.approvals() == []
    assert all(not connector.enabled for connector in store.connectors())
    assert all(connector.assigned_agents == [] for connector in store.connectors())


def test_personal_project_provider_enforces_ownership_and_archive_lifecycle() -> None:
    provider = InMemoryWorkspaceProjectProvider(Settings())
    owner = _identity("researcher-a")
    other_user = _identity("researcher-b")
    project = provider.create_project(
        owner,
        PersonalProjectCreate(
            name="Cancer outcomes review",
            description="A private workspace for a bounded evidence review.",
        ),
    )

    assert provider.active_project_id(owner) == project.project_id
    assert provider.workspace_for(owner, None).project_id == project.project_id
    assert provider.workspace_for(owner, project.project_id).library() == []
    assert provider.list_projects(other_user) == ()
    with pytest.raises(WorkspaceProjectUnavailableError):
        provider.workspace_for(other_user, project.project_id)

    renamed = provider.update_project(
        owner,
        project.project_id,
        PersonalProjectUpdate(
            name="Oncology outcomes review",
            description="A private workspace for an oncology evidence review.",
        ),
    )

    assert renamed.name == "Oncology outcomes review"
    assert provider.workspace_for(owner, project.project_id).settings().name == renamed.name
    assert provider.workspace_for(owner, project.project_id).settings().description == renamed.description

    archived = provider.update_project(owner, project.project_id, PersonalProjectUpdate(archive=True))

    assert archived.lifecycle.value == "archived"
    assert provider.active_project_id(owner) is None
    with pytest.raises(WorkspaceProjectUnavailableError):
        provider.workspace_for(owner, project.project_id)