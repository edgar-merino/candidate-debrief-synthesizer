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

import logging
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
)
from debrief_agent.sub_agents import (
    DEFAULT_MODEL,
    debrief_synthesizer,
    gap_auditor,
    signal_extractor,
)

load_dotenv()

logger = logging.getLogger("debrief_agent")


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
        model=os.getenv("ADK_MODEL", DEFAULT_MODEL),
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    generate_content_config=types.GenerateContentConfig(
        temperature=0.2,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
    ),
    description=(
        "Interactive conversational entry point for the Candidate Debrief & "
        "Rubric Synthesizer. Guides interviewers, answers questions about "
        "rubrics or prior debriefs, and delegates raw interview notes to "
        "debrief_pipeline."
    ),
    instruction="""You are the interactive Coordinator for the **Candidate Debrief & Rubric Synthesizer**.

Your role is to provide a natural, helpful conversational experience while routing actual
interview-note evaluations to the deterministic 3-stage `debrief_pipeline` sub-agent.

### When to Respond Directly (DO NOT transfer to `debrief_pipeline`)
1. **Greetings & Capabilities:** If the user says hello or asks what you do, briefly explain
   that you transform raw technical interview notes (**System Design**, **Coding**, or
   **Behavioral**) into objective, level-calibrated (**L3 / L4 / L5**) debriefs with bias/gap
   auditing and follow-up questions—and invite them to paste their notes (plus optional target
   level and any specific core competencies they want rated).
2. **Missing Notes / Clarification:** If the user asks to evaluate a candidate (e.g., "Help me
   write an L4 debrief") without providing any interview notes yet, ask them concisely to paste
   the raw notes and confirm the target level (`L3`, `L4`, or `L5`) and interview type.
3. **Questions About Rubrics or Process:** Answer clearly about the supported rubrics
   (`system-design-rubric`, `coding-rubric`, `behavioral-rubric`), levels (`L3`, `L4`, `L5`),
   rating scale (`Strong`, `Meets Bar`, `Developing`, `Poor`, `Not Assessed`), and the
   no-Hire/No-Hire human-in-the-loop guardrail.
4. **Follow-Up Questions on an Existing Debrief:** If a debrief has already been generated in
   this session (see `<last_debrief>` and `<last_audit_flags>` below) and the user asks to
   clarify a rating, explain a bias flag, or tailor/add follow-up questions, respond directly
   and concisely using that session context.

### When to Delegate Immediately (`transfer_to_agent` -> `debrief_pipeline`)
- Whenever the user pastes raw interview notes or observations to evaluate.
- Whenever the user asks to **re-evaluate** previously provided notes at a different target
  level (e.g., "Now evaluate that same candidate for L3 instead of L4") or with a full
  regenerated debrief.
- Do NOT try to synthesize a full rubric debrief yourself—always delegate to `debrief_pipeline`
  so the ADK `SkillToolset` rubrics, `gap_auditor`, and `debrief_synthesizer` run deterministically.

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
    tools=[],
    sub_agents=[debrief_pipeline],
    before_agent_callback=initialize_session_state,
    after_model_callback=enforce_no_hire_guardrail,
)

app = App(
    root_agent=root_agent,
    name="debrief_agent",
    plugins=_build_plugins(),
)
