"""Observability, context/state management, and deterministic guardrail callbacks.

Provides:
- Safe session state initialization & multi-turn debrief history tracking
- Per-stage structured logging and OpenTelemetry span enrichment
- Skill/resource usage tracking for SkillToolset calls
- Deterministic after-model guardrail enforcing the no Hire/No-Hire policy
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from opentelemetry import trace

logger = logging.getLogger("debrief_agent.observability")

# Patterns matching explicit hiring verdict/recommendation statements while
# avoiding false positives on policy disclaimers (e.g., "No Hire/No-Hire
# recommendation is provided").
_PROHIBITED_VERDICT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?im)^[ \t]*[-*#]*[ \t]*(?:overall[ \t]+)?(?:hiring[ \t]+)?"
        r"(?:recommendation|verdict|decision|rating)[ \t]*:[ \t]*.*?"
        r"\b(?:strong[ \t]+hire|lean[ \t]+hire|no[\s-]+hire|hire|reject)\b.*$"
    ),
    re.compile(
        r"(?im)^[ \t]*[-*#]*[ \t]*\*\*(?:strong[ \t]+hire|lean[ \t]+hire|"
        r"no[\s-]+hire|hire)\*\*[ \t]*$"
    ),
    re.compile(
        r"(?i)\b(?:i|we)[ \t]+recommend[ \t]+(?:hiring|a[ \t]+(?:strong[ \t]+|"
        r"lean[ \t]+)?(?:no[\s-]+)?hire|not[ \t]+hiring)\b"
    ),
)

_REDACTION_NOTICE = (
    "[REDACTED BY GUARDRAIL: Hiring recommendations are reserved exclusively "
    "for human interviewers.]"
)

# Inline triple/quadruple backtick snippets inside prose or list items often
# break Markdown renderers (including Gemini Enterprise). Convert them to safe
# single-backtick inline code while leaving standalone fenced code blocks intact.
_INLINE_MULTI_BACKTICK_RE = re.compile(r"(?<!\n)(`{3,4})([^\n`]+?)\1")
_DUPLICATE_HR_RE = re.compile(r"(?m)(?:^[ \t]*---[ \t]*\n){2,}")
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


def normalize_markdown_output(text: str) -> str:
    """Normalizes generated Markdown so it renders cleanly across ADK Web and Gemini."""
    cleaned = _INLINE_MULTI_BACKTICK_RE.sub(lambda m: f"`{m.group(2).strip()}`", text)
    cleaned = _DUPLICATE_HR_RE.sub("---\n", cleaned)
    cleaned = _EXCESS_BLANK_LINES_RE.sub("\n\n", cleaned)
    return cleaned.strip()


def sanitize_hiring_verdicts(text: str) -> tuple[str, bool]:
    """Removes prohibited hiring verdict statements from generated debrief text.

    Returns:
        A tuple of (sanitized_text, guardrail_triggered).
    """
    sanitized = text
    triggered = False
    for pattern in _PROHIBITED_VERDICT_PATTERNS:
        updated, count = pattern.subn(_REDACTION_NOTICE, sanitized)
        if count > 0:
            triggered = True
            sanitized = updated
    return sanitized, triggered


def initialize_session_state(callback_context: CallbackContext) -> None:
    """Safely initializes session state keys on the root coordinator before turn 1.

    Prevents KeyError when conversational instructions reference {final_debrief}
    or {audit_flags} prior to the first pipeline run.
    """
    state = callback_context.state
    state.setdefault("extracted_signals", "")
    state.setdefault("audit_flags", "")
    state.setdefault("final_debrief", "")
    state.setdefault("loaded_resources", [])
    state.setdefault("debrief_history", [])


def initialize_pipeline_state(callback_context: CallbackContext) -> None:
    """Initializes required session state keys before the pipeline runs.

    Prevents template KeyErrors on downstream state placeholders and tracks
    multi-turn debrief metadata within the session.
    """
    state = callback_context.state
    state.setdefault("extracted_signals", "")
    state.setdefault("audit_flags", "")
    state.setdefault("final_debrief", "")
    state.setdefault("loaded_resources", [])
    state.setdefault("debrief_history", [])
    state["pipeline_run_count"] = int(state.get("pipeline_run_count", 0)) + 1
    state["_pipeline_start_ts"] = time.perf_counter()

    span = trace.get_current_span()
    span.set_attribute("debrief.pipeline.run_count", state["pipeline_run_count"])

    logger.info(
        "Debrief pipeline started",
        extra={
            "event": "pipeline_start",
            "agent_name": callback_context.agent_name,
            "run_count": state["pipeline_run_count"],
        },
    )


def finalize_pipeline_state(callback_context: CallbackContext) -> None:
    """Records end-to-end latency and appends a compact summary to session history."""
    state = callback_context.state
    start_ts = state.get("_pipeline_start_ts")
    duration_ms = (
        round((time.perf_counter() - float(start_ts)) * 1000, 2)
        if start_ts is not None
        else None
    )

    history = list(state.get("debrief_history", []))
    history.append(
        {
            "run": state.get("pipeline_run_count", 1),
            "skill": state.get("active_rubric_skill", "unknown"),
            "resources": list(state.get("loaded_resources", [])),
            "duration_ms": duration_ms,
        }
    )
    # Cap in-session history to the 10 most recent debriefs to bound state size.
    state["debrief_history"] = history[-10:]

    span = trace.get_current_span()
    if duration_ms is not None:
        span.set_attribute("debrief.pipeline.duration_ms", duration_ms)
    if skill := state.get("active_rubric_skill"):
        span.set_attribute("debrief.skill.active", str(skill))

    logger.info(
        "Debrief pipeline completed",
        extra={
            "event": "pipeline_end",
            "agent_name": callback_context.agent_name,
            "active_skill": state.get("active_rubric_skill", "unknown"),
            "duration_ms": duration_ms,
        },
    )


def record_stage_start(callback_context: CallbackContext) -> None:
    """Records stage start time and attaches stage metadata to the active OTel span."""
    stage = callback_context.agent_name
    callback_context.state[f"_stage_start_{stage}"] = time.perf_counter()

    span = trace.get_current_span()
    span.set_attribute("debrief.stage.name", stage)

    logger.info(
        "Stage started",
        extra={"event": "stage_start", "stage": stage},
    )


def record_stage_end(callback_context: CallbackContext) -> None:
    """Records per-stage duration on the active OTel span and structured logs."""
    stage = callback_context.agent_name
    start_ts = callback_context.state.get(f"_stage_start_{stage}")
    duration_ms = (
        round((time.perf_counter() - float(start_ts)) * 1000, 2)
        if start_ts is not None
        else None
    )

    span = trace.get_current_span()
    if duration_ms is not None:
        span.set_attribute(f"debrief.stage.{stage}.duration_ms", duration_ms)

    logger.info(
        "Stage completed",
        extra={"event": "stage_end", "stage": stage, "duration_ms": duration_ms},
    )


def trace_skill_tool_call(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: dict[str, Any],
) -> None:
    """Tracks SkillToolset invocations in session state and OpenTelemetry spans."""
    span = trace.get_current_span()
    tool_name = tool.name
    span.set_attribute("debrief.tool.name", tool_name)

    if tool_name == "load_skill" and (skill_name := args.get("skill_name")):
        tool_context.state["active_rubric_skill"] = skill_name
        span.set_attribute("debrief.skill.loaded", str(skill_name))
    elif tool_name == "load_skill_resource":
        resource_path = args.get("file_path") or args.get("resource_path") or ""
        if resource_path:
            loaded = list(tool_context.state.get("loaded_resources", []))
            if resource_path not in loaded:
                loaded.append(str(resource_path))
            tool_context.state["loaded_resources"] = loaded
            span.set_attribute("debrief.skill.resource_loaded", str(resource_path))

    logger.info(
        "Skill tool executed",
        extra={
            "event": "skill_tool_call",
            "tool_name": tool_name,
            "tool_args": {k: str(v) for k, v in args.items()},
        },
    )


def enforce_no_hire_guardrail(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse | None:
    """Deterministic guardrail ensuring the synthesizer never emits a Hire/No-Hire verdict."""
    if not llm_response.content or not llm_response.content.parts:
        return None

    modified = False
    guardrail_triggered = False
    new_parts: list[types.Part] = []
    for part in llm_response.content.parts:
        if part.text:
            sanitized, triggered = sanitize_hiring_verdicts(part.text)
            normalized = normalize_markdown_output(sanitized)
            if triggered:
                guardrail_triggered = True
            if triggered or normalized != part.text:
                modified = True
                new_parts.append(types.Part.from_text(text=normalized))
                continue
        new_parts.append(part)

    span = trace.get_current_span()
    span.set_attribute("debrief.guardrail.no_hire_triggered", guardrail_triggered)

    if not modified:
        return None

    if guardrail_triggered:
        logger.warning(
            "No-Hire guardrail redacted a prohibited verdict in synthesizer output",
            extra={
                "event": "guardrail_triggered",
                "agent_name": callback_context.agent_name,
            },
        )
    return LlmResponse(
        content=types.Content(
            role=llm_response.content.role or "model",
            parts=new_parts,
        ),
        grounding_metadata=llm_response.grounding_metadata,
        partial=llm_response.partial,
        turn_complete=llm_response.turn_complete,
        interrupted=llm_response.interrupted,
        usage_metadata=llm_response.usage_metadata,
    )
