"""Unit tests verifying pipeline architecture, custom tools, HITL, async memory,
multi-model routing, structured JSON logging, PII redaction, and guardrails.

These tests are strictly deterministic (no LLM calls) so they run reliably in CI.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from debrief_agent.agent import app, debrief_pipeline, root_agent
from debrief_agent.callbacks import (
    enforce_no_hire_guardrail,
    finalize_pipeline_state,
    initialize_pipeline_state,
    log_tool_intent,
    log_tool_outcome,
    normalize_markdown_output,
    sanitize_hiring_verdicts,
)
from debrief_agent.logging_config import JsonLogFormatter
from debrief_agent.pii import redact_pii
from debrief_agent.sub_agents import (
    debrief_synthesizer,
    gap_auditor,
    rubric_toolset,
    signal_extractor,
)
from debrief_agent.tools import (
    export_debrief_artifact,
    recall_prior_debriefs,
    validate_interview_notes,
)


def test_pipeline_structure_and_multi_model_routing() -> None:
    """Verifies coordinator, 3-stage SequentialAgent, and tiered multi-model routing."""
    assert isinstance(root_agent, LlmAgent)
    assert root_agent.name == "debrief_coordinator"
    assert len(root_agent.tools) == 3
    assert root_agent.sub_agents == [debrief_pipeline]

    assert isinstance(debrief_pipeline, SequentialAgent)
    assert debrief_pipeline.name == "debrief_pipeline"
    assert app.name == "debrief_agent"
    assert [a.name for a in debrief_pipeline.sub_agents] == [
        "signal_extractor",
        "gap_auditor",
        "debrief_synthesizer",
    ]

    # Verify tiered multi-model routing: gap_auditor uses reasoning model (Pro)
    # while coordinator/extractor/synthesizer use fast model (Flash).
    assert signal_extractor.model.model != gap_auditor.model.model
    assert signal_extractor.model.model == debrief_synthesizer.model.model


def test_strict_tool_and_context_isolation() -> None:
    """Verifies only Stage 1 has SkillToolset and Stages 2-3 use isolated context."""
    assert len(signal_extractor.tools) == 1
    assert signal_extractor.output_key == "extracted_signals"
    assert signal_extractor.before_tool_callback is log_tool_intent
    assert signal_extractor.after_tool_callback is log_tool_outcome

    assert gap_auditor.tools == []
    assert gap_auditor.output_key == "audit_flags"
    assert gap_auditor.include_contents == "none"

    assert debrief_synthesizer.tools == []
    assert debrief_synthesizer.output_key == "final_debrief"
    assert debrief_synthesizer.include_contents == "none"


def test_rubric_skills_and_level_anchors_loaded() -> None:
    """Verifies all 3 rubric skills load L3/L4/L5 anchors and debrief templates."""
    expected_skills = {"system-design-rubric", "coding-rubric", "behavioral-rubric"}
    assert set(rubric_toolset._skills.keys()) == expected_skills

    for skill_name, skill in rubric_toolset._skills.items():
        assert skill.name == skill_name
        assert sorted(skill.resources.references.keys()) == [
            "l3-anchors.md",
            "l4-anchors.md",
            "l5-anchors.md",
        ]
        assert sorted(skill.resources.assets.keys()) == ["debrief-template.md"]


def test_validate_interview_notes_guided_errors_and_pii_detection() -> None:
    """Verifies custom tool validation, guided recovery hints, and PII scrubbing."""
    short_res = validate_interview_notes("too short", target_level="L4")
    assert short_res["status"] == "error"
    assert short_res["error_code"] == "NOTES_TOO_SHORT"
    assert "recovery_hint" in short_res

    bad_level = validate_interview_notes(
        "Candidate designed a distributed RAG pipeline with vector search and latency SLAs.",
        target_level="L9",
    )
    assert bad_level["status"] == "error"
    assert bad_level["error_code"] == "UNSUPPORTED_LEVEL"
    assert "L3, L4, and L5" in bad_level["recovery_hint"]

    good_res = validate_interview_notes(
        "Candidate Jane (jane@example.com, 555-123-4567) designed a multi-tenant RAG "
        "architecture with vector search and p95 latency targets.",
        target_level="l4",
    )
    assert good_res["status"] == "ok"
    assert good_res["normalized_level"] == "L4"
    assert "system-design-rubric" in good_res["detected_rubrics"]
    assert "EMAIL_ADDRESS" in good_res["pii_detected"]
    assert "PHONE_NUMBER" in good_res["pii_detected"]
    assert "jane@example.com" not in good_res["redacted_notes"]


@pytest.mark.asyncio
async def test_export_debrief_artifact_hitl_pause_and_approval() -> None:
    """Verifies HITL execution pause via request_confirmation, approval, and rejection."""
    sample_md = "# Debrief\nContact: alice@example.com\nHiring Recommendation: Strong Hire\n"

    # 1. First call without confirmation pauses execution via request_confirmation.
    ctx_pause = MagicMock()
    ctx_pause.tool_confirmation = None
    paused = await export_debrief_artifact(
        debrief_markdown=sample_md,
        candidate_label="cand-42",
        target_level="L4",
        tool_context=ctx_pause,
    )
    assert paused["status"] == "pending_confirmation"
    ctx_pause.request_confirmation.assert_called_once()

    # 2. Rejected confirmation cancels export cleanly.
    ctx_reject = MagicMock()
    ctx_reject.tool_confirmation = SimpleNamespace(confirmed=False)
    rejected = await export_debrief_artifact(
        debrief_markdown=sample_md,
        candidate_label="cand-42",
        target_level="L4",
        tool_context=ctx_reject,
    )
    assert rejected["status"] == "cancelled"

    # 3. Approved confirmation persists PII-redacted, guardrail-checked artifact.
    ctx_approve = MagicMock()
    ctx_approve.tool_confirmation = SimpleNamespace(confirmed=True)
    ctx_approve.save_artifact = AsyncMock(return_value=1)
    saved = await export_debrief_artifact(
        debrief_markdown=sample_md,
        candidate_label="cand-42",
        target_level="L4",
        tool_context=ctx_approve,
    )
    assert saved["status"] == "success"
    assert saved["artifact_name"] == "debrief_cand-42_l4.md"
    assert saved["version"] == 1
    ctx_approve.save_artifact.assert_awaited_once()
    saved_bytes = ctx_approve.save_artifact.call_args.kwargs["artifact"].inline_data.data
    saved_text = saved_bytes.decode("utf-8")
    assert "alice@example.com" not in saved_text
    assert "Strong Hire" not in saved_text


@pytest.mark.asyncio
async def test_async_memory_recall_and_persistence() -> None:
    """Verifies async recall_prior_debriefs and async finalize_pipeline_state."""
    # Empty query guided error
    err = await recall_prior_debriefs("   ")
    assert err["status"] == "error"
    assert err["error_code"] == "EMPTY_QUERY"

    # Successful async memory recall with PII redaction
    fake_memory = SimpleNamespace(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text="Prior L4 debrief for bob@example.com on RAG.")],
        )
    )
    ctx = MagicMock()
    ctx.search_memory = AsyncMock(return_value=SimpleNamespace(memories=[fake_memory]))
    res = await recall_prior_debriefs("L4 RAG debrief", tool_context=ctx)
    assert res["status"] == "ok"
    assert res["match_count"] == 1
    assert "bob@example.com" not in res["matches"][0]

    # Async finalize_pipeline_state calls add_session_to_memory & scrubs PII
    state: dict[str, object] = {
        "pipeline_run_count": 1,
        "final_debrief": "Debrief contact: secret@corp.com",
        "debrief_history": [],
    }
    cb_ctx = MagicMock()
    cb_ctx.agent_name = "debrief_pipeline"
    cb_ctx.state = state
    cb_ctx.add_session_to_memory = AsyncMock()
    await finalize_pipeline_state(cb_ctx)
    cb_ctx.add_session_to_memory.assert_awaited_once()
    assert "secret@corp.com" not in str(state["final_debrief"])


def test_json_log_formatter_and_pii_redaction() -> None:
    """Verifies dedicated JsonLogFormatter outputs valid JSON with PII scrubbed."""
    formatter = JsonLogFormatter(service_name="debrief-test")
    record = logging.LogRecord(
        name="debrief_agent.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Processing notes for user@example.com phone 555-867-5309",
        args=(),
        exc_info=None,
    )
    record.event = "tool_intent"
    record.tool_args = {"email": "user@example.com"}

    rendered = formatter.format(record)
    parsed = json.loads(rendered)
    assert parsed["severity"] == "INFO"
    assert parsed["service"] == "debrief-test"
    assert "user@example.com" not in parsed["message"]
    assert "555-867-5309" not in parsed["message"]
    assert "user@example.com" not in json.dumps(parsed["attributes"])

    redacted_ssn, types_found = redact_pii("SSN 123-45-6789")
    assert "123-45-6789" not in redacted_ssn
    assert "US_SOCIAL_SECURITY_NUMBER" in types_found


def test_no_hire_guardrail_redacts_prohibited_verdicts() -> None:
    """Verifies deterministic guardrail redacts explicit Hire/No-Hire verdicts."""
    bad_output = (
        "# Candidate Debrief — System Design (L5)\n\n"
        "## Executive Summary\nStrong technical trade-offs.\n\n"
        "Hiring Recommendation: Strong Hire\n"
        "We recommend hiring this candidate.\n"
    )
    sanitized, triggered = sanitize_hiring_verdicts(bad_output)
    assert triggered is True
    assert "Strong Hire" not in sanitized
    assert "REDACTED BY GUARDRAIL" in sanitized

    ctx = SimpleNamespace(agent_name="debrief_synthesizer", state={})
    resp = LlmResponse(
        content=types.Content(role="model", parts=[types.Part.from_text(text=bad_output)])
    )
    rewritten = enforce_no_hire_guardrail(ctx, resp)  # type: ignore[arg-type]
    assert rewritten is not None
    assert rewritten.content is not None
    assert "REDACTED BY GUARDRAIL" in (rewritten.content.parts[0].text or "")


def test_no_hire_guardrail_allows_clean_debrief() -> None:
    """Verifies compliant debriefs pass through untouched without false positives."""
    clean_output = (
        "# Candidate Debrief — System Design (L5)\n\n"
        "## Executive Summary\n"
        "Candidate demonstrated independent hybrid-search trade-off analysis.\n\n"
        "## Core Competency Ratings\n"
        "- **Writing Code:** Poor\n"
        "  - **Evidence:** No executable Python code written beyond function signature.\n"
        "- **System Design:** Meets Bar\n"
        "  - **Evidence:** Independently compared hybrid vs dense retrieval.\n"
        "- **Technical Problem-Solving:** Developing\n"
        "  - **Evidence:** Required hints on rate limiting.\n\n"
        "## Suggested Follow-Up Questions\n"
        "1. How would you enforce tenant ACLs during vector retrieval?\n\n"
        "## Level-Fit Conclusion (L5)\n"
        "- **Observed Level Alignment:** Partial / Mixed Alignment\n"
        "- **Summary Rationale:** Strong independent retrieval trade-offs, but missing security/governance signal must be gathered before a human hiring decision.\n"
    )
    sanitized, triggered = sanitize_hiring_verdicts(clean_output)
    assert triggered is False
    assert sanitized == clean_output


def test_initialize_pipeline_state_sets_defaults_safely() -> None:
    """Verifies before_agent_callback initializes state keys and increments run count."""
    state: dict[str, object] = {}
    ctx = SimpleNamespace(agent_name="debrief_pipeline", state=state)

    initialize_pipeline_state(ctx)  # type: ignore[arg-type]
    assert state["extracted_signals"] == ""
    assert state["audit_flags"] == ""
    assert state["final_debrief"] == ""
    assert state["pipeline_run_count"] == 1
    assert state["debrief_history"] == []

    state["debrief_history"] = [{"run": 1, "skill": "system-design-rubric"}]
    initialize_pipeline_state(ctx)  # type: ignore[arg-type]
    assert state["pipeline_run_count"] == 2
    assert len(state["debrief_history"]) == 1  # type: ignore[arg-type]


def test_normalize_markdown_output_fixes_broken_fences_and_dividers() -> None:
    """Verifies inline multi-backticks and duplicate horizontal rules are normalized."""
    raw = (
        "# Debrief\n\n"
        '1. Parse markdown JSON (e.g., ````json {"k": 1} ````) cleanly.\n'
        "---\n"
        "---\n\n\n"
        "## Conclusion\n"
    )
    cleaned = normalize_markdown_output(raw)
    assert "````" not in cleaned
    assert '`json {"k": 1}`' in cleaned
    assert "---\n---" not in cleaned
    assert "\n\n\n" not in cleaned
