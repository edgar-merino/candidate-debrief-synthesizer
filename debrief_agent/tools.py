"""Custom Python ADK tools with comprehensive docstrings, Pydantic validation,
guided error handling, asynchronous memory recall, and HITL confirmation.
"""

from __future__ import annotations

import re
from typing import Any

from google.adk.tools.tool_context import ToolContext
from google.genai import types
from pydantic import BaseModel, Field, ValidationError

from debrief_agent.callbacks import sanitize_hiring_verdicts
from debrief_agent.pii import redact_pii

SUPPORTED_LEVELS = frozenset({"L3", "L4", "L5"})
SUPPORTED_INTERVIEW_TYPES = frozenset({"system_design", "coding", "behavioral"})
MIN_NOTES_CHAR_LENGTH = 40

_ROUND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "system_design": (
        "architecture",
        "system design",
        "rag",
        "vector",
        "latency",
        "qps",
        "database",
        "scaling",
        "ingestion",
        "pipeline",
        "microservices",
    ),
    "coding": (
        "coding",
        "python",
        "algorithm",
        "pseudocode",
        "function",
        "regex",
        "complexity",
        "edge case",
        "parse",
        "hash map",
        "data structure",
    ),
    "behavioral": (
        "behavioral",
        "leadership",
        "conflict",
        "stakeholder",
        "star",
        "mentoring",
        "cross-functional",
        "ownership",
        "feedback",
    ),
}


class ValidateNotesInput(BaseModel):
    """Validated input schema for interview notes pre-flight checks."""

    notes: str = Field(
        ...,
        description="Raw, unstructured technical interview notes to validate.",
    )
    target_level: str = Field(
        default="L5",
        description="Target engineering level: L3, L4, or L5.",
    )
    interview_type: str = Field(
        default="",
        description="Optional round type: system_design, coding, or behavioral.",
    )


def validate_interview_notes(
    notes: str,
    target_level: str = "L5",
    interview_type: str = "",
) -> dict[str, Any]:
    """Validates raw interview notes before running the 3-stage debrief pipeline.

    Performs pre-flight schema validation, detects supported seniority levels
    (L3/L4/L5), infers which rubric skill(s) apply, and scans for candidate PII
    (emails, phone numbers, SSNs, secrets) that should be redacted.

    Args:
        notes: Raw interview notes pasted by the interviewer (minimum 40 chars).
        target_level: Desired evaluation baseline level. Must be one of
            ``"L3"``, ``"L4"``, or ``"L5"`` (case-insensitive). Defaults to ``"L5"``.
        interview_type: Optional explicit interview round type (``"system_design"``,
            ``"coding"``, or ``"behavioral"``). Leave empty to auto-detect.

    Returns:
        A dictionary containing:
        - ``status``: ``"ok"`` when valid, or ``"error"`` on validation failure.
        - ``normalized_level``: Canonical level (``"L3"`` | ``"L4"`` | ``"L5"``).
        - ``detected_rubrics``: List of matching ADK skill names to activate.
        - ``pii_detected``: List of PII categories detected and redacted in preview.
        - ``redacted_notes``: PII-scrubbed notes safe to pass downstream.
        - ``error_code`` & ``recovery_hint`` (only on ``status == "error"``):
          Structured guidance explaining exactly how the agent or user should fix
          the input and retry.
    """
    try:
        parsed = ValidateNotesInput(
            notes=notes,
            target_level=target_level,
            interview_type=interview_type,
        )
    except ValidationError as exc:
        return {
            "status": "error",
            "error_code": "INVALID_SCHEMA",
            "message": f"Input failed schema validation: {exc.errors()[0]['msg']}",
            "recovery_hint": (
                "Ensure `notes` is a non-empty string, `target_level` is L3/L4/L5, "
                "and `interview_type` is system_design, coding, behavioral, or empty."
            ),
        }

    cleaned_notes = parsed.notes.strip()
    if len(cleaned_notes) < MIN_NOTES_CHAR_LENGTH:
        return {
            "status": "error",
            "error_code": "NOTES_TOO_SHORT",
            "message": (
                f"Interview notes are too short ({len(cleaned_notes)} chars; "
                f"minimum is {MIN_NOTES_CHAR_LENGTH} chars) to produce a grounded debrief."
            ),
            "recovery_hint": (
                "Ask the interviewer to paste concrete observations, candidate quotes, "
                "or question/answer shorthand before invoking debrief_pipeline."
            ),
        }

    normalized_level = parsed.target_level.strip().upper()
    if normalized_level not in SUPPORTED_LEVELS:
        return {
            "status": "error",
            "error_code": "UNSUPPORTED_LEVEL",
            "message": f"Unsupported target level '{parsed.target_level}'.",
            "recovery_hint": (
                "Supported baseline levels are L3, L4, and L5. Ask the user to "
                "confirm one of L3, L4, or L5 and re-call validate_interview_notes."
            ),
        }

    normalized_type = parsed.interview_type.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized_type and normalized_type not in SUPPORTED_INTERVIEW_TYPES:
        return {
            "status": "error",
            "error_code": "UNSUPPORTED_INTERVIEW_TYPE",
            "message": f"Unsupported interview_type '{parsed.interview_type}'.",
            "recovery_hint": (
                "Use one of 'system_design', 'coding', 'behavioral', or pass an "
                "empty string to auto-detect from the notes."
            ),
        }

    lower_notes = cleaned_notes.lower()
    detected_rounds: list[str] = []
    if normalized_type:
        detected_rounds.append(normalized_type)
    else:
        for round_name, keywords in _ROUND_KEYWORDS.items():
            if any(kw in lower_notes for kw in keywords):
                detected_rounds.append(round_name)

    rubric_map = {
        "system_design": "system-design-rubric",
        "coding": "coding-rubric",
        "behavioral": "behavioral-rubric",
    }
    detected_rubrics = [rubric_map[r] for r in detected_rounds]

    redacted_notes, pii_types = redact_pii(cleaned_notes)

    return {
        "status": "ok",
        "normalized_level": normalized_level,
        "detected_rubrics": detected_rubrics,
        "word_count": len(cleaned_notes.split()),
        "pii_detected": pii_types,
        "redacted_notes": redacted_notes,
        "guidance": (
            "Notes validated. Transfer to `debrief_pipeline` to synthesize the debrief."
            if detected_rubrics
            else "Notes validated, but interview type was ambiguous. Ask the user to "
            "confirm whether this is System Design, Coding, or Behavioral before running."
        ),
    }


async def recall_prior_debriefs(
    query: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Asynchronously searches long-term memory for prior debriefs or rubric calibrations.

    Uses ADK's asynchronous ``ToolContext.search_memory`` API (backed by
    ``VertexAiMemoryBankService`` in Agent Runtime or ``InMemoryMemoryService``
    locally) to recall past candidate debrief patterns, calibration notes, or
    earlier interview rounds across sessions.

    Args:
        query: Natural-language search query (e.g., ``"L4 system design RAG gaps"``
            or ``"prior coding debrief calibration"``).
        tool_context: ADK-injected tool context providing access to memory service.

    Returns:
        A dictionary containing:
        - ``status``: ``"ok"``, ``"empty"``, or ``"error"``.
        - ``matches``: Up to 5 PII-redacted memory snippets matching the query.
        - ``recovery_hint`` (on ``"empty"`` or ``"error"``): Actionable guidance
          so the agent can inform the user gracefully without hallucinating.
    """
    cleaned_query = query.strip()
    if not cleaned_query:
        return {
            "status": "error",
            "error_code": "EMPTY_QUERY",
            "message": "Memory search query cannot be empty.",
            "recovery_hint": (
                "Provide a descriptive query naming the interview type, level, or "
                "competency you want to recall (e.g., 'L4 system design debrief')."
            ),
        }

    if tool_context is None:
        return {
            "status": "error",
            "error_code": "MISSING_TOOL_CONTEXT",
            "message": "ToolContext is unavailable for memory lookup.",
            "recovery_hint": "Continue using only the current session's debrief state.",
        }

    try:
        response = await tool_context.search_memory(cleaned_query)
    except ValueError as exc:
        return {
            "status": "error",
            "error_code": "MEMORY_SERVICE_UNAVAILABLE",
            "message": f"Long-term memory service is not configured: {exc}",
            "recovery_hint": (
                "Inform the user that cross-session memory is not enabled in this "
                "runtime and answer using the current session's `{final_debrief}`."
            ),
        }
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "MEMORY_SEARCH_FAILED",
            "message": f"Async memory search failed: {type(exc).__name__}",
            "recovery_hint": (
                "Retry with a shorter query or proceed using current session context."
            ),
        }

    memories = getattr(response, "memories", None) or []
    snippets: list[str] = []
    for entry in memories[:5]:
        content = getattr(entry, "content", None)
        if content and getattr(content, "parts", None):
            text = " ".join(p.text for p in content.parts if getattr(p, "text", None))
            if text:
                redacted, _ = redact_pii(text[:600])
                snippets.append(redacted)

    if not snippets:
        return {
            "status": "empty",
            "matches": [],
            "recovery_hint": (
                "No prior debrief memories matched this query. Proceed using the "
                "current session notes or ask the interviewer for additional context."
            ),
        }

    return {
        "status": "ok",
        "match_count": len(snippets),
        "matches": snippets,
    }


async def export_debrief_artifact(
    debrief_markdown: str,
    candidate_label: str = "candidate",
    target_level: str = "L5",
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Exports a finalized debrief as a versioned ADK artifact with mandatory HITL approval.

    Implements an explicit **Human-in-the-Loop (HITL)** execution pause using
    ``tool_context.request_confirmation(...)`` so a human interviewer must review
    and explicitly approve the PII-redacted, guardrail-checked debrief before it
    is persisted to the artifact store (GCS in production, in-memory locally).

    Args:
        debrief_markdown: Complete Markdown debrief text to export. Must be
            non-empty and free of prohibited Hire/No-Hire verdicts.
        candidate_label: Short, non-PII identifier for the filename (e.g.,
            ``"candidate_042"``). Non-alphanumeric characters are sanitized.
        target_level: Evaluation baseline level (``"L3"``, ``"L4"``, or ``"L5"``).
        tool_context: ADK-injected tool context for HITL confirmation and artifact I/O.

    Returns:
        A dictionary containing:
        - ``status``: ``"pending_confirmation"`` (paused for human approval),
          ``"success"`` (artifact saved), ``"cancelled"`` (rejected by human),
          or ``"error"`` (with ``error_code`` and ``recovery_hint``).
        - ``artifact_name`` & ``version`` (on ``status == "success"``).
    """
    cleaned_md = debrief_markdown.strip()
    if not cleaned_md:
        return {
            "status": "error",
            "error_code": "EMPTY_DEBRIEF",
            "message": "Cannot export an empty debrief.",
            "recovery_hint": (
                "Generate a debrief via `debrief_pipeline` first, then pass the "
                "final Markdown debrief to `export_debrief_artifact`."
            ),
        }

    normalized_level = target_level.strip().upper()
    if normalized_level not in SUPPORTED_LEVELS:
        return {
            "status": "error",
            "error_code": "UNSUPPORTED_LEVEL",
            "message": f"Unsupported target_level '{target_level}'.",
            "recovery_hint": "Pass one of 'L3', 'L4', or 'L5' as target_level.",
        }

    # Enforce both the no-Hire guardrail and active PII redaction before export.
    sanitized_md, verdict_redacted = sanitize_hiring_verdicts(cleaned_md)
    redacted_md, pii_types = redact_pii(sanitized_md)

    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", candidate_label.strip()).strip("_") or "candidate"
    filename = f"debrief_{safe_label}_{normalized_level.lower()}.md"

    if tool_context is None:
        return {
            "status": "error",
            "error_code": "MISSING_TOOL_CONTEXT",
            "message": "ToolContext is required for HITL confirmation and artifact export.",
            "recovery_hint": "Invoke this tool through an ADK Runner with an artifact service.",
        }

    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        tool_context.request_confirmation(
            hint=(
                f"Approve exporting sanitized {normalized_level} debrief artifact "
                f"'{filename}'? (PII types scrubbed: {pii_types or 'none'}, "
                f"hiring verdict guardrail triggered: {verdict_redacted})"
            ),
            payload={
                "filename": filename,
                "target_level": normalized_level,
                "pii_redacted_types": pii_types,
                "verdict_guardrail_triggered": verdict_redacted,
                "char_count": len(redacted_md),
            },
        )
        return {
            "status": "pending_confirmation",
            "artifact_name": filename,
            "message": (
                f"Execution paused: awaiting explicit human interviewer confirmation "
                f"to export '{filename}'."
            ),
        }

    if not confirmation.confirmed:
        return {
            "status": "cancelled",
            "artifact_name": filename,
            "message": "Debrief export cancelled by human reviewer.",
        }

    try:
        artifact_part = types.Part.from_bytes(
            data=redacted_md.encode("utf-8"),
            mime_type="text/markdown",
        )
        version = await tool_context.save_artifact(filename=filename, artifact=artifact_part)
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "ARTIFACT_SAVE_FAILED",
            "message": f"Failed to persist artifact '{filename}': {type(exc).__name__}",
            "recovery_hint": (
                "Verify the configured artifact service (GCS bucket or in-memory) "
                "and retry export."
            ),
        }

    return {
        "status": "success",
        "artifact_name": filename,
        "version": version,
        "pii_redacted_types": pii_types,
        "message": f"Approved debrief exported as '{filename}' (version {version}).",
    }
