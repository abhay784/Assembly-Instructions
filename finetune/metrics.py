"""
finetune/metrics.py — Per-run quality metrics for tracking improvement over time.

Each pipeline publish appends one record to run_metrics.jsonl. Run
`python -m finetune.metrics` to print a summary table grouped by model.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

_METRICS_PATH = Path("run_metrics.jsonl")

_CONFIDENCE_SCORE = {"high": 1.0, "medium": 0.5, "low": 0.0}


def record_run_metrics(
    run_id: str,
    model_used: str,
    review_items: list[dict],
) -> None:
    """
    Append one metrics record for a completed publish run.

    `review_items` is the list from review_required.json["flagged_steps"].
    Steps not in review (auto-passed by eval gate) are counted separately
    via `auto_passed_count = total_revised - len(review_items)`.
    """
    approved = sum(1 for s in review_items if s.get("approved", False))
    approval_rate = approved / len(review_items) if review_items else None

    flag_counts: dict[str, int] = {}
    confidence_scores: list[float] = []
    for item in review_items:
        for flag in item.get("flags", []):
            ft = flag.get("flag_type", "unknown")
            flag_counts[ft] = flag_counts.get(ft, 0) + 1
        conf = item.get("confidence", "unknown")
        if conf in _CONFIDENCE_SCORE:
            confidence_scores.append(_CONFIDENCE_SCORE[conf])

    avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else None

    record = {
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model_used": model_used,
        "review_required_count": len(review_items),
        "approved_count": approved,
        "approval_rate": approval_rate,
        "avg_confidence_score": avg_confidence,
        "flag_counts": flag_counts,
    }

    with open(_METRICS_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")


def print_metrics_report(metrics_path: Path = _METRICS_PATH) -> None:
    """Print a summary table grouped by model, showing trend over time."""
    if not metrics_path.exists():
        print("No metrics recorded yet. Run the pipeline with --publish to start collecting.")
        return

    records = [json.loads(line) for line in metrics_path.read_text().splitlines() if line.strip()]
    if not records:
        print("No metrics records found.")
        return

    # Group by model
    by_model: dict[str, list[dict]] = {}
    for r in records:
        model = r.get("model_used", "unknown")
        by_model.setdefault(model, []).append(r)

    print(f"\n{'Model':<40} {'Runs':>5} {'Avg Approval':>13} {'Avg Confidence':>15} {'Flags/Run':>10}")
    print("-" * 88)

    model_summaries = []
    for model, recs in by_model.items():
        approval_rates = [r["approval_rate"] for r in recs if r.get("approval_rate") is not None]
        conf_scores = [r["avg_confidence_score"] for r in recs if r.get("avg_confidence_score") is not None]
        flags_per_run = [sum(r.get("flag_counts", {}).values()) for r in recs]

        avg_approval = sum(approval_rates) / len(approval_rates) if approval_rates else None
        avg_conf = sum(conf_scores) / len(conf_scores) if conf_scores else None
        avg_flags = sum(flags_per_run) / len(flags_per_run) if flags_per_run else 0.0

        model_summaries.append((model, len(recs), avg_approval, avg_conf, avg_flags))

    for model, runs, avg_approval, avg_conf, avg_flags in model_summaries:
        approval_str = f"{avg_approval:.1%}" if avg_approval is not None else "n/a"
        conf_str = f"{avg_conf:.2f}" if avg_conf is not None else "n/a"
        print(f"{model:<40} {runs:>5} {approval_str:>13} {conf_str:>15} {avg_flags:>10.1f}")

    # Trend arrow between consecutive model versions
    if len(model_summaries) >= 2:
        print()
        prev = model_summaries[-2]
        curr = model_summaries[-1]
        if prev[2] is not None and curr[2] is not None:
            delta = curr[2] - prev[2]
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            print(f"Approval rate trend vs previous model: {arrow} {delta:+.1%}")
        if prev[3] is not None and curr[3] is not None:
            delta = curr[3] - prev[3]
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            print(f"Confidence trend vs previous model:    {arrow} {delta:+.2f}")


if __name__ == "__main__":
    print_metrics_report()
