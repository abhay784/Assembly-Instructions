"""
Phase 3 — Composer Angle Automation

Auto-calculates optimal camera angles for assembly steps and programs them
into Composer views, reducing manual view authoring.

Key functions:
  suggest_angle(step, parts_db) → dict
    Analyzes parts in a step and suggests optimal viewing angle

  apply_angle(view_id, angle, composer_client) → bool
    Programs the angle into Composer via /author_view endpoint

  optimize_angles_for_steps(steps, action_plans, composer_client) → list[ActionPlan]
    Batch optimization: analyzes all new/rerendered steps, applies angles,
    returns updated action plans with needs_manual_view=False where successful
"""

import json
import math
from dataclasses import dataclass
from typing import Optional

from composer.client import ComposerClient
from schemas.instruction import Step, PartRef
from schemas.pipeline_state import ActionPlan


@dataclass
class CameraAngle:
    """Camera angle in Composer format (azimuth, elevation in degrees)."""
    azimuth: float  # 0-360, 0=front, 90=right, 180=back, 270=left
    elevation: float  # 0-90, 0=horizontal, 90=top-down


# Standard viewing angles optimized for assembly clarity
ISOMETRIC_STANDARD = CameraAngle(azimuth=45, elevation=35)  # Front-right-top
ISOMETRIC_OPPOSITE = CameraAngle(azimuth=225, elevation=35)  # Back-left-top
TOP_DOWN = CameraAngle(azimuth=0, elevation=85)  # Bird's eye view
FRONT_VIEW = CameraAngle(azimuth=0, elevation=0)  # Direct front


def suggest_angle(step: Step, parts_db: dict | None = None) -> Optional[CameraAngle]:
    """
    Suggest optimal viewing angle for a step based on parts involved.

    Rules:
    - If step involves fasteners (bolts, screws) primarily: use isometric standard
    - If step involves vertical assembly (tall parts): use front-right isometric
    - If step involves horizontal assembly: use standard isometric
    - If step is top-level assembly view: use top-down
    - Default: standard isometric (45/35)
    """
    if not step.parts_referenced:
        return ISOMETRIC_STANDARD

    # Analyze parts involved
    part_keywords = set()
    for part in step.parts_referenced:
        part_id = part.part_id.lower()
        part_keywords.add(part_id)

        # Extract material/type hints
        if "bracket" in part_id or "frame" in part_id:
            part_keywords.add("structural")
        if "screw" in part_id or "bolt" in part_id or "nut" in part_id or "washer" in part_id:
            part_keywords.add("fastener")
        if "shaft" in part_id or "rod" in part_id:
            part_keywords.add("linear")
        if "gear" in part_id or "pulley" in part_id:
            part_keywords.add("rotational")

    # Heuristic rules
    if "fastener" in part_keywords and len(step.parts_referenced) <= 3:
        # Close-up fastening detail: angled to show bolt head and hole
        return ISOMETRIC_STANDARD

    if "rotational" in part_keywords:
        # Gears/pulleys: show face-on to see teeth/grooves clearly
        return CameraAngle(azimuth=0, elevation=45)

    if "vertical" in step.heading.lower() or "upright" in step.body_text.lower():
        return CameraAngle(azimuth=45, elevation=45)

    # Default: standard isometric works for most assembly steps
    return ISOMETRIC_STANDARD


def apply_angle(view_id: str, angle: CameraAngle, composer: ComposerClient) -> bool:
    """
    Apply camera angle to a Composer view.

    Calls POST /author_view with azimuth/elevation.
    Returns True if successful, False if endpoint not implemented or fails.
    """
    try:
        response = composer.author_view(
            view_id=view_id,
            azimuth=angle.azimuth,
            elevation=angle.elevation,
        )
        return response.get("success", False)
    except Exception as e:
        # /author_view not yet implemented (501), or network error
        # Log and continue — fallback to manual authoring
        print(f"    [angle] {view_id}: author_view failed ({e}), manual authoring needed")
        return False


def optimize_angles_for_steps(
    steps: list[Step],
    action_plans: list[ActionPlan],
    composer: ComposerClient,
) -> list[ActionPlan]:
    """
    Batch optimize angles for all new/rerendered steps.

    For each step with needs_manual_view=True:
      1. Suggest optimal angle based on parts
      2. Apply angle via /author_view
      3. If successful, set needs_manual_view=False

    Returns updated action plans.
    """
    step_map = {s.step_id: s for s in steps}
    updated_plans = []

    for plan in action_plans:
        step = step_map.get(plan.step_id)
        if not step:
            updated_plans.append(plan)
            continue

        # Only optimize if needs_manual_view is True (new step or flagged)
        if not plan.needs_manual_view or plan.action not in ("add_step_flagged", "rewrite_text_and_rerender"):
            updated_plans.append(plan)
            continue

        # Try to auto-calculate and apply angle
        angle = suggest_angle(step)
        if angle and apply_angle(plan.step_id, angle, composer):
            # Success: angle was programmed into Composer
            updated = ActionPlan.model_validate({
                **plan.model_dump(),
                "needs_manual_view": False,
                "rationale": f"{plan.rationale} [angle auto-optimized: {angle.azimuth}°/{angle.elevation}°]"
            })
            print(f"    [angle] {plan.step_id}: optimized to {angle.azimuth}°/{angle.elevation}°")
            updated_plans.append(updated)
        else:
            # Optimization failed or not available
            updated_plans.append(plan)

    return updated_plans


def batch_suggest_angles(steps: list[Step]) -> dict[str, CameraAngle]:
    """
    Suggest angles for all steps without applying them.
    Useful for preview or dry-run analysis.

    Returns: {step_id: CameraAngle, ...}
    """
    result = {}
    for step in steps:
        angle = suggest_angle(step)
        if angle:
            result[step.step_id] = angle
    return result


def print_angle_report(action_plans: list[ActionPlan], steps: list[Step]) -> None:
    """Print summary of angle optimizations applied."""
    step_map = {s.step_id: s for s in steps}
    optimized = [p for p in action_plans if "[angle auto-optimized:" in p.rationale]

    if not optimized:
        print("    [angle] No steps auto-optimized")
        return

    print(f"    [angle] Optimized {len(optimized)} step(s):")
    for plan in optimized:
        # Extract angle from rationale: "[angle auto-optimized: XXX°/YYY°]"
        import re
        match = re.search(r'\[angle auto-optimized: ([\d.]+)°/([\d.]+)°\]', plan.rationale)
        if match:
            az, el = match.groups()
            print(f"      - {plan.step_id}: {az}°/{el}°")
