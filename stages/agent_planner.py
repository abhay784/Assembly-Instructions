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
5. Output ONLY valid JSON. No markdown, no explanation.

WORKED EXAMPLES — one per action so the boundary is clear:

Example 1 — rewrite_text (text-only change):
  Input: step says "Torque the M3 screw to 0.4 Nm"; ECO changes "torque_nm" from 0.4 → 0.6.
  Correct: {"action": "rewrite_text", "needs_manual_view": false, "rationale": "Torque value change is prose-only; no visual difference in the assembly view."}

Example 2 — rewrite_text_and_rerender (visible geometry change):
  Input: step shows the upper bracket; ECO changes the bracket's mounting holes from 4 holes to 2 holes.
  Correct: {"action": "rewrite_text_and_rerender", "needs_manual_view": false, "rationale": "Hole pattern change is visible in any view containing the bracket — re-render the existing view."}

Example 3 — add_step_flagged (brand-new part with no existing step):
  Input: ECO adds a "thermistor cable shield" which has no current step.
  Correct: {"action": "add_step_flagged", "needs_manual_view": true, "rationale": "New part not in any existing step; engineer must author the view because no Composer view_id exists."}

Example 4 — no_change (indirect impact, no visible/textual consequence):
  Input: step assembles the Y-axis frame; ECO changes the color of an unrelated logo plate that's only mentioned in the BoM, not in this step.
  Correct: {"action": "no_change", "needs_manual_view": false, "rationale": "Indirect impact only; this step does not reference the changed part."}

EDGE CASES — disambiguate these specifically:

- Cosmetic-only changes (paint color, decal text) where the part is visible in the step → "rewrite_text_and_rerender" if the change appears in the existing view, otherwise "rewrite_text".
- Tolerance tightening with no nominal change (e.g. ±0.2mm → ±0.05mm) → "no_change". Tolerances are not in prose at the assembly-instruction level.
- ECO renames a part without altering geometry → "rewrite_text" so the prose uses the new name. Never "rewrite_text_and_rerender" for a pure rename.
- ECO adds a part AND modifies an existing step (e.g. new screw used in an old step) → emit TWO plans: "rewrite_text" for the existing step + "add_step_flagged" for any net-new step required."""


# Output scales linearly with the affected_steps count, so a single call
# eventually overflows even a 16K-token cap on large diffs (the Prusa
# MK3→MK3S diff produces 300+ affected steps). Batching keeps each call
# under cap and the per-batch output ~2-3K tokens. 30 is conservative
# enough that even verbose rationales fit; raise it if you want fewer
# round-trips and trust each output to stay tight.
_BATCH_SIZE = 30
_PER_BATCH_MAX_TOKENS = 16000


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

    context_items = [_build_context_item(aff, eco_map, step_map) for aff in affected_steps]

    plans: list[ActionPlan] = []
    total_batches = (len(context_items) + _BATCH_SIZE - 1) // _BATCH_SIZE

    for batch_idx in range(0, len(context_items), _BATCH_SIZE):
        batch = context_items[batch_idx : batch_idx + _BATCH_SIZE]
        batch_num = batch_idx // _BATCH_SIZE + 1
        print(f"  Stage 3: planning batch {batch_num}/{total_batches} ({len(batch)} step(s))")

        batch_plans = _plan_batch(batch, llm, batch_num=batch_num, total_batches=total_batches)
        plans.extend(batch_plans)

    return plans


def _build_context_item(
    aff: AffectedStep,
    eco_map: dict,
    step_map: dict,
) -> dict:
    eco = eco_map.get(aff.eco_id)
    step = step_map.get(aff.step_id)
    return {
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
    }


def _plan_batch(
    context_items: list[dict],
    llm: LLMClient,
    batch_num: int,
    total_batches: int,
) -> list[ActionPlan]:
    user_message = json.dumps(context_items, indent=2)
    response = llm.complete(
        messages=[{"role": "user", "content": user_message}],
        system=_SYSTEM_PROMPT,
        max_tokens=_PER_BATCH_MAX_TOKENS,
        cache_system=True,  # Same system prompt across all 10+ batches
    )

    if response.truncated:
        raise RuntimeError(
            f"Agent planner batch {batch_num}/{total_batches} hit the output token "
            f"limit ({_PER_BATCH_MAX_TOKENS}) on {len(context_items)} step(s). "
            f"Reduce _BATCH_SIZE."
        )

    raw = _strip_fences(response.content)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Agent planner batch {batch_num}/{total_batches} returned invalid JSON: {e}\n"
            f"Response length: {len(raw)} chars, stop_reason={response.stop_reason!r}\n"
            f"Tail of response (last 500 chars):\n{raw[-500:]}"
        ) from e

    return [ActionPlan.model_validate(item) for item in parsed]


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text
