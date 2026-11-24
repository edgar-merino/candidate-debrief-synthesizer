"""Sub-agents for the Candidate Debrief & Rubric Synthesizer pipeline.

Implements strict tool isolation and context hygiene across three sequential stages:
1. signal_extractor: Uses SkillToolset to load rubric anchors & templates.
2. gap_auditor: Pure reasoning over {extracted_signals} (no tools, isolated context).
3. debrief_synthesizer: Pure formatting over {extracted_signals} & {audit_flags}
   (no tools, isolated context, deterministic no-Hire/No-Hire guardrail).
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.models import Gemini
from google.adk.skills import load_skill_from_dir
from google.adk.tools.skill_toolset import SkillToolset
from google.genai import types

from debrief_agent.callbacks import (
    enforce_no_hire_guardrail,
    record_stage_end,
    record_stage_start,
    trace_skill_tool_call,
)

load_dotenv()

DEFAULT_MODEL = "gemini-3.8-flash"
SKILLS_DIR = Path(__file__).parent / "skills"


def _build_model() -> Gemini:
    """Builds the configured Gemini model from ADK_MODEL (default: gemini-3.8-flash)."""
    model_name = os.getenv("ADK_MODEL", DEFAULT_MODEL)
    return Gemini(
        model=model_name,
        retry_options=types.HttpRetryOptions(attempts=3),
    )


def build_rubric_toolset(skills_dir: Path = SKILLS_DIR) -> SkillToolset:
    """Dynamically loads all ADK rubric skills from the local skills directory."""
    skill_dirs = sorted(
        path
        for path in skills_dir.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    )
    if not skill_dirs:
        raise FileNotFoundError(f"No valid ADK skill directories found in {skills_dir}")

    skills = [load_skill_from_dir(skill_dir) for skill_dir in skill_dirs]
    return SkillToolset(skills=skills)


rubric_toolset = build_rubric_toolset()
_model = _build_model()
_fast_reasoning_config = types.GenerateContentConfig(
    temperature=0.1,
    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
)
_fast_formatting_config = types.GenerateContentConfig(
    temperature=0.1,
    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
)

signal_extractor = LlmAgent(
    name="signal_extractor",
    model=_model,
    generate_content_config=_fast_reasoning_config,
    description=(
        "Identifies interview type and target level, loads the matching ADK rubric "
        "skill and level anchors, and maps raw interview notes to rubric competencies."
    ),
    instruction="""You are Stage 1 of a structured candidate debrief pipeline: the Signal Extractor.

Your job is to transform raw, unstructured technical interview notes into concise, structured,
rubric-mapped behavioral evidence.

Follow these steps strictly in order (minimize tool round-trips):
1. Identify the interview type(s) (System Design, Coding, or Behavioral) and target level
   (L3, L4, or L5) from the raw notes. If no level is stated, default to L5 and note it.
2. Do NOT call `list_skills` — the available skills are `system-design-rubric`,
   `coding-rubric`, and `behavioral-rubric`. Immediately call `load_skill` for each
   matching interview type (in parallel if multiple rounds appear).
3. In the next turn, call `load_skill_resource` in parallel for BOTH:
   - The level-specific anchor file (`references/l3-anchors.md`, `references/l4-anchors.md`, or `references/l5-anchors.md`)
   - The output template (`assets/debrief-template.md`)
4. Map verbatim quotes and concrete observations from the raw notes to EVERY competency
   defined in the loaded anchor file:
   - Explicitly distinguish `[Independent]` signals from `[Hint-Assisted]` signals.
   - Mark competencies with no supporting evidence as `Not Assessed`.
   - Capture any subjective, vague, or potentially biased remarks verbatim in a dedicated
     **Raw Subjective / Unverified Remarks** list for Stage 2 (`gap_auditor`).
5. Keep your output concise and structured using clean Markdown bullets, including:
   - Candidate Information Summary (only details explicitly in the notes; else "Not provided in raw notes")
   - Interview Type, Evaluation Baseline Level (`L3`/`L4`/`L5`, specified vs. defaulted), and Rubric Skill Name
   - Any specific Core Competencies explicitly requested by the user
   - Loaded rubric competency names, brief anchor criteria, and rating calibration scale
   - Competency-by-competency extracted signals (`[Independent]` / `[Hint-Assisted]` / `Not Assessed`)
   - Raw Subjective / Unverified Remarks (verbatim) and Ambiguous Hint Usage notes
   - Required template section headings from `assets/debrief-template.md` (concise list, no boilerplate comments).
""",
    tools=[rubric_toolset],
    output_key="extracted_signals",
    before_agent_callback=record_stage_start,
    after_agent_callback=record_stage_end,
    after_tool_callback=trace_skill_tool_call,
)

gap_auditor = LlmAgent(
    name="gap_auditor",
    model=_model,
    generate_content_config=_fast_reasoning_config,
    include_contents="none",
    description=(
        "Audits extracted interview signals for subjective or biased phrasing, "
        "unassessed rubric competencies, and ambiguous hint usage."
    ),
    instruction="""You are Stage 2 of a structured candidate debrief pipeline: the Gap & Bias Auditor.

You have NO tools. Perform pure, concise reasoning over the extracted signals below:

<extracted_signals>
{extracted_signals}
</extracted_signals>

Produce a compact bulleted audit report (under 250 words) covering exactly three areas:
1. **Subjective or Biased Phrasing Flags:** Quote each non-behavioral, vague, or biased
   remark (e.g., "seemed nervous", "good culture fit", English/language comments) and state
   that it MUST be excluded from competency evaluation.
2. **Unassessed Competencies (`Not Assessed`):** List every `Not Assessed` or partially
   assessed competency and what specific signal is missing for the target level.
3. **Hint Dependency & Attribution Ambiguities:** Highlight areas of heavy hint reliance
   or unclear independent vs. prompted attribution.

Be strictly grounded in `{extracted_signals}`. Do NOT invent facts or make any hiring recommendation.
""",
    tools=[],
    output_key="audit_flags",
    before_agent_callback=record_stage_start,
    after_agent_callback=record_stage_end,
)

debrief_synthesizer = LlmAgent(
    name="debrief_synthesizer",
    model=_model,
    generate_content_config=_fast_formatting_config,
    include_contents="none",
    description=(
        "Synthesizes the final rubric-aligned Markdown debrief and targeted follow-up "
        "questions from extracted signals and audit flags."
    ),
    instruction="""You are Stage 3 of a structured candidate debrief pipeline: the Debrief Synthesizer.

You have NO tools. Produce the final standardized Markdown candidate debrief using ONLY
the verified evidence and audit findings below:

<extracted_signals>
{extracted_signals}
</extracted_signals>

<audit_flags>
{audit_flags}
</audit_flags>

Strict Formatting & Safety Rules:
1. Output clean, valid GitHub-Flavored Markdown (CommonMark):
   - Use a single `# ` H1 title at the top, `## ` for major sections, and `### ` for sub-sections.
   - Leave a blank line before and after every heading and list.
   - NEVER use triple or quadruple backticks (e.g., triple-backtick code fences) inline
     inside bullet points or numbered questions — use single backticks for inline code.
   - Never emit consecutive `---` dividers.
2. Follow the exact section order from the loaded template:
   - **Candidate Information Summary** (when provided in raw notes; otherwise `Not provided in raw notes.`)
   - **Evaluation Baseline Level** (baseline level, specified vs. defaulted, 1-sentence calibration bar)
   - **Interview Metadata** (including Assessment Coverage counts)
   - **Executive Summary**
   - **Core Competency Ratings**
   - **Competency Breakdown**
   - **Signal Quality & Bias Audit**
   - **Suggested Follow-Up Questions**
   - **Level-Fit Conclusion**
3. In **Core Competency Ratings**, assign an explicit level-calibrated rating
   (`Strong` | `Meets Bar` | `Developing` | `Poor` | `Not Assessed`) to each applicable core
   competency (including **Domain-Specific Acumen**, **Writing Code**, **System Design**,
   **Technical Problem-Solving**, and/or **Ownership & Execution** / **Collaboration &
   Communication**, plus any specific competency requested by the user in `{extracted_signals}`),
   followed by a concise **Evidence:** paragraph grounded strictly in observable signals.
   If multiple interview rounds appear, synthesize ONE unified report covering all rounds cleanly.
4. Ground every competency section strictly in observable behavioral evidence and verbatim
   quotes from `{extracted_signals}`, keeping `[Independent]` vs. `[Hint-Assisted]` tags intact.
   Exclude flagged subjective/biased remarks from ratings; report them in **Signal Quality & Bias Audit**.
5. Explicitly mark any unassessed competencies as `Not Assessed`.
6. In **Suggested Follow-Up Questions**, provide 3–5 concrete probe questions directly
   targeting the `Not Assessed` gaps and hint ambiguities in `{audit_flags}`.
7. End with **Level-Fit Conclusion**: provide an **Observed Level Alignment**
   (`Strong Alignment` | `Meets Level Expectations` | `Partial / Mixed Alignment` |
   `Below Level Expectations` | `Insufficient Signal`) and a brief 2–4 sentence rationale.
8. **STRICT GUARDRAIL:** NEVER output a Hire, No-Hire, Strong Hire, Lean Hire, or numeric
   hiring decision/score. All hiring judgment belongs exclusively to the human interviewer.
""",
    tools=[],
    output_key="final_debrief",
    before_agent_callback=record_stage_start,
    after_agent_callback=record_stage_end,
    after_model_callback=enforce_no_hire_guardrail,
)
