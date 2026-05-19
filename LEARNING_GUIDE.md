# Learning Loop Quick Start

The pipeline now learns from your approvals. After each `--publish`, Claude gets better at writing assembly instructions in your style.

## The Cycle (50–100 Runs)

### 1. Run Pipeline (Normal)
```bash
python pipeline.py --eco eco.json --instructions guide.pdf
```
Outputs `review_required.json` with flagged steps.

### 2. Review & Approve
Edit `output/review_required.json`:
```json
{
  "flagged_steps": [
    {
      "step_id": "step_001",
      "approved": true,  // ← Change this
      "revised_text": "...",
      ...
    }
  ]
}
```

### 3. Publish (Auto-Collects Training Data)
```bash
python pipeline.py --publish <run_id>
```

**What happens automatically:**
- ✅ Approved revisions → `training_data.jsonl`
- ✅ Metrics recorded → `run_metrics.jsonl`
- ✅ Prints: `"Collected N new training example(s). Total: M"`

Repeat steps 1–3 about **50–100 times** (collect ≥100 approved examples).

---

## Monitor Progress

```bash
python -m finetune.metrics
```

Prints a table:
```
Model                                    Runs  Avg Approval  Avg Confidence
─────────────────────────────────────────────────────────────────────────
claude-opus-4-7                          15   85.0%         0.72
```

Shows approval rate and confidence trending up = your training data is working!

---

## Train Fine-Tuned Model (Once)

When you have ≥100 approved examples:

```bash
python -m finetune.trainer
```

This:
1. Splits data into train (80%) / validation (20%)
2. Runs **baseline eval** on validation set with base Claude
3. Uploads training file to Anthropic
4. Starts a fine-tuning job
5. Prints a **job ID** — save this

Example output:
```
Job created: ft_dGV0X2l0: (status: queued)
```

---

## Wait for Training (~1–4 hours)

While training runs, you can continue reviewing and publishing runs. The training happens in the cloud.

To check status:
```bash
python -m finetune.trainer --poll ft_dGV0X2l0
```

When done, it prints:
```
Fine-tuning complete! Model ID: claude-3-5-sonnet-20241022-ft-xxxxxxxx
```

And a comparison:
```
Metric                          Baseline Fine-tuned Delta
─────────────────────────────────────────────────────────
Spec Compliance                    0.850       0.920  ↑ +0.070
ECO Accuracy (1–5)                3.800       4.200  ↑ +0.400
Completeness (1–5)                4.100       4.350  ↑ +0.250
```

---

## Deploy Fine-Tuned Model

Add to `.env`:
```env
FINETUNED_MODEL=claude-3-5-sonnet-20241022-ft-xxxxxxxx
```

(Replace with the model ID from trainer output.)

**Next pipeline run automatically uses it.** No code changes needed.

---

## Optional: Auto-Approve High-Confidence Revisions

Edit `stages/eval_gate.py` to integrate `should_auto_approve()`:

```python
from finetune.auto_eval import should_auto_approve

# In the evaluation loop:
auto_approved, scores = should_auto_approve(
    revised_text=revised.revised_body_text,
    original_text=original.body_text,
    eco=eco,
    llm=llm,
    spec_threshold=0.9,
    judge_threshold=4.0,
)
if auto_approved:
    # Skip adding to review_required.json
    continue
```

This reduces engineer review load by auto-approving revisions that score high on:
- Spec compliance ≥0.9 (no hallucinated values)
- Style preservation ≥4/5
- ECO accuracy ≥4/5
- Completeness ≥4/5

---

## Data Files Reference

| File | Purpose | When Created |
|------|---------|--------------|
| `training_data.jsonl` | Accumulated fine-tuning examples | After first `--publish` |
| `training_data_train.jsonl` | Training set (80%) | When you run `python -m finetune.trainer` |
| `training_data_val.jsonl` | Validation set (20%) | When you run `python -m finetune.trainer` |
| `run_metrics.jsonl` | Quality metrics per run | After each `--publish` |
| `.pipeline_state/finetune_<job_id>_baseline.json` | Baseline eval scores before fine-tuning | When you run `python -m finetune.trainer` |

---

## Troubleshooting

**"Not enough examples: N/100"**
- Keep running the pipeline and approving revisions. Need ≥100 examples.

**"Fine-tuning job failed"**
- Check `run_metrics.jsonl` — examples may have bad formatting
- Verify `training_data.jsonl` has valid JSONL lines (one JSON object per line)

**"Fine-tuned model not being used"**
- Check `.env` has `FINETUNED_MODEL=<id>` set
- Restart pipeline (env vars are loaded at startup)

**"How do I compare models?"**
- `python -m finetune.metrics` shows approval rate trend
- Trainer automatically prints before/after eval scores

---

## Next Steps

1. Run the pipeline on a real dataset
2. Approve good revisions for 50–100 runs
3. `python -m finetune.metrics` — confirm approval trends are improving
4. `python -m finetune.trainer` — start training
5. `python -m finetune.trainer --poll <job_id>` — check status
6. Set `FINETUNED_MODEL` in `.env` and continue using pipeline
7. Optionally integrate auto-approval into `stages/eval_gate.py`

Each cycle (collect → train → deploy) takes a few days and improves your model further.
