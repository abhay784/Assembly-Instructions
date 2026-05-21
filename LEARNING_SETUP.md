# Fine-Tuning & Learning Implementation

## Goal

Implement a learning loop so the Claude model improves on each pipeline iteration based on user approvals/rejections from `review_required.json`.

## Current State

- Pipeline generates revised instructions using Claude API
- User approves/rejects revisions in `review_required.json`
- **But:** Model doesn't learn from feedback (stateless API calls)

## Desired Implementation

### Phase 1: Collect Training Data
After each pipeline run:
1. Extract approved revisions from `review_required.json`
2. Save as fine-tuning examples:
   ```json
   {
     "messages": [
       {"role": "user", "content": "Original: 'Assemble Part X (50mm)...'\nECO: 'Part X dimension 50mm → 60mm'\nRevise the instruction."},
       {"role": "assistant", "content": "Assemble Part X (60mm)..."}
     ]
   }
   ```
3. Accumulate examples in a training dataset (e.g., `training_data.jsonl`)

### Phase 2: Fine-Tune Claude
Once you have enough approved examples (~100+):
1. Call Anthropic fine-tuning API:
   ```python
   client.fine_tuning.jobs.create(
       model="claude-opus-4-7",
       training_data="training_data.jsonl",
       learning_rate=0.001,
   )
   ```
2. Wait for job to complete (~1-4 hours)
3. Deploy new fine-tuned model

### Phase 3: Use Fine-Tuned Model
1. Update `llm/claude_client.py` to use the new fine-tuned model ID
2. Next pipeline run automatically uses the improved model
3. Repeat: collect → fine-tune → deploy

## Key Decisions

- **Model to fine-tune:** Claude Opus 4.7 (most capable)
- **Training format:** conversation pairs (original + ECO context → revised text)
- **Min examples before fine-tuning:** 100+ approved revisions
- **Retraining frequency:** After each 50-100 new approvals

## Files to Modify/Create

1. **New: `finetune/collector.py`** — Extract approved examples from review_required.json
2. **New: `finetune/trainer.py`** — Handle fine-tuning API calls
3. **Modify: `pipeline.py`** — Add post-publish hook to collect training data
4. **Modify: `llm/claude_client.py`** — Use fine-tuned model ID if available
5. **New: `training_data.jsonl`** — Accumulated training examples

## Expected Outcome

Over multiple runs:
- Month 1: Generic Claude revisions (baseline)
- Month 2: Claude learns your assembly instruction style
- Month 3: Tailored revisions for your specific products/processes
- Ongoing: Model improves as more examples are approved

## Success Criteria

- [ ] Training data collected after each review
- [ ] Fine-tuning job runs successfully
- [ ] New fine-tuned model deployed and used in pipeline
- [ ] Revisions become more accurate/relevant over time
- [ ] Tracking system to measure improvement (e.g., approval rate increases)

## Notes

- Keep original Claude model as fallback
- Store fine-tuned model IDs in `.env` or config file
- Log which model was used for each pipeline run (for traceability)
- Fine-tuning costs ~10% of inference costs
- Can always create new fine-tuned versions (non-destructive)
