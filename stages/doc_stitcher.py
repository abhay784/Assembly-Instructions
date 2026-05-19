"""
Stage 7 — Document Stitcher

Merges unchanged original steps with revised/new EvaluatedSteps, renders
the Jinja2 HTML template, and generates diff.json.

Step ordering: section order is preserved from the original document.
New steps (add_step_flagged) are inserted after the step that precedes
them in the ECO context — defaulting to end of their section.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from schemas.instruction import Step
from schemas.pipeline_state import ActionPlan, EvaluatedStep


def run(
    original_steps: list[Step],
    evaluated_steps: list[EvaluatedStep],
    action_plans: list[ActionPlan],
    run_id: str,
    eco_ids: list[str],
    template_dir: str = "templates",
) -> tuple[str, dict]:
    """
    Returns (rendered_html, diff_dict).
    """
    revised_map = {e.revised_step.step_id: e for e in evaluated_steps}
    plan_map = {p.step_id: p for p in action_plans}

    merged_steps = _merge_steps(original_steps, revised_map, plan_map)
    diff = _build_diff(original_steps, evaluated_steps, action_plans, run_id, eco_ids)
    html = _render_template(merged_steps, diff, template_dir)

    return html, diff


def _merge_steps(
    original_steps: list[Step],
    revised_map: dict[str, EvaluatedStep],
    plan_map: dict[str, ActionPlan],
) -> list[dict]:
    """
    Returns a flat list of step dicts ready for the template.
    Each dict has: step_id, section, step_number, heading, body_text,
    images, specs, callouts, eval_flags, is_revised, is_new, needs_manual_view.
    """
    merged: list[dict] = []
    seen_ids: set[str] = set()

    for step in original_steps:
        seen_ids.add(step.step_id)
        if step.step_id in revised_map:
            evaluated = revised_map[step.step_id]
            revised = evaluated.revised_step
            merged.append({
                "step_id": step.step_id,
                "section": step.section,
                "step_number": step.step_number,
                "heading": step.heading,
                "body_text": revised.revised_body_text,
                "images": _resolve_images(step, evaluated),
                "specs": [s.model_dump() for s in step.specs],
                "callouts": [c.model_dump() for c in step.callouts],
                "eval_flags": [f.model_dump() for f in evaluated.eval_flags],
                "is_revised": True,
                "is_new": False,
                "needs_manual_view": revised.needs_manual_view,
                "confidence": revised.confidence,
            })
        else:
            merged.append({
                "step_id": step.step_id,
                "section": step.section,
                "step_number": step.step_number,
                "heading": step.heading,
                "body_text": step.body_text,
                "images": [img.model_dump() for img in step.images],
                "specs": [s.model_dump() for s in step.specs],
                "callouts": [c.model_dump() for c in step.callouts],
                "eval_flags": [],
                "is_revised": False,
                "is_new": False,
                "needs_manual_view": False,
                "confidence": "high",
            })

    # Append new steps (add_step_flagged) not in original
    for step_id, evaluated in revised_map.items():
        if step_id not in seen_ids and evaluated.revised_step.is_new_step:
            revised = evaluated.revised_step
            merged.append({
                "step_id": step_id,
                "section": revised.original_step.section or "New Steps",
                "step_number": 999,
                "heading": revised.original_step.heading or step_id,
                "body_text": revised.revised_body_text,
                "images": [],
                "specs": [],
                "callouts": [],
                "eval_flags": [f.model_dump() for f in evaluated.eval_flags],
                "is_revised": False,
                "is_new": True,
                "needs_manual_view": revised.needs_manual_view,
                "confidence": revised.confidence,
            })

    return merged


def _resolve_images(step: Step, evaluated: EvaluatedStep) -> list[dict]:
    """Return image list with new_path substituted for re-rendered images."""
    image_list = []
    rendered = evaluated.rendered_image

    for img in step.images:
        img_dict = img.model_dump()
        if rendered and not rendered.skipped and rendered.new_path:
            img_dict["resolved_path"] = rendered.new_path
        else:
            img_dict["resolved_path"] = img.image_id
        image_list.append(img_dict)

    return image_list


def _build_diff(
    original_steps: list[Step],
    evaluated_steps: list[EvaluatedStep],
    action_plans: list[ActionPlan],
    run_id: str,
    eco_ids: list[str],
) -> dict:
    original_map = {s.step_id: s for s in original_steps}
    plan_map = {p.step_id: p for p in action_plans}

    steps_modified = []
    steps_added = []
    steps_unchanged = []
    flags_raised = []

    for evaluated in evaluated_steps:
        revised = evaluated.revised_step
        plan = plan_map.get(revised.step_id)

        for flag in evaluated.eval_flags:
            flags_raised.append({
                "step_id": revised.step_id,
                "flag_type": flag.flag_type,
                "detail": flag.detail,
                "severity": flag.severity,
            })

        if revised.is_new_step:
            steps_added.append({
                "step_id": revised.step_id,
                "text": revised.revised_body_text,
                "confidence": revised.confidence,
                "needs_manual_view": revised.needs_manual_view,
            })
        elif plan and plan.action != "no_change":
            original = original_map.get(revised.step_id)
            steps_modified.append({
                "step_id": revised.step_id,
                "action": plan.action,
                "text_before": original.body_text if original else "",
                "text_after": revised.revised_body_text,
                "image_rerendered": plan.action == "rewrite_text_and_rerender",
                "confidence": revised.confidence,
            })
        else:
            steps_unchanged.append(revised.step_id)

    return {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "eco_ids": eco_ids,
        "steps_modified": steps_modified,
        "steps_added": steps_added,
        "steps_unchanged": steps_unchanged,
        "flags_raised": flags_raised,
        "summary": {
            "total_modified": len(steps_modified),
            "total_added": len(steps_added),
            "total_unchanged": len(steps_unchanged),
            "total_flags": len(flags_raised),
        },
    }


def _render_template(merged_steps: list[dict], diff: dict, template_dir: str) -> str:
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=True,
    )
    template = env.get_template("assembly.html.j2")

    sections: dict[str, list[dict]] = {}
    for step in merged_steps:
        section = step["section"]
        sections.setdefault(section, []).append(step)

    return template.render(
        sections=sections,
        diff_summary=diff["summary"],
        generated_at=diff["generated_at"],
        run_id=diff["run_id"],
    )
