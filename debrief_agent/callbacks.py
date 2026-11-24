"""Observability, context/memory management, PII redaction, and guardrail callbacks.

Provides:
- Dedicated structured JSON logging with OTel trace/span correlation & active PII redaction
- Safe session state initialization & async memory persistence (`add_session_to_memory`)
- Explicit intent-vs-outcome tool call logging (`before_tool_callback` + `after_tool_callback`)
- Per-stage latency tracing on OpenTelemetry spans
- Deterministic after-model guardrail enforcing the no Hire/No-Hire policy & Markdown hygiene
"""

from __future__ import annotations

import re
import time
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types
from opentelemetry import trace

from debrief_agent.logging_config import configure_structured_logging
from debrief_agent.pii import redact_payload, redact_pii

logger = configure_structured_logging("debrief_agent.observability")

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
    """Initializes required session state keys before the pipeline runs."""
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


async def finalize_pipeline_state(callback_context: CallbackContext) -> None:
    """Records latency, scrubs PII, and persists session to long-term memory asynchronously."""
    state = callback_context.state
    start_ts = state.get("_pipeline_start_ts")
    duration_ms = (
        round((time.perf_counter() - float(start_ts)) * 1000, 2)
        if start_ts is not None
        else None
    )

    # Scrub PII from stored debrief state before persisting to memory/history.
    if isinstance(state.get("final_debrief"), str) and state["final_debrief"]:
        redacted_debrief, pii_types = redact_pii(state["final_debrief"])
        if pii_types:
            state["final_debrief"] = redacted_debrief
            logger.info(
                "Redacted PII from final_debrief prior to memory persistence",
                extra={"event": "pii_redacted", "pii_types": pii_types},
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

    memory_persisted = False
    if hasattr(callback_context, "add_session_to_memory"):
        try:
            await callback_context.add_session_to_memory()
            memory_persisted = True
        except ValueError:
            # Memory service not configured on this runner (e.g., lightweight unit test).
            memory_persisted = False
        except Exception as exc:
            logger.warning(
                "Async memory persistence skipped: %s",
                type(exc).__name__,
                extra={"event": "memory_persist_error", "error": str(exc)},
            )

    span = trace.get_current_span()
    if duration_ms is not None:
        span.set_attribute("debrief.pipeline.duration_ms", duration_ms)
    if skill := state.get("active_rubric_skill"):
        span.set_attribute("debrief.skill.active", str(skill))
    span.set_attribute("debrief.memory.persisted", memory_persisted)

    logger.info(
        "Debrief pipeline completed",
        extra={
            "event": "pipeline_end",
            "agent_name": callback_context.agent_name,
            "active_skill": state.get("active_rubric_skill", "unknown"),
            "duration_ms": duration_ms,
            "memory_persisted": memory_persisted,
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


def log_tool_intent(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
) -> None:
    """Logs explicit tool-call intent before execution (intent-vs-outcome tracing)."""
    tool_name = tool.name
    tool_context.state[f"_tool_start_{tool_name}"] = time.perf_counter()

    safe_args = redact_payload({k: str(v)[:200] for k, v in args.items()})
    span = trace.get_current_span()
    span.set_attribute("debrief.tool.intent.name", tool_name)

    logger.info(
        "Tool call intent recorded",
        extra={
            "event": "tool_intent",
            "tool_name": tool_name,
            "tool_args": safe_args,
        },
    )


def log_tool_outcome(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: ToolContext,
    tool_response: dict[str, Any] | Any,
) -> None:
    """Logs tool execution outcome, duration, and SkillToolset state after execution."""
    tool_name = tool.name
    start_ts = tool_context.state.get(f"_tool_start_{tool_name}")
    duration_ms = (
        round((time.perf_counter() - float(start_ts)) * 1000, 2)
        if start_ts is not None
        else None
    )

    outcome_status = (
        str(tool_response.get("status", "ok"))
        if isinstance(tool_response, dict)
        else "ok"
    )
    span = trace.get_current_span()
    span.set_attribute("debrief.tool.name", tool_name)
    span.set_attribute("debrief.tool.outcome.status", outcome_status)
    if duration_ms is not None:
        span.set_attribute("debrief.tool.duration_ms", duration_ms)

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
        "Tool call outcome recorded",
        extra={
            "event": "tool_outcome",
            "tool_name": tool_name,
            "outcome_status": outcome_status,
            "duration_ms": duration_ms,
        },
    )


# Backwards-compatible alias for earlier imports.
trace_skill_tool_call = log_tool_outcome


def enforce_no_hire_guardrail(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> LlmResponse | None:
    """Deterministic guardrail ensuring no Hire/No-Hire verdict & clean Markdown."""
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
