"""
Stage 5 — Image Renderer

Renders a PNG for every step whose action plan is rewrite_text_and_rerender
or add_step_flagged.  Two backends are tried in order:

  1. Composer bridge  (http://localhost:8000)
     Start with:  python -m uvicorn composer.server:app --port 8000
     Needs an .smg file OR pre-rendered PNGs in RENDER_DIR.

  2. SolidWorks direct  (COM automation via pywin32)
     Used automatically when the Composer bridge is not reachable AND
     --after-model points to a .sldasm file.
     SolidWorks must be installed and licensed.  pip install pywin32.

Camera angles are calculated by finetune.angle_optimizer.suggest_angle()
based on part types in each step (fasteners → isometric, gears → face-on…).
"""

import os
import re
import shutil
from pathlib import Path
from typing import Optional

from composer.client import ComposerClient
from schemas.instruction import Step
from schemas.pipeline_state import ActionPlan, RenderedImage, RevisedStep


# Default angle when no heuristic applies
_DEFAULT_AZIMUTH = 45.0
_DEFAULT_ELEVATION = 35.0


def run(
    action_plans: list[ActionPlan],
    revised_steps: list[RevisedStep],
    document_id: str,
    revision_id: str,
    smg_path: str,
    new_cad_path: str,
    assets_dir: str = "assets",
    original_steps: list[Step] | None = None,
) -> list[RenderedImage]:
    render_plans = [
        p for p in action_plans
        if p.action in ("rewrite_text_and_rerender", "add_step_flagged")
    ]

    if not render_plans:
        return _skipped_images(action_plans, revised_steps)

    output_dir = Path(assets_dir) / document_id / revision_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Backend 1: Composer bridge ---
    with ComposerClient() as composer:
        if composer.health():
            return _render_via_composer(
                composer, render_plans, action_plans, revised_steps,
                smg_path, new_cad_path, output_dir, assets_dir, document_id,
            )

    print("  Stage 5: Composer bridge not reachable — trying SolidWorks direct")

    # --- Backend 2: SolidWorks COM ---
    if new_cad_path and new_cad_path.lower().endswith((".sldasm", ".sldprt")):
        sw_results = _render_via_solidworks(
            assembly_path=new_cad_path,
            render_plans=render_plans,
            output_dir=output_dir,
            assets_dir=assets_dir,
            document_id=document_id,
            original_steps=original_steps,
        )
        if sw_results is not None:
            sw_results += _skipped_images(
                [p for p in action_plans if p not in render_plans],
                revised_steps,
            )
            return sw_results

    print(
        "  Stage 5: no rendering backend available — "
        "start the Composer bridge (python -m uvicorn composer.server:app --port 8000) "
        "or ensure SolidWorks is installed with pywin32"
    )
    return _skipped_images(action_plans, revised_steps)


# ---------------------------------------------------------------------------
# Composer bridge backend
# ---------------------------------------------------------------------------

def _render_via_composer(
    composer: ComposerClient,
    render_plans: list[ActionPlan],
    all_plans: list[ActionPlan],
    revised_steps: list[RevisedStep],
    smg_path: str,
    new_cad_path: str,
    output_dir: Path,
    assets_dir: str,
    document_id: str,
) -> list[RenderedImage]:
    # Sync only when both CAD paths are provided
    if smg_path and new_cad_path:
        composer.sync(smg_path=smg_path, new_cad_path=new_cad_path)

    available_views = {v["view_id"] for v in composer.list_views()}
    results: list[RenderedImage] = []

    for plan in render_plans:
        view_id = plan.step_id
        if view_id not in available_views:
            results.append(RenderedImage(
                step_id=plan.step_id, view_id=view_id,
                old_path=None, new_path=None, skipped=True,
            ))
            continue

        old_path = _find_previous_render(assets_dir, document_id, view_id)
        rendered_path = composer.render(view_id)

        local_path = str(output_dir / f"{view_id}.png")
        if rendered_path != local_path:
            shutil.copy2(rendered_path, local_path)

        results.append(RenderedImage(
            step_id=plan.step_id, view_id=view_id,
            old_path=old_path, new_path=local_path, skipped=False,
        ))

    results += _skipped_images(
        [p for p in all_plans if p not in render_plans],
        revised_steps,
    )
    return results


# ---------------------------------------------------------------------------
# SolidWorks direct backend
# ---------------------------------------------------------------------------

def _render_via_solidworks(
    assembly_path: str,
    render_plans: list[ActionPlan],
    output_dir: Path,
    assets_dir: str,
    document_id: str,
    original_steps: list[Step] | None,
) -> list[RenderedImage] | None:
    """
    Open the SolidWorks assembly and render each step as PNG.
    Returns None if SolidWorks is not available.
    """
    try:
        from composer.sw_renderer import SolidWorksRenderer
        from finetune.angle_optimizer import suggest_angle, ISOMETRIC_STANDARD
    except ImportError as e:
        print(f"  SW Renderer: import failed — {e}")
        return None

    step_map = {s.step_id: s for s in (original_steps or [])}

    renderer = SolidWorksRenderer(assembly_path)
    if not renderer.connect():
        return None
    if not renderer.open_assembly():
        renderer.close()
        return None

    results: list[RenderedImage] = []
    rendered_count = 0

    try:
        for plan in render_plans:
            view_id = plan.step_id
            old_path = _find_previous_render(assets_dir, document_id, view_id)
            local_path = str(output_dir / f"{view_id}.png")

            # Get angle: parse from rationale if angle_optimizer already ran,
            # else ask suggest_angle(), else fall back to isometric.
            az, el = _extract_angle_from_rationale(plan.rationale)
            if az is None:
                step = step_map.get(view_id)
                if step:
                    angle = suggest_angle(step) or ISOMETRIC_STANDARD
                else:
                    angle = ISOMETRIC_STANDARD
                az, el = angle.azimuth, angle.elevation

            try:
                renderer.render_step(view_id, az, el, local_path)
                results.append(RenderedImage(
                    step_id=plan.step_id, view_id=view_id,
                    old_path=old_path, new_path=local_path, skipped=False,
                ))
                rendered_count += 1
            except Exception as e:
                print(f"    step {view_id}: render failed — {e}")
                results.append(RenderedImage(
                    step_id=plan.step_id, view_id=view_id,
                    old_path=old_path, new_path=None, skipped=True,
                ))
    finally:
        renderer.close()

    print(f"  Stage 5 (SolidWorks): {rendered_count}/{len(render_plans)} image(s) rendered")
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_angle_from_rationale(rationale: str) -> tuple[float | None, float | None]:
    """Parse '[angle auto-optimized: 45°/35°]' from action plan rationale."""
    match = re.search(
        r'\[angle auto-optimized:\s*([\d.]+)°/([\d.]+)°\]',
        rationale or "",
    )
    if match:
        return float(match.group(1)), float(match.group(2))
    return None, None


def _find_previous_render(assets_dir: str, document_id: str, view_id: str) -> str | None:
    doc_dir = Path(assets_dir) / document_id
    if not doc_dir.exists():
        return None
    candidates = sorted(
        doc_dir.glob(f"*/{view_id}.png"),
        key=os.path.getmtime,
        reverse=True,
    )
    return str(candidates[0]) if candidates else None


def _skipped_images(
    plans: list[ActionPlan],
    revised_steps: list[RevisedStep],
) -> list[RenderedImage]:
    step_ids = {p.step_id for p in plans}
    return [
        RenderedImage(
            step_id=s.step_id, view_id=s.step_id,
            old_path=None, new_path=None, skipped=True,
        )
        for s in revised_steps
        if s.step_id in step_ids
    ]
