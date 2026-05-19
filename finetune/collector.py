"""
finetune/collector.py — Extract approved revisions as fine-tuning examples.

After each `pipeline.py --publish` run, call `collect_approved_examples()` to
append new approved revisions to training_data.jsonl. Deduplicates across runs
so re-publishing the same run_id is safe.
"""

import json
from pathlib import Path

from schemas.eco import ECO
from schemas.instruction import Step
from schemas.pipeline_state import ActionPlan, RevisedStep


def collect_approved_examples(
    run_id: str,
    output_dir: str,
    training_jsonl_path: Path,
) -> int:
    """
    Extract approved ReviewItems for `run_id` and append them to `training_jsonl_path`.

    Loads the ECO and text-revision checkpoints from .pipeline_state/<run_id>/
    so the training prompt is reconstructed from the same inputs the model saw
    at inference time.

    Returns the number of new examples appended (0 if all already collected).
    """
    from pipeline import _load_stage  # import here to avoid circular at module load

    # Load existing example IDs to deduplicate
    seen_ids: set[str] = set()
    if training_jsonl_path.exists():
        for line in training_jsonl_path.read_text().splitlines():
            try:
                record = json.loads(line)
                if "_meta" in record:
                    seen_ids.add(record["_meta"]["id"])
            except (json.JSONDecodeError, KeyError):
                pass

    # Load checkpoints
    ecos: list[ECO] = _load_stage(run_id, "0_eco_ingest") or []
    revised_steps: list[RevisedStep] = _load_stage(run_id, "4_text_revision") or []

    eco_map = {eco.eco_id: eco for eco in ecos}
    revised_map = {rs.step_id: rs for rs in revised_steps}

    # Load review JSON from output dir
    review_path = Path(output_dir) / "review_required.json"
    if not review_path.exists():
        return 0
    review = json.loads(review_path.read_text())

    new_examples: list[dict] = []
    for item in review.get("flagged_steps", []):
        if not item.get("approved", False):
            continue

        step_id = item["step_id"]
        unique_id = f"{run_id}:{step_id}"
        if unique_id in seen_ids:
            continue

        revised = revised_map.get(step_id)
        if not revised:
            continue
        eco = eco_map.get(revised.revision_source)
        if not eco:
            continue

        example = format_training_example(
            original_step=revised.original_step,
            revised_text=item.get("revised_text", revised.revised_body_text),
            eco=eco,
            is_new_step=revised.is_new_step,
        )
        # Attach metadata so we can deduplicate on future runs
        example["_meta"] = {"id": unique_id, "run_id": run_id, "step_id": step_id}
        new_examples.append(example)

    if new_examples:
        with open(training_jsonl_path, "a") as f:
            for ex in new_examples:
                f.write(json.dumps(ex) + "\n")

    return len(new_examples)


def format_training_example(
    original_step: Step,
    revised_text: str,
    eco: ECO,
    is_new_step: bool = False,
) -> dict:
    """
    Build a fine-tuning message pair for one approved revision.

    The user prompt is built by calling `_build_initial_message()` from
    `stages/text_revision.py` directly, ensuring training and inference prompts
    are identical. Any future prompt changes automatically propagate to training.
    """
    from stages.text_revision import _build_initial_message

    plan = ActionPlan(
        step_id=original_step.step_id,
        eco_id=eco.eco_id,
        action="add_step_flagged" if is_new_step else "rewrite_text",
    )
    user_prompt = _build_initial_message(plan, eco, original_step, is_new_step)
    return {
        "messages": [
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": revised_text},
        ]
    }


def count_examples(jsonl_path: Path) -> int:
    """Count training examples in the JSONL file."""
    if not jsonl_path.exists():
        return 0
    return sum(1 for line in jsonl_path.read_text().splitlines() if line.strip())


def split_train_val(jsonl_path: Path, val_fraction: float = 0.2) -> tuple[Path, Path]:
    """
    Split training_data.jsonl into train and validation sets.

    Stratifies by confidence level so the validation set represents hard cases
    (low/medium confidence) proportionally. Returns (train_path, val_path).
    """
    if not jsonl_path.exists():
        raise FileNotFoundError(f"{jsonl_path} not found — run collect first")

    records = [json.loads(line) for line in jsonl_path.read_text().splitlines() if line.strip()]

    # Group by confidence for stratified split
    by_confidence: dict[str, list[dict]] = {"high": [], "medium": [], "low": [], "unknown": []}
    for record in records:
        conf = record.get("_meta", {}).get("confidence", "unknown")
        by_confidence.setdefault(conf, []).append(record)

    train_records, val_records = [], []
    for group in by_confidence.values():
        cutoff = max(1, int(len(group) * val_fraction))
        val_records.extend(group[:cutoff])
        train_records.extend(group[cutoff:])

    train_path = jsonl_path.with_name("training_data_train.jsonl")
    val_path = jsonl_path.with_name("training_data_val.jsonl")

    train_path.write_text("\n".join(json.dumps(r) for r in train_records) + "\n")
    val_path.write_text("\n".join(json.dumps(r) for r in val_records) + "\n")

    return train_path, val_path
