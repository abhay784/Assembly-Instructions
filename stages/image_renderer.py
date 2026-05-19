"""
Stage 5 — Image Renderer

Calls the SolidWorks Composer FastAPI bridge to re-render views for steps
whose action plan includes rewrite_text_and_rerender.

For each affected step:
  1. Verify the view_id (== step_id) exists in the .smg file
  2. Call /render/{view_id} to produce a new PNG
  3. Save to assets/{document_id}/{revision_id}/{view_id}.png
  4. Keep the old PNG path for before/after diff in review output

Steps with needs_manual_view=True are skipped — flagged for engineer.
"""

import os
import shutil
from pathlib import Path

from composer.client import ComposerClient
from schemas.pipeline_state import ActionPlan, RenderedImage, RevisedStep


def run(
    action_plans: list[ActionPlan],
    revised_steps: list[RevisedStep],
    document_id: str,
    revision_id: str,
    smg_path: str,
    new_cad_path: str,
    assets_dir: str = "assets",
) -> list[RenderedImage]:
    render_plans = [
        p for p in action_plans
        if p.action == "rewrite_text_and_rerender" and not p.needs_manual_view
    ]

    if not render_plans:
        return _skipped_images(action_plans, revised_steps)

    with ComposerClient() as composer:
        if not composer.health():
            raise RuntimeError(
                "Composer bridge at SOLIDWORKS_API is not reachable. "
                "Start the FastAPI service on the Windows machine."
            )

        # Sync once per run — update .smg with new CAD
        composer.sync(smg_path=smg_path, new_cad_path=new_cad_path)

        available_views = {v["view_id"] for v in composer.list_views()}
        output_dir = Path(assets_dir) / document_id / revision_id
        output_dir.mkdir(parents=True, exist_ok=True)

        results: list[RenderedImage] = []
        for plan in render_plans:
            view_id = plan.step_id  # 1:1 mapping

            if view_id not in available_views:
                results.append(RenderedImage(
                    step_id=plan.step_id,
                    view_id=view_id,
                    old_path=None,
                    new_path=None,
                    skipped=True,
                ))
                continue

            old_path = _find_previous_render(assets_dir, document_id, view_id)
            rendered_path = composer.render(view_id)

            # Copy PNG from Windows path to local assets dir
            local_path = str(output_dir / f"{view_id}.png")
            if rendered_path != local_path:
                shutil.copy2(rendered_path, local_path)

            results.append(RenderedImage(
                step_id=plan.step_id,
                view_id=view_id,
                old_path=old_path,
                new_path=local_path,
                skipped=False,
            ))

    # Append skipped entries for steps that don't need a render
    results += _skipped_images(
        [p for p in action_plans if p not in render_plans],
        revised_steps,
    )
    return results


def _find_previous_render(assets_dir: str, document_id: str, view_id: str) -> str | None:
    """Find the most recent existing PNG for this view across all revisions."""
    doc_dir = Path(assets_dir) / document_id
    if not doc_dir.exists():
        return None
    candidates = sorted(doc_dir.glob(f"*/{view_id}.png"), key=os.path.getmtime, reverse=True)
    return str(candidates[0]) if candidates else None


def _skipped_images(
    plans: list[ActionPlan],
    revised_steps: list[RevisedStep],
) -> list[RenderedImage]:
    step_ids = {p.step_id for p in plans}
    results = []
    for step in revised_steps:
        if step.step_id in step_ids:
            results.append(RenderedImage(
                step_id=step.step_id,
                view_id=step.step_id,
                old_path=None,
                new_path=None,
                skipped=True,
            ))
    return results
