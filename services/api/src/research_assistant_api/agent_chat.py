"""Conversational surface over deployed Foundry Hosted Agents.

The studios previously drove agents through ``POST /api/studios/{capability}/run``:
one templated request, one deterministic artifact, no memory. This module adds
the missing conversational path using the two primitives the Responses protocol
actually provides (see
https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions):

* **conversation** -- a durable, Foundry-stored history of messages and tool
  calls. Passing it on every turn is what makes the agent remember; reusing a
  session id alone does not replay anything.
* **agent_session_id** -- a VM-isolated sandbox with a persistent ``$HOME``.
  Attachments are uploaded into it via the session files API, which is why the
  session is created *explicitly* here rather than left to the first turn: a
  file has to be on disk before the message that talks about it is sent.

Three boundaries are deliberate and load-bearing:

1. The browser only ever holds an opaque ``thread_id``. The conversation id,
    session id, and delegated user identity stay server-side, so a caller cannot bind
   themselves to somebody else's sandbox by guessing an identifier.
2. ``agent_name`` is validated against the capability's deployed agents. A
   client cannot name an arbitrary agent and have this service invoke it.
3. Assistant output is untrusted data. It is stored and returned verbatim for
   rendering, and it never selects a tool, grants a permission, or moves an
   approval. The dataset compute approval gates in ``dataset_execution`` and
   ``approval_context`` are unchanged and are not reachable from this path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Annotated, Protocol, cast
from uuid import uuid4

from azure.ai.projects import AIProjectClient
from azure.core.credentials import TokenCredential
from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from research_assistant_core.agent_surfaces import (
    chat_capabilities,
    endpoint_for,
    find_surface,
)
from research_assistant_core.azure_auth import azure_credential
from research_assistant_core.models import Capability
from starlette.concurrency import run_in_threadpool

from research_assistant_api.config import Settings
from research_assistant_api.cosmos_workspace import (
    WorkspaceProjectProvider,
    WorkspaceProjectUnavailableError,
)
from research_assistant_api.foundry import (
    HostedAgentConfigurationError,
    HostedAgentInvocationError,
    HostedAgentNotReadyError,
    HostedAgentProgress,
    HostedAgentReply,
    build_hosted_agent_reply,
    create_response_with_retries,
    parse_hosted_agent_payload,
    stream_response_events,
)
from research_assistant_api.identity import IdentityContext, resolve_identity
from research_assistant_api.workspace import (
    ChatActivity,
    ChatAttachment,
    ChatMessage,
    ChatThread,
    ChatThreadConflictError,
    VerifiedGrantOpportunity,
    WorkspaceStore,
    utc_now,
)

logger = logging.getLogger("research_assistant.agent_chat")

router = APIRouter(prefix="/api/agent-chat", tags=["agent-chat"])

#: Capabilities that render the chat template. Institutional Q&A keeps its
#: version-and-abstain surface and workflow automation keeps its DAG editor,
#: so neither is offered here.
CHAT_CAPABILITIES = chat_capabilities()

MAX_UPLOAD_BYTES = 20_000_000
MAX_MESSAGE_CHARS = 8_000

#: Mirrors the library ingestion allowlist. Anything the session sandbox
#: cannot usefully open is rejected at the edge rather than burning a 50 MB
#: upload against the platform limit.
ALLOWED_UPLOAD_TYPES = {
    "application/json",
    "application/pdf",
    "text/csv",
    "text/markdown",
    "text/plain",
}


class AgentChoice(BaseModel):
    """A deployed agent the caller may talk to for a given capability."""

    name: str
    label: str
    description: str


class ChatAttachmentView(BaseModel):
    path: str
    size_bytes: int
    content_type: str
    uploaded_at: datetime


class ChatActivityView(BaseModel):
    kind: str
    label: str
    status: str
    detail: str | None = None


class ChatMessageView(BaseModel):
    id: str
    role: str
    content: str
    created_at: datetime
    agent_name: str | None = None
    attachments: list[ChatAttachmentView] = Field(default_factory=list)
    activity: list[ChatActivityView] = Field(default_factory=list)
    duration_ms: int | None = None
    source_count: int = 0
    opportunities: list[VerifiedGrantOpportunity] = Field(default_factory=list)


class _AgentReplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    claims: list[object] = Field(default_factory=list)
    code: str | None = None
    computed_outputs: list[object] = Field(default_factory=list)
    conflicts: list[object] = Field(default_factory=list)
    consensus: list[object] = Field(default_factory=list)
    decisions: list[object] = Field(default_factory=list)
    disagreements: list[object] = Field(default_factory=list)
    effective_dates: list[object] = Field(default_factory=list)
    evidence: list[object] = Field(default_factory=list)
    lead_record_ids: list[object] = Field(default_factory=list)
    limitations: list[object] = Field(default_factory=list)
    opportunities: list[VerifiedGrantOpportunity] = Field(default_factory=list)
    ready_for_review: bool | None = None
    record_ids: list[object] = Field(default_factory=list)
    requirements: list[object] = Field(default_factory=list)
    search_urls: list[object] = Field(default_factory=list)
    selected_opportunities: list[object] = Field(default_factory=list)
    unresolved: list[object] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_public_contract(self) -> _AgentReplyEnvelope:
        if not self.summary.strip():
            raise ValueError("Agent reply summary must not be blank")
        identifiers = [item.grants_gov_id for item in self.opportunities]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Agent reply contains duplicate Grants.gov opportunities")
        return self


_AGENT_REPLY_FIELDS: dict[str, frozenset[str]] = {
    "literature-agent": frozenset(
        {"summary", "claims", "limitations", "evidence", "consensus", "disagreements", "search_urls"}
    ),
    "grant-agent": frozenset(
        {
            "summary",
            "claims",
            "limitations",
            "evidence",
            "requirements",
            "ready_for_review",
            "selected_opportunities",
            "opportunities",
        }
    ),
    "matching-agent": frozenset(
        {"summary", "claims", "limitations", "evidence", "record_ids", "lead_record_ids"}
    ),
    "dataset-agent": frozenset(
        {"summary", "claims", "limitations", "evidence", "code", "computed_outputs"}
    ),
    "screening-agent": frozenset({"summary", "decisions", "conflicts", "unresolved"}),
}


class ChatThreadView(BaseModel):
    """Read model for a thread. Carries no session, conversation, or owner id."""

    id: str
    capability: Capability
    agent_name: str
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageView] = Field(default_factory=list)
    attachments: list[ChatAttachmentView] = Field(default_factory=list)


class ChatThreadCreate(BaseModel):
    capability: Capability
    agent_name: str = Field(min_length=1, max_length=120)


class ChatMessageCreate(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    client_message_id: str = Field(
        min_length=16,
        max_length=120,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


@dataclass(frozen=True, slots=True)
class ThreadHandle:
    conversation_id: str
    session_id: str


class ChatGateway(Protocol):
    def open_thread(self, agent_name: str, *, user_identity: str) -> ThreadHandle: ...

    def send(
        self,
        *,
        agent_name: str,
        conversation_id: str,
        session_id: str,
        user_identity: str,
        text: str,
    ) -> HostedAgentReply: ...

    def stream(
        self,
        *,
        agent_name: str,
        conversation_id: str,
        session_id: str,
        user_identity: str,
        text: str,
    ) -> Iterator[HostedAgentProgress]: ...

    def upload(
        self,
        *,
        agent_name: str,
        session_id: str,
        user_identity: str,
        path: str,
        content: bytes,
    ) -> None: ...


class AgentChatGateway:
    """Session- and conversation-aware client for deployed Hosted Agents."""

    def __init__(self, settings: Settings, credential: TokenCredential | None = None) -> None:
        self._settings = settings
        self._credential = credential or self._build_credential()

    def _build_credential(self) -> TokenCredential:
        return azure_credential(self._settings.managed_identity_client_id)

    def _project(self) -> AIProjectClient:
        endpoint = self._settings.foundry_project_endpoint
        if not endpoint:
            raise HostedAgentConfigurationError("FOUNDRY_PROJECT_ENDPOINT is required in hosted execution mode")
        return AIProjectClient(endpoint=endpoint, credential=self._credential, allow_preview=True)

    def open_thread(self, agent_name: str, *, user_identity: str) -> ThreadHandle:
        """Provision a sandbox and a conversation, in that order.

        The session is created ahead of the first turn so the caller can attach
        files to their opening message; the conversation is what carries history
        forward on every later turn.
        """
        project = self._project()
        headers = {"x-ms-user-identity": user_identity}
        try:
            session = project.agents.create_session(
                agent_name=agent_name,
                body={},
                headers=headers,
            )
            conversation = project.get_openai_client(agent_name=agent_name).conversations.create(
                extra_headers=headers,
            )
        except Exception as exc:
            raise HostedAgentInvocationError(f"Could not open a session for Hosted Agent {agent_name}.") from exc
        session_id = getattr(session, "agent_session_id", None) or getattr(session, "id", None)
        conversation_id = getattr(conversation, "id", None)
        if not session_id or not conversation_id:
            raise HostedAgentInvocationError(
                f"Hosted Agent {agent_name} returned an incomplete session or conversation reference."
            )
        return ThreadHandle(conversation_id=str(conversation_id), session_id=str(session_id))

    def send(
        self,
        *,
        agent_name: str,
        conversation_id: str,
        session_id: str,
        user_identity: str,
        text: str,
    ) -> HostedAgentReply:
        client = self._project().get_openai_client(agent_name=agent_name)
        started_at = time.monotonic()
        response = create_response_with_retries(
            client,
            agent_name,
            {
                "input": text,
                "extra_body": {
                    "conversation": conversation_id,
                    "agent_session_id": session_id,
                },
                "extra_headers": {"x-ms-user-identity": user_identity},
            },
        )
        return build_hosted_agent_reply(response, agent_name, started_at)

    def stream(
        self,
        *,
        agent_name: str,
        conversation_id: str,
        session_id: str,
        user_identity: str,
        text: str,
    ) -> Iterator[HostedAgentProgress]:
        client = self._project().get_openai_client(agent_name=agent_name)
        return stream_response_events(
            client,
            agent_name,
            {
                "input": text,
                "extra_body": {
                    "conversation": conversation_id,
                    "agent_session_id": session_id,
                },
                "extra_headers": {"x-ms-user-identity": user_identity},
            },
        )

    def upload(
        self,
        *,
        agent_name: str,
        session_id: str,
        user_identity: str,
        path: str,
        content: bytes,
    ) -> None:
        project = self._project()
        try:
            project.agents.upload_session_file(
                agent_name,
                session_id,
                content,
                path=path,
                headers={"x-ms-user-identity": user_identity},
            )
        except Exception as exc:
            raise HostedAgentInvocationError(f"Could not upload {path} to the Hosted Agent session.") from exc


def _capability_agents(capability: Capability) -> tuple[AgentChoice, ...]:
    """Deployed agents for a capability, in registry order."""
    surface = find_surface(capability)
    if surface is None:
        return ()
    return tuple(
        AgentChoice(
            name=endpoint.name,
            label=endpoint.label,
            description=endpoint.description,
        )
        for endpoint in surface.agents
    )


def _workspace_access(request: Request) -> tuple[WorkspaceStore, IdentityContext]:
    settings = cast(Settings, request.app.state.settings)
    identity = resolve_identity(request, settings)
    provider = cast(WorkspaceProjectProvider, request.app.state.workspace_projects)
    try:
        store = provider.workspace_for(identity, request.headers.get("X-Research-Project-ID"))
    except WorkspaceProjectUnavailableError as exc:
        raise HTTPException(status_code=404, detail="The requested project is unavailable.") from exc
    return store, identity


def _gateway(request: Request) -> ChatGateway:
    gateway = getattr(request.app.state, "agent_chat", None)
    if gateway is None:
        raise HTTPException(status_code=503, detail="Agent chat is not configured for this deployment.")
    return cast(ChatGateway, gateway)


def delegated_user_identity_for(identity: IdentityContext, store: WorkspaceStore) -> str:
    """Derive a stable opaque identity from authenticated server-side state."""
    subject = f"{identity.tenant_id}\0{store.project_id}\0{identity.user_id}"
    return f"ra:{sha256(subject.encode('utf-8')).hexdigest()}"


def _load_thread(store: WorkspaceStore, thread_id: str, identity: IdentityContext) -> ChatThread:
    thread = store.chat_thread(thread_id, owner_principal_id=identity.user_id)
    if thread is None:
        raise HTTPException(status_code=404, detail="Chat thread not found.")
    return thread


def _save_thread(store: WorkspaceStore, thread: ChatThread) -> ChatThread:
    try:
        return store.save_chat_thread(thread)
    except ChatThreadConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="The chat changed while this request was running. Retry the turn.",
        ) from exc


def _claim_turn(
    store: WorkspaceStore,
    thread: ChatThread,
    client_message_id: str,
) -> ChatThread:
    try:
        return store.claim_chat_turn(thread, client_message_id)
    except ChatThreadConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="Another chat turn is already in progress. Retry this turn shortly.",
        ) from exc


def _release_turn(
    store: WorkspaceStore,
    thread: ChatThread,
    client_message_id: str,
) -> None:
    try:
        store.release_chat_turn(thread, client_message_id)
    except Exception:
        logger.exception("Could not release the durable lease for chat %s.", thread.id)


def _validate_agent(capability: Capability, agent_name: str) -> AgentChoice:
    for choice in _capability_agents(capability):
        if choice.name == agent_name:
            return choice
    raise HTTPException(
        status_code=422,
        detail="The requested agent is not deployed for this capability.",
    )


def _require_chat_capability(capability: Capability) -> Capability:
    if capability not in CHAT_CAPABILITIES:
        raise HTTPException(status_code=422, detail="This capability does not expose a chat surface.")
    return capability


def _attachment_view(attachment: ChatAttachment) -> ChatAttachmentView:
    return ChatAttachmentView(**attachment.model_dump())


def _message_view(message: ChatMessage) -> ChatMessageView:
    return ChatMessageView(
        id=message.id,
        role=message.role,
        content=message.content,
        created_at=message.created_at,
        agent_name=message.agent_name,
        attachments=[_attachment_view(item) for item in message.attachments],
        activity=[ChatActivityView(**item.model_dump()) for item in message.activity],
        duration_ms=message.duration_ms,
        source_count=message.source_count,
        opportunities=list(message.opportunities),
    )


def _thread_view(thread: ChatThread) -> ChatThreadView:
    return ChatThreadView(
        id=thread.id,
        capability=thread.capability,
        agent_name=thread.agent_name,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
        messages=[_message_view(message) for message in thread.messages],
        attachments=[_attachment_view(item) for item in thread.attachments],
    )


def _safe_upload_path(filename: str | None) -> str:
    """Reduce a client filename to a single flat, sandbox-relative name."""
    candidate = (filename or "attachment").replace("\\", "/").rsplit("/", 1)[-1].strip()
    cleaned = "".join(character for character in candidate if character.isalnum() or character in "._- ").strip()
    cleaned = cleaned.lstrip(".")
    return cleaned[:120] or "attachment"


def _gateway_failure(exc: Exception) -> HTTPException:
    if isinstance(exc, HostedAgentConfigurationError | HostedAgentNotReadyError):
        return HTTPException(status_code=503, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@router.get("/agents", response_model=list[AgentChoice])
def list_chat_agents(capability: Capability, request: Request) -> list[AgentChoice]:
    _workspace_access(request)
    return list(_capability_agents(_require_chat_capability(capability)))


@router.post("/threads", response_model=ChatThreadView, status_code=status.HTTP_201_CREATED)
async def open_chat_thread(payload: ChatThreadCreate, request: Request) -> ChatThreadView:
    store, identity = _workspace_access(request)
    capability = _require_chat_capability(payload.capability)
    _validate_agent(capability, payload.agent_name)
    gateway = _gateway(request)
    delegated_user_identity = delegated_user_identity_for(identity, store)
    try:
        handle = await run_in_threadpool(
            gateway.open_thread,
            payload.agent_name,
            user_identity=delegated_user_identity,
        )
    except (
        HostedAgentConfigurationError,
        HostedAgentNotReadyError,
        HostedAgentInvocationError,
    ) as exc:
        raise _gateway_failure(exc) from exc
    now = utc_now()
    thread = _save_thread(
        store,
        ChatThread(
            id=f"chat-{uuid4().hex[:16]}",
            project_id=store.project_id,
            tenant_id=identity.tenant_id,
            capability=capability,
            agent_name=payload.agent_name,
            owner_principal_id=identity.user_id,
            conversation_id=handle.conversation_id,
            session_id=handle.session_id,
            delegated_user_identity=delegated_user_identity,
            created_at=now,
            updated_at=now,
        ),
    )
    return _thread_view(thread)


@router.get("/threads/{thread_id}", response_model=ChatThreadView)
def get_chat_thread(thread_id: str, request: Request) -> ChatThreadView:
    store, identity = _workspace_access(request)
    return _thread_view(_load_thread(store, thread_id, identity))


@router.post("/threads/{thread_id}/files", response_model=ChatAttachmentView)
async def upload_chat_file(
    thread_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
) -> ChatAttachmentView:
    store, identity = _workspace_access(request)
    thread = _load_thread(store, thread_id, identity)
    if thread.active_turn_id is not None and (
        thread.active_turn_expires_at is None
        or thread.active_turn_expires_at > utc_now()
    ):
        raise HTTPException(
            status_code=409,
            detail="Wait for the active chat turn before changing its attachments.",
        )
    if file.content_type not in ALLOWED_UPLOAD_TYPES:
        raise HTTPException(
            status_code=415,
            detail="Supported attachments are PDF, plain text, Markdown, CSV, and JSON.",
        )
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status_code=422, detail="The attached file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Attachments are limited to 20 MB.")
    path = _safe_upload_path(file.filename)
    gateway = _gateway(request)
    try:
        await run_in_threadpool(
            gateway.upload,
            agent_name=thread.agent_name,
            session_id=thread.session_id,
            user_identity=thread.delegated_user_identity,
            path=path,
            content=content,
        )
    except (
        HostedAgentConfigurationError,
        HostedAgentNotReadyError,
        HostedAgentInvocationError,
    ) as exc:
        raise _gateway_failure(exc) from exc
    attachment = ChatAttachment(
        path=path,
        size_bytes=len(content),
        content_type=file.content_type,
        uploaded_at=utc_now(),
    )
    remaining = [item for item in thread.attachments if item.path != attachment.path]
    _save_thread(
        store,
        thread.model_copy(update={"attachments": [*remaining, attachment]}),
    )
    return _attachment_view(attachment)


@router.post("/threads/{thread_id}/messages", response_model=ChatMessageView)
async def send_chat_message(
    thread_id: str,
    payload: ChatMessageCreate,
    request: Request,
) -> ChatMessageView:
    store, identity = _workspace_access(request)
    thread = _load_thread(store, thread_id, identity)
    client_message_id = payload.client_message_id
    assistant_id = f"reply-{client_message_id}"
    existing = next((message for message in thread.messages if message.id == assistant_id), None)
    if existing is not None:
        return _message_view(existing)
    _require_ready_agent_connectors(thread, store)

    turn_key = (thread.id, identity.user_id, client_message_id)
    if turn_key in _ACTIVE_STREAM_TURNS:
        raise HTTPException(status_code=409, detail="This chat turn is already streaming.")
    gateway = _gateway(request)
    thread = _claim_turn(store, thread, client_message_id)
    active = _ACTIVE_CHAT_TURNS.get(turn_key)
    if active is None:
        active = asyncio.create_task(
            _execute_chat_turn(
                gateway=gateway,
                thread=thread,
                payload=payload,
                request=request,
                store=store,
                identity=identity,
                client_message_id=client_message_id,
            )
        )
        _ACTIVE_CHAT_TURNS[turn_key] = active

        def remove_completed(completed: asyncio.Task[ChatMessageView]) -> None:
            if _ACTIVE_CHAT_TURNS.get(turn_key) is completed:
                _ACTIVE_CHAT_TURNS.pop(turn_key, None)

        active.add_done_callback(remove_completed)
    return await asyncio.shield(active)


_ACTIVE_CHAT_TURNS: dict[tuple[str, str, str], asyncio.Task[ChatMessageView]] = {}
_ACTIVE_STREAM_TURNS: set[tuple[str, str, str]] = set()


def _sse_event(event_type: str, payload: dict[str, object]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


@router.post(
    "/threads/{thread_id}/messages/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_chat_message(
    thread_id: str,
    payload: ChatMessageCreate,
    request: Request,
) -> StreamingResponse:
    """Stream observable Hosted Agent progress, then persist one canonical turn."""
    store, identity = _workspace_access(request)
    thread = _load_thread(store, thread_id, identity)
    client_message_id = payload.client_message_id
    assistant_id = f"reply-{client_message_id}"
    existing = next((message for message in thread.messages if message.id == assistant_id), None)
    if existing is not None:
        body: Iterator[str] = iter(
            [
                _sse_event(
                    "completed",
                    {"type": "completed", "message": _message_view(existing).model_dump(mode="json")},
                )
            ]
        )
        return StreamingResponse(body, media_type="text/event-stream")
    _require_ready_agent_connectors(thread, store)

    turn_key = (thread.id, identity.user_id, client_message_id)
    if turn_key in _ACTIVE_STREAM_TURNS or turn_key in _ACTIVE_CHAT_TURNS:
        raise HTTPException(status_code=409, detail="This chat turn is already in progress.")
    gateway = _gateway(request)
    body = _execute_chat_turn_stream(
        gateway=gateway,
        thread=thread,
        store=store,
        payload=payload,
        client_message_id=client_message_id,
        identity=identity,
        settings=cast(Settings, request.app.state.settings),
        turn_key=turn_key,
    )
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _execute_chat_turn_stream(
    *,
    gateway: ChatGateway,
    thread: ChatThread,
    store: WorkspaceStore,
    payload: ChatMessageCreate,
    client_message_id: str,
    identity: IdentityContext,
    settings: Settings,
    turn_key: tuple[str, str, str],
) -> Iterator[str]:
    started_at = utc_now()
    completed = False
    claimed = False
    try:
        thread = store.claim_chat_turn(thread, client_message_id)
        claimed = True
        _ACTIVE_STREAM_TURNS.add(turn_key)
        pending = [item for item in thread.attachments if not _already_announced(thread, item)]
        envelope = _contract_envelope(
            thread,
            store=store,
            identity=identity,
            settings=settings,
            text=payload.text,
            attachments=pending,
        )
        yield _sse_event(
            "started",
            {
                "type": "started",
                "message_id": f"reply-{client_message_id}",
                "agent_name": thread.agent_name,
                "created_at": started_at.isoformat(),
            },
        )
        for progress in gateway.stream(
            agent_name=thread.agent_name,
            conversation_id=thread.conversation_id,
            session_id=thread.session_id,
            user_identity=thread.delegated_user_identity,
            text=envelope,
        ):
            if progress.type == "activity" and progress.activity is not None:
                activity = ChatActivityView(
                    kind=progress.activity.kind,
                    label=progress.activity.label,
                    status=progress.activity.status,
                    detail=progress.activity.detail,
                )
                yield _sse_event(
                    "activity",
                    {
                        "type": "activity",
                        "activity_id": progress.activity_id or f"activity-{uuid4().hex}",
                        "activity": activity.model_dump(mode="json"),
                    },
                )
                continue
            if progress.type != "completed" or progress.reply is None:
                continue

            reply = progress.reply
            final_payload = _validated_final_payload(
                reply.content,
                agent_name=thread.agent_name,
            )
            opportunities = _verified_grant_opportunities(reply.content)
            content = _validate_public_reply(
                _render_agent_reply(
                    reply.content,
                    opportunities=opportunities,
                ),
                payload=final_payload,
                opportunities=opportunities,
            )
            follow_up = _next_steps(thread, reply.content, evidence_count=0)
            assistant_message = ChatMessage(
                id=f"reply-{client_message_id}",
                role="assistant",
                content=f"{content}\n\n{follow_up}" if follow_up else content,
                created_at=utc_now(),
                agent_name=reply.agent_name,
                activity=[
                    ChatActivity(
                        kind=item.kind,
                        label=item.label,
                        status=item.status,
                        detail=item.detail,
                    )
                    for item in reply.activity
                ],
                duration_ms=reply.duration_ms,
                source_count=reply.source_count,
                opportunities=opportunities,
            )
            user_message = ChatMessage(
                id=client_message_id,
                role="user",
                content=payload.text,
                created_at=started_at,
                attachments=list(pending),
            )
            store.save_chat_thread(
                thread.model_copy(
                    update={
                        "messages": [*thread.messages, user_message, assistant_message],
                        "active_turn_id": None,
                        "active_turn_lease_id": None,
                        "active_turn_expires_at": None,
                    }
                )
            )
            completed = True
            yield _sse_event(
                "completed",
                {
                    "type": "completed",
                    "message": _message_view(assistant_message).model_dump(mode="json"),
                },
            )
            return
    except (
        HostedAgentConfigurationError,
        HostedAgentNotReadyError,
        HostedAgentInvocationError,
    ) as exc:
        status_code = (
            503
            if isinstance(exc, HostedAgentConfigurationError | HostedAgentNotReadyError)
            else 502
        )
        yield _sse_event(
            "error",
            {"type": "error", "detail": str(exc), "status": status_code},
        )
    except ChatThreadConflictError:
        yield _sse_event(
            "error",
            {
                "type": "error",
                "detail": "The chat changed while this request was running. Retry the turn.",
                "status": 409,
            },
        )
    except Exception:
        logger.exception("Unexpected failure while streaming Hosted Agent %s.", thread.agent_name)
        yield _sse_event(
            "error",
            {
                "type": "error",
                "detail": "The Hosted Agent stream ended unexpectedly.",
                "status": 502,
            },
        )
    finally:
        _ACTIVE_STREAM_TURNS.discard(turn_key)
        if claimed and not completed:
            _release_turn(store, thread, client_message_id)


async def _execute_chat_turn(
    *,
    gateway: ChatGateway,
    thread: ChatThread,
    payload: ChatMessageCreate,
    request: Request,
    store: WorkspaceStore,
    identity: IdentityContext,
    client_message_id: str,
) -> ChatMessageView:
    pending = [item for item in thread.attachments if not _already_announced(thread, item)]
    user_message = ChatMessage(
        id=client_message_id,
        role="user",
        content=payload.text,
        created_at=utc_now(),
        attachments=list(pending),
    )
    completed = False
    try:
        try:
            reply = await run_in_threadpool(
                gateway.send,
                agent_name=thread.agent_name,
                conversation_id=thread.conversation_id,
                session_id=thread.session_id,
                user_identity=thread.delegated_user_identity,
                text=_contract_envelope(
                    thread,
                    store=store,
                    identity=identity,
                    settings=cast(Settings, request.app.state.settings),
                    text=payload.text,
                    attachments=pending,
                ),
            )
            final_payload = _validated_final_payload(
                reply.content,
                agent_name=thread.agent_name,
            )
            opportunities = _verified_grant_opportunities(reply.content)
            content = _validate_public_reply(
                _render_agent_reply(
                    reply.content,
                    opportunities=opportunities,
                ),
                payload=final_payload,
                opportunities=opportunities,
            )
        except (
            HostedAgentConfigurationError,
            HostedAgentNotReadyError,
            HostedAgentInvocationError,
        ) as exc:
            raise _gateway_failure(exc) from exc
        follow_up = _next_steps(thread, reply.content, evidence_count=0)
        assistant_message = ChatMessage(
            id=f"reply-{client_message_id}",
            role="assistant",
            content=f"{content}\n\n{follow_up}" if follow_up else content,
            created_at=utc_now(),
            agent_name=reply.agent_name,
            activity=[
                ChatActivity(
                    kind=item.kind,
                    label=item.label,
                    status=item.status,
                    detail=item.detail,
                )
                for item in reply.activity
            ],
            duration_ms=reply.duration_ms,
            source_count=reply.source_count,
            opportunities=opportunities,
        )
        _save_thread(
            store,
            thread.model_copy(
                update={
                    "messages": [*thread.messages, user_message, assistant_message],
                    "active_turn_id": None,
                    "active_turn_lease_id": None,
                    "active_turn_expires_at": None,
                }
            )
        )
        completed = True
        return _message_view(assistant_message)
    finally:
        if not completed:
            _release_turn(store, thread, client_message_id)


def _already_announced(thread: ChatThread, attachment: ChatAttachment) -> bool:
    return any(
        item.path == attachment.path and item.uploaded_at == attachment.uploaded_at
        for message in thread.messages
        for item in message.attachments
    )


#: Hosted specialists validate each turn against their declared input contract
#: (`shared/middleware.py` -> `input_model.model_validate_json`), so a plain chat
#: string comes back as "Hosted invocation does not match the agent input
#: contract". Every contract extends `ResearchRequest` and forbids unknown
#: fields, so this carries the shared required keys plus only the extras a
#: given agent declares.
_INTERNAL_SENSITIVITY = "internal"

_PRIVATE_REPLY_MARKERS = (
    "authorized_connector_ids",
    "principal_id",
    "project_id",
    "selected_opportunities",
    "sensitivity",
    "session_files",
    "session_id",
    "tenant_id",
    "your reply did not match",
)

#: Specialists answer with their typed output contract, so the raw reply is a
#: JSON document. Rendering it verbatim in the transcript is unreadable; these
#: are the contract fields worth surfacing, in the order a reader wants them.
_REPLY_SECTIONS: tuple[tuple[str, str], ...] = (
    ("consensus", "Consensus"),
    ("disagreements", "Disagreements"),
    ("requirements", "Requirements"),
    ("computed_outputs", "Computed outputs"),
    ("record_ids", "Matched records"),
    ("lead_record_ids", "Leads"),
    ("effective_dates", "Effective dates"),
    ("search_urls", "Searched sources"),
    ("limitations", "Limitations"),
)


def _bullet(value: object) -> str:
    if isinstance(value, dict):
        name = value.get("name")
        if name and "value" in value:
            return f"**{name}**: {value.get('value')}"
        label = value.get("text") or value.get("title") or value.get("id")
        if label:
            return str(label)
    return str(value)


def _final_payload(raw: str) -> dict[str, object] | None:
    """Return the last complete typed payload in a Hosted Agent reply."""
    try:
        payload = parse_hosted_agent_payload(raw)
    except HostedAgentInvocationError:
        return None
    return payload if "summary" in payload else None


def _validated_final_payload(raw: str, *, agent_name: str) -> dict[str, object]:
    payload = _final_payload(raw)
    if payload is None:
        raise HostedAgentInvocationError(
            "The Hosted Agent returned an invalid structured response."
        )
    allowed_fields = _AGENT_REPLY_FIELDS.get(agent_name)
    if allowed_fields is None or not set(payload).issubset(allowed_fields):
        raise HostedAgentInvocationError(
            "The Hosted Agent returned an invalid structured response."
        )
    try:
        _AgentReplyEnvelope.model_validate(payload)
    except ValidationError as exc:
        raise HostedAgentInvocationError(
            "The Hosted Agent returned an invalid structured response."
        ) from exc
    return payload


def _validate_public_reply(
    content: str,
    *,
    payload: dict[str, object],
    opportunities: list[VerifiedGrantOpportunity],
) -> str:
    public_text = "\n".join([
        content,
        *_string_values(payload),
        *(item.model_dump_json() for item in opportunities),
    ])
    normalized = public_text.casefold()
    if any(marker in normalized for marker in _PRIVATE_REPLY_MARKERS):
        raise HostedAgentInvocationError(
            "The Hosted Agent returned unsafe structured response content."
        )
    if content.lstrip().startswith(("{", "[")):
        raise HostedAgentInvocationError(
            "The Hosted Agent returned unsafe structured response content."
        )
    return content


def _string_values(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)


def _verified_grant_opportunities(raw: str) -> list[VerifiedGrantOpportunity]:
    payload = _final_payload(raw)
    if payload is None:
        return []
    values = payload.get("opportunities")
    if not isinstance(values, list):
        return []
    opportunities: list[VerifiedGrantOpportunity] = []
    seen_ids: set[str] = set()
    for value in values[:50]:
        try:
            opportunity = VerifiedGrantOpportunity.model_validate(value)
        except ValidationError:
            continue
        if opportunity.grants_gov_id in seen_ids:
            continue
        seen_ids.add(opportunity.grants_gov_id)
        opportunities.append(opportunity)
    return opportunities


def _is_grants_gov_evidence(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    evidence_id = str(value.get("evidence_id") or "")
    source_uri = str(value.get("source_uri") or "")
    return evidence_id.startswith("connector:grants_gov:") or source_uri.startswith(
        "https://www.grants.gov/search-results-detail/"
    )


def _grant_conversation_lead(
    opportunities: list[VerifiedGrantOpportunity],
) -> str:
    first = opportunities[0]
    rationale = first.relevance_rationale.strip()
    if len(opportunities) > 1:
        return (
            f"I found {len(opportunities)} opportunities. "
            f"{first.opportunity_number} ranks first for this request: {rationale}"
        )
    if first.relevance == "unassessed":
        return f"I verified {first.opportunity_number} against Grants.gov. {rationale}"
    return (
        f"{first.opportunity_number} is the strongest match for this request: "
        f"{rationale}"
    )


def _render_agent_reply(
    raw: str,
    *,
    opportunities: list[VerifiedGrantOpportunity] | None = None,
) -> str:
    """Turn a typed contract response into readable Markdown, or pass prose through."""
    payload = _final_payload(raw)
    if payload is None:
        return raw

    opportunities = (
        _verified_grant_opportunities(raw)
        if opportunities is None
        else opportunities
    )
    raw_evidence = payload.get("evidence")
    evidence = raw_evidence if isinstance(raw_evidence, list) else []
    grants_gov_evidence_ids = {
        str(item.get("evidence_id"))
        for item in evidence
        if _is_grants_gov_evidence(item)
        and isinstance(item, dict)
        and item.get("evidence_id")
    }
    summary = (
        _grant_conversation_lead(opportunities)
        if opportunities
        else str(payload.get("summary") or "").strip()
    )
    lines: list[str] = [summary] if summary else []

    claims = payload.get("claims") or []
    if isinstance(claims, list) and claims:
        findings: list[str] = []
        unsupported: list[str] = []
        for claim in claims:
            if not isinstance(claim, dict):
                findings.append(f"- {_bullet(claim)}")
                continue
            claim_evidence_ids = {
                str(value) for value in claim.get("evidence_ids") or []
            }
            if (
                opportunities
                and
                claim_evidence_ids
                and claim_evidence_ids <= grants_gov_evidence_ids
            ):
                continue
            support = str(claim.get("support") or "").replace("_", " ")
            if support == "unsupported":
                unsupported.append(f"- Unsupported: {claim.get('text', '')}")
                continue
            marker = f" _({support})_" if support and support != "supported" else ""
            findings.append(f"- {claim.get('text', '')}{marker}")
        if findings:
            lines.append("\n**Findings**\n")
            lines.extend(findings)
        if unsupported:
            lines.append("\n**Not established**\n")
            lines.extend(unsupported)

    for key, label in _REPLY_SECTIONS:
        values = payload.get(key)
        if isinstance(values, list) and values:
            lines.append(f"\n**{label}**\n")
            lines.extend(f"- {_bullet(item)}" for item in values)

    if payload.get("code"):
        lines.append("\n**Code**\n")
        lines.append(f"```python\n{payload['code']}\n```")

    if evidence:
        evidence_lines: list[str] = []
        for item in evidence:
            if not isinstance(item, dict):
                evidence_lines.append(f"- {_bullet(item)}")
                continue
            if opportunities and _is_grants_gov_evidence(item):
                continue
            title = item.get("title") or item.get("evidence_id") or "Source"
            uri = item.get("source_uri")
            evidence_lines.append(f"- [{title}]({uri})" if uri else f"- {title}")
        if evidence_lines:
            lines.append("\n**Evidence**\n")
            lines.extend(evidence_lines)

    return "\n".join(line for line in lines if line is not None).strip()


def _agent_connector_ids(agent_name: str) -> tuple[str, ...]:
    """Connectors an agent may reach, from the governed catalog."""
    from research_assistant_core.connector_catalog import connector_definitions

    agent_id = agent_name.removesuffix("-agent")
    return tuple(
        connector.id
        for connector in connector_definitions()
        if agent_id in connector.assigned_agents
    )


def _ready_agent_connector_ids(
    agent_name: str,
    store: WorkspaceStore,
) -> tuple[str, ...]:
    configured = frozenset(_agent_connector_ids(agent_name))
    agent_id = agent_name.removesuffix("-agent")
    return tuple(
        connector.id
        for connector in store.connectors()
        if connector.id in configured
        and connector.enabled
        and connector.test_status in {"ready", "ready_with_key"}
        and agent_id in connector.assigned_agents
    )


def _require_ready_agent_connectors(
    thread: ChatThread,
    store: WorkspaceStore,
) -> None:
    from research_assistant_core.connector_catalog import connector_definitions

    agent_id = thread.agent_name.removesuffix("-agent")
    required = tuple(
        item
        for item in connector_definitions()
        if item.required and agent_id in item.assigned_agents
    )
    if not required:
        return
    configured = {item.id: item for item in store.connectors()}
    unavailable = [
        definition.name
        for definition in required
        if (
            (connector := configured.get(definition.id)) is None
            or not connector.required
            or not connector.enabled
            or agent_id not in connector.assigned_agents
            or connector.test_status not in {"ready", "ready_with_key"}
        )
    ]
    if unavailable:
        names = ", ".join(unavailable)
        noun = "connector" if len(unavailable) == 1 else "connectors"
        raise HTTPException(
            status_code=503,
            detail=(
                f"Required {noun} {names} is not ready for {thread.agent_name}. "
                "Test it in Project Settings, then retry."
            ),
        )


def _resolved_nothing(raw: str) -> bool:
    """True when a typed reply supported no claim at all."""
    payload = _final_payload(raw)
    if payload is None:
        return False
    if _verified_grant_opportunities(raw):
        return False
    claims = payload.get("claims")
    if not isinstance(claims, list):
        return False
    return not any(
        isinstance(claim, dict) and claim.get("support") == "supported" for claim in claims
    )


def _next_steps(thread: ChatThread, raw: str, *, evidence_count: int) -> str:
    """Name the route forward when a turn resolves nothing.

    Abstaining on absent evidence is correct, but on its own it is terminal: the
    researcher is told what could not be answered and nothing about what to do.
    """
    endpoint = endpoint_for(thread.agent_name)
    if endpoint is None:
        return ""
    if not _resolved_nothing(raw):
        return ""
    return "\n".join(
        [
            "**Next steps** — nothing in this turn could be supported.",
            "- Add sources in **Library** so the agent can search them, then ask again.",
            "- Or name the specific paper, metric, or population you want assessed.",
        ]
    )


def _contract_envelope(
    thread: ChatThread,
    *,
    store: WorkspaceStore,
    identity: IdentityContext,
    settings: Settings,
    text: str,
    attachments: list[ChatAttachment],
) -> str:
    # Agents pin themselves to this deployment's tenant/project and reject any other
    # scope, so the envelope carries the deployment scope; the caller is carried by
    # principal_id and the delegated identity header.
    envelope: dict[str, object] = {
        "query": text,
        "tenant_id": settings.workspace_tenant_id,
        "project_id": settings.workspace_project_id,
        "principal_id": identity.user_id,
        "session_id": thread.id,
        "sensitivity": _INTERNAL_SENSITIVITY,
    }
    connector_ids = _ready_agent_connector_ids(thread.agent_name, store)
    if connector_ids:
        envelope["authorized_connector_ids"] = list(connector_ids)
    if attachments:
        envelope["session_files"] = [
            {
                "evidence_id": f"file:{item.path}",
                "path": item.path,
                "content_type": item.content_type,
                "size_bytes": item.size_bytes,
            }
            for item in attachments
        ]
    if thread.capability == Capability.GRANT:
        opportunity_id = _grant_opportunity_id(thread, text)
        if opportunity_id is not None:
            envelope["opportunity_id"] = opportunity_id
    if thread.capability == Capability.DATASET:
        latest = attachments[-1] if attachments else None
        envelope["dataset_id"] = latest.path if latest else f"{thread.id}-session-dataset"
    return json.dumps(envelope, separators=(",", ":"))


_EXACT_GRANT_ID = re.compile(
    r"\bgrants\.gov\s+opportunity(?:\s+id)?\s+([0-9]{1,12})\b",
    re.IGNORECASE,
)
_GRANT_CONTEXT_REFERENCES = (
    "preceding request",
    "previous request",
    "that opportunity",
    "the same opportunity",
)


def _grant_opportunity_id(thread: ChatThread, text: str) -> str | None:
    exact = _EXACT_GRANT_ID.search(text)
    if exact is not None:
        return exact.group(1)
    normalized = text.casefold()
    if not any(marker in normalized for marker in _GRANT_CONTEXT_REFERENCES):
        return None
    prior_ids = {
        opportunity.grants_gov_id
        for message in thread.messages
        if message.role == "assistant"
        for opportunity in message.opportunities
    }
    return next(iter(prior_ids)) if len(prior_ids) == 1 else None


def build_agent_chat_gateway(settings: Settings) -> ChatGateway:
    """Compose the Foundry gateway for this deployment."""
    if not settings.foundry_project_endpoint:
        raise HostedAgentConfigurationError("FOUNDRY_PROJECT_ENDPOINT is required.")
    return AgentChatGateway(settings)


__all__ = [
    "AgentChatGateway",
    "AgentChoice",
    "ChatGateway",
    "ChatMessageView",
    "ChatThreadView",
    "build_agent_chat_gateway",
    "delegated_user_identity_for",
    "router",
]
