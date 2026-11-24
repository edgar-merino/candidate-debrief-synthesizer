---
name: system-design-rubric
description: >
  Rubric and evaluation anchors for L3, L4, and L5 System Design technical
  interviews (Cloud AI Engineer / Technical Solutions Consultant). Activate when
  raw interview notes describe an architecture, distributed systems, cloud
  platform, or AI/ML system design interview.
---

# System Design Interview Rubric

Use this skill to evaluate raw technical interview notes from a **System Design** round (Cloud AI Engineer / Technical Solutions Consultant).

## Step-by-Step Evaluation Instructions

1. **Determine the target level** from the raw interview notes (`L3`, `L4`, or `L5`). If no level is explicitly stated, default to `L5` and clearly note the assumption in your output.
2. **Load the matching level anchors** using `load_skill_resource`:
   - For L3: `references/l3-anchors.md`
   - For L4: `references/l4-anchors.md`
   - For L5: `references/l5-anchors.md`
3. **Load the debrief output template** using `load_skill_resource`:
   - `assets/debrief-template.md`
4. **Extract competency-mapped signals** from the raw notes against every competency listed in the loaded anchors:
   - Quote or closely paraphrase concrete, observable candidate behaviors and technical statements.
   - Explicitly label whether each signal was **Independent** (candidate surfaced it unprompted) or **Prompted / Hint-Assisted** (interviewer had to nudge or ask directly).
   - If a competency has no supporting evidence in the raw notes, mark it explicitly as `Not Assessed`.
   - Preserve any subjective, vague, or non-behavioral interviewer remarks verbatim in a separate "Raw Subjective / Unverified Notes" list so the downstream `gap_auditor` can flag them.
5. **Include rubric metadata in your output**:
   - Include the exact list of rubric competencies for the selected level and the key sections required by `assets/debrief-template.md` so downstream stages (`gap_auditor` and `debrief_synthesizer`) have complete context without needing tool access.

## Core Competency Rating Calibration (L3 / L4 / L5)

Apply these definitions strictly against the loaded target-level anchors (`l3-anchors.md`, `l4-anchors.md`, or `l5-anchors.md`):
- **Strong:** Consistently exceeds target-level positive anchors independently with deep trade-off reasoning and minimal/no prompting.
- **Meets Bar:** Satisfies target-level positive anchors with mostly independent signal and minor, constructive use of hints.
- **Developing:** Shows partial positive signals but relies heavily on hints or misses core expectations for the target level.
- **Poor:** Predominantly aligns with target-level negative anchors or fails to demonstrate foundational baseline skills for the target level.
- **Not Assessed:** Insufficient or missing behavioral evidence in the raw notes to evaluate the competency.
