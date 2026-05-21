"""
Image Rating Agent — Phase 2

Evaluates rendered assembly images on a numeric scale (0-10) with detailed feedback
on specific issues (visibility, angle, context). This produces training signal for
the fine-tuned model to improve image rendering over multiple iterations.

Output: image_ratings.jsonl with per-image scores + suggestions.
"""

import base64
import json
import os
import re
from pathlib import Path
from typing import Literal

from llm.client import LLMClient
from schemas.pipeline_state import RenderedImage, RevisedStep


_IMAGE_RATING_SYSTEM = """You are a technical illustration expert evaluating assembly instruction diagrams.

Rate this CAD image on a NUMERIC SCALE (0-10) across three dimensions, then provide suggestions.

**Dimensions:**
1. Visibility (0-5): Are all required parts visible and clearly distinguishable?
   - 5: All parts perfectly visible, clear colors/contrast
   - 4: All parts visible, minor clarity issues
   - 3: All parts visible but some overlap or unclear geometry
   - 2: Some parts hidden or very hard to see
   - 1: Major parts not visible
   - 0: Image unusable

2. Angle (0-5): Is the camera angle optimal for understanding assembly?
   - 5: Perfect isometric/perspective shows all relevant sides and relationships
   - 4: Good angle, very usable for assembly
   - 3: Usable but could be improved (e.g., too steep, misses mating context)
   - 2: Angle is awkward, makes assembly understanding difficult
   - 1: Very poor angle, nearly incomprehensible
   - 0: Completely wrong perspective

3. Context (0-5): Does the image show necessary mating relationships?
   - 5: Shows primary part + all mating context perfectly
   - 4: Shows primary part + most mating relationships
   - 3: Shows primary part and some context
   - 2: Shows primary part but insufficient mating context
   - 1: Missing critical assembly context
   - 0: No useful context

**Respond with JSON:**
{
  "overall_score": <0-10, weighted sum>,
  "dimensions": {
    "visibility": <0-5>,
    "angle": <0-5>,
    "context": <0-5>
  },
  "issues": [
    {
      "issue_type": "part_not_visible" | "poor_angle" | "missing_context" | "unclear_geometry" | "bad_lighting",
      "severity": "critical" | "warning" | "info",
      "part_affected": "<part name or 'overall'>",
      "description": "<human-readable explanation>"
    }
  ],
  "suggestions": [
    "<specific suggestion, e.g., 'Rotate 15 degrees to show bolt hole'>"
  ]
}

Output ONLY valid JSON."""


def rate_image(
    image_path: str,
    step_text: str,
    llm: LLMClient,
) -> dict:
    """
    Rate a single assembly image on a 0-10 scale with detailed feedback.

    Args:
        image_path: Path to PNG image file
        step_text: Assembly step text for context
        llm: LLM client for vision evaluation

    Returns:
        Dictionary with overall_score, dimensions, issues, suggestions
    """
    if not os.path.exists(image_path):
        return {
            "overall_score": 0,
            "dimensions": {"visibility": 0, "angle": 0, "context": 0},
            "issues": [{"issue_type": "file_not_found", "severity": "critical", "part_affected": "overall", "description": f"Image file not found: {image_path}"}],
            "suggestions": [],
            "_error": "file_not_found",
        }

    try:
        with open(image_path, "rb") as f:
            image_b64 = base64.standard_b64encode(f.read()).decode()
    except OSError as e:
        return {
            "overall_score": 0,
            "dimensions": {"visibility": 0, "angle": 0, "context": 0},
            "issues": [{"issue_type": "read_error", "severity": "critical", "part_affected": "overall", "description": f"Could not read image: {e}"}],
            "suggestions": [],
            "_error": "read_error",
        }

    # Build user message with step text + image
    user_message = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"Assembly Step:\n{step_text[:500]}\n\nEvaluate this assembly image:",
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
            },
        ],
    }

    response = llm.complete(
        messages=[user_message],
        system=_IMAGE_RATING_SYSTEM,
    )

    result = _parse_rating_response(response.content)
    if result:
        return result
    else:
        return {
            "overall_score": 0,
            "dimensions": {"visibility": 0, "angle": 0, "context": 0},
            "issues": [{"issue_type": "eval_failed", "severity": "warning", "part_affected": "overall", "description": "Vision LLM rating failed to parse"}],
            "suggestions": [],
            "_error": "parse_failed",
        }


def collect_image_ratings(
    evaluated_steps: list,  # List of EvaluatedStep
    ratings_jsonl: Path,
    run_id: str,
    llm: LLMClient,
) -> int:
    """
    Collect image ratings for all rendered images in a pipeline run.

    Args:
        evaluated_steps: List of EvaluatedStep from eval_gate
        ratings_jsonl: Path to output JSONL file (append mode)
        run_id: Pipeline run ID
        llm: LLM client

    Returns:
        Count of newly rated images
    """
    new_ratings = 0
    seen_ids = _load_existing_ratings(ratings_jsonl)

    for evaluated_step in evaluated_steps:
        image = evaluated_step.rendered_image
        revised = evaluated_step.revised_step

        # Skip if no image or already rated
        if not image or image.skipped or not image.new_path:
            continue

        dedup_id = f"{run_id}:{revised.step_id}"
        if dedup_id in seen_ids:
            continue

        # Rate the image
        rating = rate_image(
            image_path=image.new_path,
            step_text=revised.revised_body_text,
            llm=llm,
        )

        # Append to JSONL with metadata
        record = {
            "step_id": revised.step_id,
            "run_id": run_id,
            "timestamp": _get_timestamp(),
            "score": rating.get("overall_score", 0),
            "dimensions": rating.get("dimensions", {}),
            "issues": rating.get("issues", []),
            "suggestions": rating.get("suggestions", []),
            "_meta": {"id": dedup_id},
        }

        ratings_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with open(ratings_jsonl, "a") as f:
            f.write(json.dumps(record) + "\n")

        new_ratings += 1

    return new_ratings


def print_image_report(ratings_jsonl: Path) -> None:
    """
    Print a summary report of image ratings: trends, issue breakdown, etc.
    """
    if not ratings_jsonl.exists():
        print("No image ratings found.")
        return

    ratings = _load_ratings_jsonl(ratings_jsonl)
    if not ratings:
        print("No image ratings found.")
        return

    # Group by run_id to show trends
    runs = {}
    for record in ratings:
        run_id = record.get("run_id")
        if run_id not in runs:
            runs[run_id] = []
        runs[run_id].append(record)

    print("\n" + "=" * 80)
    print("IMAGE QUALITY REPORT")
    print("=" * 80)

    # Overall stats
    all_scores = [r.get("score", 0) for r in ratings]
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
    print(f"\nOverall: {len(ratings)} images rated, avg score {avg_score:.1f}/10")

    # Issue breakdown
    issue_types = {}
    for record in ratings:
        for issue in record.get("issues", []):
            itype = issue.get("issue_type", "unknown")
            issue_types[itype] = issue_types.get(itype, 0) + 1

    if issue_types:
        print(f"\nIssue Types:")
        for itype, count in sorted(issue_types.items(), key=lambda x: -x[1]):
            pct = 100 * count / len(ratings)
            print(f"  {itype}: {count} ({pct:.0f}%)")

    # Per-run trend
    print(f"\nBy Run:")
    for run_id in sorted(runs.keys()):
        run_ratings = runs[run_id]
        run_avg = sum(r.get("score", 0) for r in run_ratings) / len(run_ratings)
        print(f"  {run_id}: {len(run_ratings)} images, avg {run_avg:.1f}/10")

    print("=" * 80 + "\n")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _parse_rating_response(content: str) -> dict | None:
    """Parse JSON response from vision LLM."""
    content = content.strip()
    content = re.sub(r"^```[a-z]*\n?", "", content)
    content = re.sub(r"\n?```$", "", content)
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


def _load_existing_ratings(ratings_jsonl: Path) -> set[str]:
    """Load set of already-rated (run_id:step_id) pairs to avoid dups."""
    if not ratings_jsonl.exists():
        return set()
    seen = set()
    for line in ratings_jsonl.read_text().splitlines():
        if line.strip():
            try:
                record = json.loads(line)
                dedup_id = record.get("_meta", {}).get("id")
                if dedup_id:
                    seen.add(dedup_id)
            except json.JSONDecodeError:
                pass
    return seen


def _load_ratings_jsonl(ratings_jsonl: Path) -> list[dict]:
    """Load all records from JSONL file."""
    records = []
    if ratings_jsonl.exists():
        for line in ratings_jsonl.read_text().splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _get_timestamp() -> str:
    """Get ISO 8601 timestamp."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
