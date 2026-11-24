# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Root agent entry point for the Candidate Debrief & Rubric Synthesizer."""

import os

from dotenv import load_dotenv
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from debrief_agent.callbacks import (
    enforce_no_hire_guardrail,
    finalize_pipeline_state,
    initialize_pipeline_state,
    initialize_session_state,
    log_tool_intent,
    log_tool_outcome,
)
from debrief_agent.logging_config import configure_structured_logging
from debrief_agent.sub_agents import (
    DEFAULT_MODEL,
    debrief_synthesizer,
    gap_auditor,
    signal_extractor,
)
from debrief_agent.tools import (
    export_debrief_artifact,
    recall_prior_debriefs,
    validate_interview_notes,
)

load_dotenv()

logger = configure_structured_logging("debrief_agent")


def _build_plugins() -> list[BasePlugin]:
    """Configures optional BigQuery Agent Analytics plugin when env vars are set."""
    plugins: list[BasePlugin] = []
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    dataset_id = os.getenv("BQ_ANALYTICS_DATASET_ID")

    if project_id and dataset_id:
        from google.adk.plugins.bigquery_agent_analytics_plugin import (
            BigQueryAgentAnalyticsPlugin,
            BigQueryLoggerConfig,
        )

        config = BigQueryLoggerConfig(
            enabled=True,
            gcs_bucket_name=os.getenv("LOGS_BUCKET_NAME"),
            enable_otel_correlation=True,
            custom_tags={"agent": "debrief_coordinator"},
        )
        plugins.append(
            BigQueryAgentAnalyticsPlugin(
                project_id=project_id,
                dataset_id=dataset_id,
                config=config,
            )
        )
        logger.info(
            "BigQuery Agent Analytics plugin enabled",
            extra={"project_id": project_id, "dataset_id": dataset_id},
        )
    return plugins


debrief_pipeline = SequentialAgent(
    name="debrief_pipeline",
    description=(
        "Deterministic 3-stage pipeline (signal_extractor -> gap_auditor -> "
        "debrief_synthesizer) that transforms raw technical interview notes into "
        "a standardized, rubric-aligned candidate debrief with bias/gap auditing "
        "and targeted follow-up questions. Transfer here whenever the user "
        "provides raw interview notes to evaluate or asks to re-run a full "
        "debrief at a different level."
    ),
    sub_agents=[
        signal_extractor,
        gap_auditor,
        debrief_synthesizer,
    ],
    before_agent_callback=initialize_pipeline_state,
    after_agent_callback=finalize_pipeline_state,
)

root_agent = LlmAgent(
    # Keep in sync with create_params.root_agent_name in agents-cli-manifest.yaml:
    # telemetry reports this name as gen_ai.agent.name, and agents-cli reports the
    # manifest's value.
    name="debrief_coordinator",
    model=Gemini(
        model=os.getenv("ADK_FAST_MODEL") or os.getenv("ADK_MODEL", DEFAULT_MODEL),
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    generate_content_config=types.GenerateContentConfig(
        temperature=0.2,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
    ),
    description=(
        "Interactive conversational entry point for the Candidate Debrief & "
        "Rubric Synthesizer. Validates notes, recalls prior debrief memories, "
        "exports artifacts with HITL approval, and delegates full debrief "
        "synthesis to debrief_pipeline."
    ),
    instruction="""You are the interactive Coordinator for the **Candidate Debrief & Rubric Synthesizer**.

Your role is to provide a natural, helpful conversational experience while routing actual
interview-note evaluations to the deterministic 3-stage `debrief_pipeline` sub-agent.

### Available Tools & When to Use Them
- `validate_interview_notes`: Use when notes look very short, ambiguous, or specify an
  unusual level (e.g., L6/L7) — follow its `recovery_hint` if it returns `status="error"`.
- `recall_prior_debriefs`: Use when the interviewer asks about prior candidate debriefs,
  earlier rounds, or cross-session rubric calibration history.
- `export_debrief_artifact`: Use ONLY when the interviewer explicitly asks to export or
  save the generated debrief as an artifact. This tool pauses for explicit human
  interviewer confirmation before persisting the PII-redacted Markdown file.

### When to Respond Directly (DO NOT transfer to `debrief_pipeline`)
1. **Greetings & Capabilities:** Briefly explain that you transform raw technical interview
   notes (**System Design**, **Coding**, or **Behavioral**) into objective, level-calibrated
   (**L3 / L4 / L5**) debriefs with bias/gap auditing and follow-up questions—and invite them
   to paste their notes.
2. **Missing Notes / Clarification:** If the user asks to evaluate a candidate without
   providing notes yet, ask them concisely to paste the raw notes and confirm the target
   level (`L3`, `L4`, or `L5`) and interview type.
3. **Questions About Rubrics or Process:** Answer clearly about the supported rubrics
   (`system-design-rubric`, `coding-rubric`, `behavioral-rubric`), levels (`L3`, `L4`, `L5`),
   rating scale (`Strong`, `Meets Bar`, `Developing`, `Poor`, `Not Assessed`), and the
   no-Hire/No-Hire human-in-the-loop guardrail.
4. **Follow-Up Questions on an Existing Debrief:** If a debrief has already been generated in
   this session (see `<last_debrief>` and `<last_audit_flags>` below) and the user asks to
   clarify a rating, explain a bias flag, or tailor follow-up questions, respond directly
   and concisely using that session context.

### When to Delegate Immediately (`transfer_to_agent` -> `debrief_pipeline`)
- Whenever the user pastes raw interview notes or observations to evaluate.
- Whenever the user asks to **re-evaluate** previously provided notes at a different target
  level (e.g., "Now evaluate that same candidate for L3 instead of L4").
- Do NOT synthesize a full rubric debrief yourself—always delegate to `debrief_pipeline`.

### Strict Guardrail
NEVER output a Hire, No-Hire, Strong Hire, Lean Hire, or numeric hiring decision/score in
conversation. All hiring judgment belongs exclusively to the human interviewer.

<last_audit_flags>
{audit_flags}
</last_audit_flags>

<last_debrief>
{final_debrief}
</last_debrief>
""",
    tools=[
        validate_interview_notes,
        recall_prior_debriefs,
        export_debrief_artifact,
    ],
    sub_agents=[debrief_pipeline],
    before_agent_callback=initialize_session_state,
    before_tool_callback=log_tool_intent,
    after_tool_callback=log_tool_outcome,
    after_model_callback=enforce_no_hire_guardrail,
)

app = App(
    root_agent=root_agent,
    name="debrief_agent",
    plugins=_build_plugins(),
)
