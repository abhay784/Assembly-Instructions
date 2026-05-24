"""
Stage 3 — Agent Planner

Single LLM call that classifies each affected step and returns an ActionPlan.
Not an agentic loop — this stage just plans. Stage 4 executes.

Actions:
  rewrite_text              — dimension/material/property change, no image update
  rewrite_text_and_rerender — mate constraint change or part geometry change
  add_step_flagged          — new part added, step must be authored; needs_manual_view=True in v1
  no_change                 — indirect impact, existing text remains valid
"""

import json
import re

from llm.client import LLMClient
from schemas.eco import ECO
from schemas.instruction import Step
from schemas.pipeline_state import ActionPlan, AffectedStep

_SYSTEM_PROMPT = """You are a mechanical assembly instruction planner. Given a list of affected assembly steps and their associated engineering change orders (ECOs), classify each step with the appropriate action.

Actions:
- "rewrite_text": Only the instruction prose needs updating (dimension, material, torque value changed). The existing Composer view image is still accurate.
- "rewrite_text_and_rerender": Both text AND the Composer CAD view image need updating (part geometry changed, mate type changed, or part swap that changes visual appearance). The existing Composer view_id for this step will be re-rendered automatically — set needs_manual_view=false.
- "add_step_flagged": A brand-new part has been added to the assembly with no existing step. A new step must be created. In v1, brand-new Composer views cannot be auto-generated — set needs_manual_view=true and flag for engineer.
- "no_change": This step is only indirectly affected. The existing text and image remain valid.

Output a JSON array where each object has:
{
  "step_id": "<step_id>",
  "eco_id": "<eco_id>",
  "action": "<action>",
  "needs_manual_view": <true|false>,
  "rationale": "<one sentence explaining why>"
}

Rules:
1. Mate constraint changes (mate_type, concentric→coincident, etc.) always require rerender — the geometry relationship is visually different.
2. Dimension changes only require rerender if the change would visibly affect the CAD view at the assembly level (e.g. a 50mm→200mm shaft change is visible; a 0.1mm tolerance change is not).
3. If impact is "indirect", lean toward "no_change" unless the ECO summary indicates assembly-level consequences.
4. needs_manual_view=true is ONLY for "add_step_flagged" (no existing Composer view exists to re-render). For "rewrite_text_and_rerender", always set needs_manual_view=false — the existing view will be updated automatically.
5. Output ONLY valid JSON. No markdown, no explanation."""


def run(
    affected_steps: list[AffectedStep],
    ecos: list[ECO],
    steps: list[Step],
    llm: LLMClient,
) -> list[ActionPlan]:
    if not affected_steps:
        return []

    eco_map = {eco.eco_id: eco for eco in ecos}
    step_map = {s.step_id: s for s in steps}

    context_items = []
    for aff in affected_steps:
        eco = eco_map.get(aff.eco_id)
        step = step_map.get(aff.step_id)
        context_items.append({
            "step_id": aff.step_id,
            "eco_id": aff.eco_id,
            "impact": aff.impact,
            "change_types": aff.change_types,
            "eco_summary": eco.summary if eco else "",
            "eco_changes": [c.model_dump() for c in eco.changes] if eco else [],
            "step_heading": step.heading if step else "",
            "step_body_preview": (step.body_text[:300] if step else ""),
            "step_images": [
                {"kind": img.kind, "visible_parts": img.visible_parts}
                for img in (step.images if step else [])
            ],
        })

    user_message = json.dumps(context_items, indent=2)
    response = llm.complete(
        messages=[{"role": "user", "content": user_message}],
        system=_SYSTEM_PROMPT,
    )

    raw = _strip_fences(response.content)
    parsed = json.loads(raw)

    plans: list[ActionPlan] = []
    for item in parsed:
        plans.append(ActionPlan.model_validate(item))
    return plans


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text
