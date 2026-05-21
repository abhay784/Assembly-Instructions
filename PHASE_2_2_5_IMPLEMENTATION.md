# Phase 2 & 2.5 Implementation Summary

**Status**: ✅ Complete and integrated into pipeline

**Date**: May 20, 2026

## What Was Built

### Phase 2: Image Rating Agent (`finetune/image_rater.py`)

**Purpose**: Rate rendered CAD images on a 0–10 scale with structured feedback to generate training signal for fine-tuning.

**Key Functions**:

1. **`rate_image(image_path, step_text, llm) → dict`**
   - Takes a PNG image and assembly step text
   - Sends to Claude vision with detailed rubric
   - Returns: overall_score (0-10), 3 dimensions (visibility, angle, context, each 0-5), issues with severity, suggestions

2. **`collect_image_ratings(evaluated_steps, ratings_jsonl, run_id, llm) → int`**
   - Runs after publish, iterates through rendered images
   - Deduplicates by (run_id:step_id)
   - Appends records to `image_ratings.jsonl`
   - Returns count of newly rated images

3. **`print_image_report(ratings_jsonl) → None`**
   - Prints summary table: overall avg score, issue breakdown by type, per-run trends

**Data Output** (`image_ratings.jsonl`):
```json
{
  "step_id": "step_001",
  "run_id": "doc_20260520T120000_abc123",
  "timestamp": "2026-05-20T12:00:00Z",
  "score": 7.5,
  "dimensions": {"visibility": 4, "angle": 3, "context": 5},
  "issues": [{"issue_type": "angle_too_steep", "severity": "warning", "part_affected": "bracket", "description": "..."}],
  "suggestions": ["Rotate 15 degrees to show bolt hole"],
  "_meta": {"id": "run_id:step_id"}
}
```

**Success Signal**: Image scores trend upward over runs as fine-tuning improves model revisions

---

### Phase 2.5: Document Evaluator (`finetune/document_evaluator.py`)

**Purpose**: Audit entire assembly document for consistency, sequence errors, and physics violations. **Critical for physics understanding**.

**Key Functions**:

1. **`evaluate_document(evaluated_steps, original_steps, llm) → dict`**
   - Takes all steps (original + revised) in sequence
   - Sends to Claude with auditor system prompt
   - Returns: overall_quality_score (0-100), issues array, cross-step patterns, physics_concerns, approval_readiness

2. **`collect_document_feedback(evaluated_steps, original_steps, feedback_jsonl, run_id, llm) → int`**
   - Runs after publish, one audit per run (deduped by run_id)
   - Appends full audit record to `document_feedback.jsonl`
   - Returns 1 (new audit) or 0 (already audited)

3. **`print_document_report(feedback_jsonl) → None`**
   - Prints summary: recent avg quality score, trend vs. first run, issue type breakdown, physics concerns, per-run readiness status

**Data Output** (`document_feedback.jsonl`):
```json
{
  "run_id": "doc_20260520T120000_abc123",
  "timestamp": "2026-05-20T12:00:00Z",
  "audit": {
    "overall_quality_score": 78,
    "total_issues": 3,
    "issues": [
      {
        "type": "sequence",
        "severity": "critical",
        "steps_involved": [3, 5],
        "description": "Step 5 refers to assembly not completed until step 6",
        "suggestion": "Move step 5 after step 6"
      }
    ],
    "cross_step_patterns": [...],
    "physics_concerns": [
      {
        "concern": "sequence_violation",
        "severity": "critical",
        "step": 5,
        "description": "Part installed before mate is seated",
        "recommendation": "Verify mate sequence"
      }
    ],
    "approval_readiness": {
      "ready_to_publish": false,
      "blocking_issues": ["Critical sequence error in step 5"],
      "confidence": 0.92
    }
  }
}
```

**Why This is Critical for Physics Understanding**:
- Catches impossible sequences (e.g., "install bolt before mating two parts")
- Flags prerequisite violations (step X refers to step Y's result but Y comes after X)
- Detects consistency issues that reveal model confusion about assembly logic
- Provides learning signal: "these sequence patterns are wrong" → fine-tuned model learns correct dependencies

---

## Integration into Pipeline

Both phases are integrated into `pipeline.py`'s `_publish()` function:

```python
# Phase 2: Collect image ratings (0-10 scale + structured feedback)
evaluated_steps = _load_stage(run_id, "6_eval_gate")
if evaluated_steps:
    llm = get_client()
    image_ratings_path = Path(output_dir) / "image_ratings.jsonl"
    new_ratings = collect_image_ratings(evaluated_steps, image_ratings_path, run_id, llm)
    print(f"  Images: rated {new_ratings} image(s)")
    print_image_report(image_ratings_path)

# Phase 2.5: Collect document-level feedback (consistency, sequence, physics)
original_steps = _load_stage(run_id, "1_instruction_parser")
if evaluated_steps and original_steps:
    llm = get_client()
    doc_feedback_path = Path(output_dir) / "document_feedback.jsonl"
    new_audits = collect_document_feedback(evaluated_steps, original_steps, doc_feedback_path, run_id, llm)
    print(f"  Document: audit complete (1 record)")
    print_document_report(doc_feedback_path)
```

**Workflow**:
1. Engineer approves revisions in `review_required.json`
2. Run: `python pipeline.py --publish <run_id>`
3. Pipeline regenerates PDF
4. **New**: Collects image ratings → `output/image_ratings.jsonl`
5. **New**: Audits document consistency → `output/document_feedback.jsonl`
6. Prints summary reports showing trends

---

## Using Phase 2 & 2.5 Output

### For Learning Loop Integration

Both outputs feed into fine-tuning:

**Image feedback** (Phase 2):
- Engineer can mark approved images in review_required.json
- Example: `"good_angle_for_this_step": true` 
- Collector can extend to include image feedback pairs (future enhancement)
- **Learning signal**: "When parts X, Y, Z are involved, angle 45/35 works well"

**Document feedback** (Phase 2.5):
- Approved document audits teach sequence logic
- Example: engineer rejects sequence error, approves fix
- **Learning signal**: "When part A mates to B and C, install in order: A→B→C"

### For Decision-Making

**Image ratings**:
- If scores plateau below 6/10: angle optimization (Phase 3) becomes critical
- If scores trend up: image feedback loop is working, fine-tuning improving rendering

**Document feedback**:
- If sequence_error rejections drop from 15% → 5%: model learning assembly physics
- If physics_concerns increase: flag new part types, may need ground-truth training examples
- If approval_readiness.ready_to_publish = true for 3+ consecutive runs: consider Phase 4 kickoff

---

## Success Metrics

**Phase 2** (Image Rating):
- Baseline: avg image score after first run
- Target: avg score trending up 0.5–1.0 points per week as fine-tuning runs
- Blocker: if scores plateau below 5/10, Phase 3 (angle automation) becomes critical

**Phase 2.5** (Document Evaluator):
- Baseline: run 1 overall_quality_score + issue breakdown
- Target: quality score trending up 5–10 points per week
- Critical metric: rejection rate by error type
  - sequence_error: 15% → 5% (model learning order)
  - impossible_operation: 10% → 2% (model learning physics)
  - Overall approval rate: 60% → 75%+ (compounding improvement)

---

## Next Steps

1. **This week**: Run pipeline with Phase 2 & 2.5 enabled
   - Confirm image_ratings.jsonl and document_feedback.jsonl created
   - Verify reports print without errors
   - Spot-check ratings and audit results for reasonableness

2. **Next week**: 
   - Collect 5–10 runs of image + document feedback
   - Monitor trend direction (should be improving as baseline fine-tuning runs)
   - Start Phase 3 (Angle Automation) in parallel if feedback signal is strong

3. **Gate decision for Phase 4**: 
   - Don't start full generation (Phase 4) until Phase 2.5 shows consistent improvement in sequence_error and impossible_operation rejection rates
   - If physics understanding isn't improving by early June, investigate what's wrong with feedback loop

---

## Design Insights

### Why Two Vision Phases?

**Phase 2 (Image Rater)** focuses on **image quality as a metric**:
- Numeric score enables trending, learning signals
- Rubric (visibility, angle, context) guides model improvements
- Vision LLM becomes a quality gate for image feedback

**Phase 2.5 (Document Evaluator)** focuses on **logic quality as a constraint**:
- Document-level audit catches errors invisible at step level
- Physics concerns teach model assembly dependencies
- Approval readiness gates phase 4 kickoff

Together: images get better (Phase 2) AND sequences get logically correct (Phase 2.5).

### Why Document Audit is Critical Before Phase 4

**Phase 4 (Full Generation)** will generate 20+ steps from scratch. Without Phase 2.5:
- Model might generate sequence: "Install bolt → separate parts → tighten bolt" (impossible)
- No feedback signal catches this until engineer reviews (too late for phase 4 launch)

**With Phase 2.5**:
- Document auditor catches sequence violations immediately
- Fine-tuning loop teaches: "these orderings violate assembly physics"
- By time phase 4 launches, model has learned prerequisite logic from 100+ examples

---

## Files Changed

### New Files
- `finetune/image_rater.py` (180 lines)
- `finetune/document_evaluator.py` (200 lines)

### Modified Files
- `pipeline.py` — added Phase 2 & 2.5 integration in `_publish()` function (lines 218-244)

### Outputs Created (on each `--publish`)
- `output/image_ratings.jsonl` — appended to on each publish
- `output/document_feedback.jsonl` — appended to on each publish

---

## Testing

Both modules compiled successfully (Python syntax check passed). 

To test end-to-end:
1. Run a sample pipeline: `python pipeline.py --eco <test_eco> --instructions <test_instr>`
2. Approve reviews: `python pipeline.py --publish <run_id>`
3. Check outputs:
   ```
   ls output/image_ratings.jsonl output/document_feedback.jsonl
   tail -f output/image_ratings.jsonl
   tail -f output/document_feedback.jsonl
   ```
4. Verify reports printed correctly during publish

---

## Summary

✅ **Phase 2 & 2.5 implemented, integrated, and ready for testing**

- Image Rating: 0–10 scale, 3-dimension rubric, issue tracking → identifies rendering improvements needed
- Document Evaluator: 0–100 audit score, sequence/physics concerns, approval readiness → prevents hallucinated impossible sequences
- Both feed fine-tuning loop: approval rate + rejection rates by error type = learning signal

Next: Test on sample run, then Phase 3 (Angle Automation) can begin in parallel.
