"""Unit tests verifying pipeline architecture, tool isolation, skills, and guardrails.

These tests are strictly deterministic (no LLM calls) so they run reliably in CI.
"""

from types import SimpleNamespace

from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.models.llm_response import LlmResponse
from google.genai import types

from debrief_agent.agent import app, debrief_pipeline, root_agent
from debrief_agent.callbacks import (
    enforce_no_hire_guardrail,
    initialize_pipeline_state,
    initialize_session_state,
    normalize_markdown_output,
    sanitize_hiring_verdicts,
)
from debrief_agent.sub_agents import (
    debrief_synthesizer,
    gap_auditor,
    rubric_toolset,
    signal_extractor,
)


def test_pipeline_structure_and_order() -> None:
    """Verifies interactive coordinator + deterministic 3-stage SequentialAgent wiring."""
    assert isinstance(root_agent, LlmAgent)
    assert root_agent.name == "debrief_coordinator"
    assert root_agent.tools == []
    assert root_agent.sub_agents == [debrief_pipeline]

    assert isinstance(debrief_pipeline, SequentialAgent)
    assert debrief_pipeline.name == "debrief_pipeline"
    assert app.name == "debrief_agent"
    assert [a.name for a in debrief_pipeline.sub_agents] == [
        "signal_extractor",
        "gap_auditor",
        "debrief_synthesizer",
    ]


def test_strict_tool_and_context_isolation() -> None:
    """Verifies only Stage 1 has SkillToolset and Stages 2-3 use isolated context."""
    assert len(signal_extractor.tools) == 1
    assert signal_extractor.output_key == "extracted_signals"

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

    # Verify callback wrapper rewrites LlmResponse cleanly
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

    # Second run preserves history and increments run counter
    state["debrief_history"] = [{"run": 1, "skill": "system-design-rubric"}]
    initialize_pipeline_state(ctx)  # type: ignore[arg-type]
    assert state["pipeline_run_count"] == 2
    assert len(state["debrief_history"]) == 1  # type: ignore[arg-type]


def test_normalize_markdown_output_fixes_broken_fences_and_dividers() -> None:
    """Verifies inline multi-backticks and duplicate horizontal rules are normalized."""
    raw = (
        "# Debrief\n\n"
        "1. Parse markdown JSON (e.g., ````json {\"k\": 1} ````) cleanly.\n"
        "---\n"
        "---\n\n\n"
        "## Conclusion\n"
    )
    cleaned = normalize_markdown_output(raw)
    assert "````" not in cleaned
    assert '`json {"k": 1}`' in cleaned
    assert "---\n---" not in cleaned
    assert "\n\n\n" not in cleaned

