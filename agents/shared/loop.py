"""Deterministic sufficiency loop for governed agents.

The model never decides whether to iterate. A Python predicate parses the typed
response and re-invokes only while the response is *objectively improvable*:
either it failed the strict output contract, or it ignored a source the runtime
had already admitted for the turn.

An abstention over an empty evidence set is a correct terminal answer and must
never be looped on -- re-asking a model that has nothing to cite is how citation
fabrication is manufactured.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from agent_framework import AgentLoopMiddleware
from pydantic import ValidationError

from .contracts import AgentManifest, bind_contracts

_CONTRACT_GAP = (
    "Your previous response did not satisfy the output contract. Re-answer using "
    "the same sources, citing an evidence_id from the admitted sources for every supported claim."
)
_IGNORED_SOURCE_GAP = (
    "Sources were admitted for this turn but your previous response made no claim "
    "about them. Assess the supplied sources and answer, or state "
    "explicitly why it cannot support a claim."
)


def _message_text(message: Any) -> str:
    text = getattr(message, "text", None)
    return text if isinstance(text, str) else ""


def _admitted_source_count(manifest: AgentManifest, messages: Sequence[Any]) -> int:
    """Size of the source set the runtime admitted for this turn."""
    input_model = bind_contracts(manifest).input_model
    for message in reversed(list(messages)):
        try:
            return len(input_model.model_validate_json(_message_text(message)).evidence)
        except ValidationError:
            continue
    return 0


def _claim_count(raw: str) -> int | None:
    """Number of claims in a parseable typed reply, or ``None`` when it is not one."""
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or "summary" not in payload:
        return None
    claims = payload.get("claims")
    return len(claims) if isinstance(claims, list) else 0


def sufficiency_predicate(
    manifest: AgentManifest,
) -> Callable[..., tuple[bool, str | None]]:
    """Build the ``should_continue`` predicate for one manifest.

    Returns ``(continue, feedback)`` so the next iteration is told which gap to
    close rather than being nudged blindly.
    """
    output_model = bind_contracts(manifest).output_model

    def should_continue(
        *,
        last_result: Any,
        original_messages: Sequence[Any] = (),
        **_: Any,
    ) -> tuple[bool, str | None]:
        raw = getattr(last_result, "text", "") or ""
        try:
            output_model.model_validate_json(raw)
        except ValidationError:
            return True, _CONTRACT_GAP
        if _claim_count(raw) == 0 and _admitted_source_count(manifest, original_messages):
            return True, _IGNORED_SOURCE_GAP
        return False, None

    return should_continue


def revision_message(manifest: AgentManifest) -> Callable[..., str | None]:
    """Build the next iteration's input for one manifest.

    ``ContractMiddleware`` re-validates the typed input contract on every
    iteration, so a revision has to be a complete envelope -- bare feedback text
    would be rejected as a malformed invocation.
    """
    input_model = bind_contracts(manifest).input_model

    def next_message(
        *,
        original_messages: Sequence[Any] = (),
        feedback: str | None = None,
        **_: Any,
    ) -> str | None:
        if not feedback:
            return None
        for message in reversed(list(original_messages)):
            try:
                request = input_model.model_validate_json(_message_text(message))
            except ValidationError:
                continue
            payload = request.model_dump(mode="json")
            payload["query"] = f"{feedback}\n\n{request.query}"
            try:
                return input_model.model_validate(payload).model_dump_json()
            except ValidationError:
                return None
        return None

    return next_message


def _record_gap(*, feedback: str | None = None, **_: Any) -> str | None:
    """Log the terse gap, not the response.

    The framework default records ``last_result.text``, which for a contract-typed
    agent is the whole JSON payload -- replayed into every later iteration.
    """
    return feedback


def loop_middleware_for_manifest(manifest: AgentManifest) -> list[AgentLoopMiddleware]:
    """The loop, or nothing when the manifest does not enable one."""
    if not manifest.loop.enabled:
        return []
    return [
        AgentLoopMiddleware(
            sufficiency_predicate(manifest),
            max_iterations=manifest.loop.max_iterations,
            next_message=revision_message(manifest),
            record_feedback=_record_gap,
            # Callers of a contract-typed agent expect exactly one payload; the
            # aggregated form concatenates every iteration into ``.text``.
            return_final_only=True,
        )
    ]
