"""
finetune/trainer.py — Kick off an Anthropic fine-tuning job and evaluate results.

Usage:
  python -m finetune.trainer              # start a new job
  python -m finetune.trainer --poll <id>  # poll an existing job and run eval

Requires ANTHROPIC_API_KEY in environment (or .env file).
Requires training_data.jsonl with >= 100 examples.
"""

import argparse
import json
import time
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from finetune.collector import count_examples, split_train_val
from finetune.auto_eval import eval_validation_set

_MIN_EXAMPLES = 100
_TRAINING_JSONL = Path("training_data.jsonl")
_VAL_JSONL = Path("training_data_val.jsonl")


def start_fine_tuning_job(
    client: anthropic.Anthropic,
    train_path: Path,
    base_model: str = "claude-opus-4-7",
    learning_rate: float = 0.001,
) -> str:
    """Upload training data and start a fine-tuning job. Returns the job ID."""
    print(f"Uploading {train_path} ...")
    with open(train_path, "rb") as f:
        uploaded = client.files.upload(file=(train_path.name, f, "application/jsonl"))
    print(f"File uploaded: {uploaded.id}")

    print(f"Starting fine-tuning job on {base_model} ...")
    job = client.fine_tuning.jobs.create(
        model=base_model,
        training_file=uploaded.id,
        hyperparameters={"learning_rate": learning_rate},
    )
    print(f"Job created: {job.id}  (status: {job.status})")
    return job.id


def poll_job(client: anthropic.Anthropic, job_id: str, poll_interval: int = 60) -> str:
    """Poll until the job completes. Returns the fine-tuned model ID."""
    print(f"Polling job {job_id} every {poll_interval}s ...")
    while True:
        job = client.fine_tuning.jobs.retrieve(job_id)
        print(f"  Status: {job.status}")
        if job.status == "succeeded":
            model_id = job.fine_tuned_model
            print(f"Fine-tuning complete! Model ID: {model_id}")
            return model_id
        if job.status in ("failed", "cancelled"):
            raise RuntimeError(f"Fine-tuning job {job_id} {job.status}: {getattr(job, 'error', '')}")
        time.sleep(poll_interval)


def _print_eval_comparison(baseline: dict, finetuned: dict) -> None:
    print(f"\n{'Metric':<30} {'Baseline':>10} {'Fine-tuned':>12} {'Delta':>8}")
    print("-" * 64)
    keys = [
        ("mean_spec_compliance", "Spec Compliance"),
        ("mean_style", "Style (1–5)"),
        ("mean_eco_accuracy", "ECO Accuracy (1–5)"),
        ("mean_completeness", "Completeness (1–5)"),
        ("mean_judge_total", "Judge Total (3–15)"),
    ]
    for key, label in keys:
        b = baseline.get(key, 0.0)
        f = finetuned.get(key, 0.0)
        delta = f - b
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        print(f"{label:<30} {b:>10.3f} {f:>12.3f} {arrow} {delta:>+6.3f}")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Fine-tune Claude on approved assembly instruction revisions")
    parser.add_argument("--poll", metavar="JOB_ID", help="Poll an existing job and run eval when done")
    parser.add_argument("--base-model", default="claude-opus-4-7")
    parser.add_argument("--learning-rate", type=float, default=0.001)
    parser.add_argument("--training-data", default=str(_TRAINING_JSONL))
    args = parser.parse_args()

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    training_path = Path(args.training_data)
    n = count_examples(training_path)

    if not args.poll:
        if n < _MIN_EXAMPLES:
            print(f"Not enough examples: {n}/{_MIN_EXAMPLES}. Keep publishing approved runs.")
            return

        print(f"Splitting {n} examples into train/val ...")
        train_path, val_path = split_train_val(training_path)
        print(f"  Train: {count_examples(train_path)}  Val: {count_examples(val_path)}")

        # Baseline eval before fine-tuning
        from llm import get_client
        llm = get_client()
        print("\nRunning baseline eval on validation set ...")
        baseline = eval_validation_set(val_path, llm)
        print(f"  Baseline ({baseline['n']} examples): judge_total={baseline['mean_judge_total']:.2f}")

        job_id = start_fine_tuning_job(client, train_path, args.base_model, args.learning_rate)
        print(f"\nJob running. To poll: python -m finetune.trainer --poll {job_id}")
        _save_baseline(job_id, baseline)
        return

    # Poll mode
    job_id = args.poll
    model_id = poll_job(client, job_id)

    val_path = _VAL_JSONL
    if val_path.exists():
        from llm import get_client
        import os
        os.environ["FINETUNED_MODEL"] = model_id
        llm = get_client()
        print("\nRunning eval on fine-tuned model ...")
        finetuned_scores = eval_validation_set(val_path, llm)

        baseline = _load_baseline(job_id)
        if baseline:
            _print_eval_comparison(baseline, finetuned_scores)
        else:
            print(f"Fine-tuned scores: {finetuned_scores}")

    print(f"\nTo use this model, add to .env:\n  FINETUNED_MODEL={model_id}")


def _baseline_path(job_id: str) -> Path:
    return Path(f".pipeline_state/finetune_{job_id}_baseline.json")


def _save_baseline(job_id: str, scores: dict) -> None:
    p = _baseline_path(job_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(scores))


def _load_baseline(job_id: str) -> dict | None:
    p = _baseline_path(job_id)
    if p.exists():
        return json.loads(p.read_text())
    return None


if __name__ == "__main__":
    main()
