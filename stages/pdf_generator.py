"""
Stage 8 — PDF Generator + Review Aggregator

Converts rendered HTML → final.pdf via WeasyPrint.
Aggregates all flagged EvaluatedSteps → review_required.json (always written).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from schemas.pipeline_state import EvaluatedStep, ReviewItem


def run(
    rendered_html: str,
    evaluated_steps: list[EvaluatedStep],
    diff: dict,
    output_dir: str,
    run_id: str,
) -> dict[str, str]:
    """
    Returns dict with paths: {pdf, diff_json, review_json}
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    pdf_path = str(out / "final.pdf")
    diff_path = str(out / "diff.json")
    review_path = str(out / "review_required.json")

    # Generate PDF
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(rendered_html)
        page.pdf(path=pdf_path, print_background=True)
        browser.close()

    # Write diff.json
    with open(diff_path, "w") as f:
        json.dump(diff, f, indent=2)

    # Build and write review_required.json — always present, even if empty
    review_items = _build_review_items(evaluated_steps)
    review_payload = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "instructions": (
            "Set 'approved': true for each step after review. "
            "Run: python pipeline.py --publish <run_id>"
        ),
        "flagged_steps": [item.model_dump() for item in review_items],
    }
    with open(review_path, "w") as f:
        json.dump(review_payload, f, indent=2)

    return {"pdf": pdf_path, "diff_json": diff_path, "review_json": review_path}


def _build_review_items(evaluated_steps: list[EvaluatedStep]) -> list[ReviewItem]:
    items = []
    for ev in evaluated_steps:
        if ev.eval_flags or ev.revised_step.confidence in ("low", "medium"):
            items.append(ReviewItem(
                step_id=ev.revised_step.step_id,
                flags=ev.eval_flags,
                original_text=ev.revised_step.original_step.body_text,
                revised_text=ev.revised_step.revised_body_text,
                confidence=ev.revised_step.confidence,
            ))
    return items
