# Implementation Roadmap: Learning Loop + Image Rating + Composer Automation + Full Generation

**Plan file location**: `/Users/abhaykorlapati/.claude/plans/learning-setup-md-in-your-project-polymorphic-reef.md` (978 lines, comprehensive)

## TL;DR: Four-Phase Roadmap (May 2026 → Q4 2026)

| Phase | Name | Timeline | Effort | Goal |
|-------|------|----------|--------|------|
| 1 ✅ | Learning Loop | ✅ DONE | ~1 day | Fine-tune Claude on approved revisions |
| 2 🎯 | Image Rating Agent | 2–3 days | 2–3 days | Rate image quality, generate feedback |
| 2.5 ✨ | Document Evaluator | 1–2 days | 1–2 days | **Catch physics errors** (critical for understanding) |
| 3 ⏳ | Composer Angle Automation | 3–5 days | 3–5 days | Auto-calculate camera angles, reduce manual work |
| 4 📅 | Full Assembly Generation | 3–4 weeks | 3–4 weeks | Generate instructions from CAD (not revision) |

---

## Phase 1: Learning Loop ✅ COMPLETE

**What**: Automatically collect approved revisions and fine-tune Claude

**Status**: Fully implemented and committed
- `finetune/collector.py` — extracts approved examples
- `finetune/metrics.py` — tracks approval rate, confidence trends
- `finetune/auto_eval.py` — spec compliance + LLM judge
- `finetune/trainer.py` — Anthropic fine-tuning orchestration

**Next**: Immediately ready for use. Just publish approved reviews and fine-tune.

---

## Phase 2: Image Rating Agent 🎯 START THIS WEEK

**What**: Rate image quality (0–10 scale), flag issues (visibility, angle, context)

**Why**: Extends vision eval in `eval_gate.py` to produce structured feedback for learning

**Files to create**:
- `finetune/image_rater.py` — vision LLM scoring with rubric
- Output: `image_ratings.jsonl` (per-image scores + issues)

**Effort**: 2–3 days (reuses existing vision code)

**Success signal**: Image scores trending up as fine-tuning improves

---

## Phase 2.5: Document Evaluator ✨ CRITICAL FOR PHYSICS LEARNING

**What**: Evaluate entire assembly document for consistency, sequence errors, physics violations

**Why**: Catches impossible sequences, contradictions, prerequisite violations → model learns assembly physics

**Files to create**:
- `finetune/document_evaluator.py` — document-level LLM audit
- Output: `document_feedback.jsonl` (inconsistencies, sequence errors, suggestions)

**Effort**: 1–2 days (can run parallel with Phase 3)

**Critical for**: Preventing hallucinated impossible sequences in Phase 4

**Success signal**: Rejection rate by error type decreases as fine-tuning runs

---

## Phase 3: Composer Angle Automation ⏳ AFTER PHASE 2

**What**: Auto-calculate optimal camera angles, program views into Composer

**Why**: Reduces manual view authoring, enables auto-generation of new part steps

**Files to create**:
- `finetune/angle_optimizer.py` — geometry-based angle calculation
- Modify `stages/agent_planner.py` to unset `needs_manual_view` when angle succeeds

**Effort**: 3–5 days (simple isometric) to 1–2 weeks (smart geometry-aware)

**Blocker**: Requires `/author_view` endpoint on Windows Composer bridge

**Success signal**: New parts auto-angle views, vision scores ≥6/10

---

## Phase 4: Full Assembly Generation 📅 Q4 2026

**What**: Generate complete assembly instructions from CAD (not revision of existing)

**Why**: From "improve instructions" → "create instructions from scratch"

**Components needed**:
1. CAD parser (extract BoM, specs) — 1 week
2. Assembly sequencer (determine step order) — 1–2 weeks
3. Text generator (LLM writes initial prose) — 1 week
4. Image generator (auto-render for each step) — 1 week
5. Full pipeline integration & testing — 1 week

**Effort**: 3–4 weeks full-time, 2–3 months part-time

**Gate**: Don't start until Phase 2.5 shows physics learning is working

**Success**: Generate 5-part assembly with ≥80% approval rate, no engineer review needed

---

## SolidWorks Simulation Integration (Optional)

**What**: Use FEA to validate assembly steps are physically legal

**Why**: Ground-truth validation — if simulation says step fails, it actually fails

**When**: Pilot in mid-June if Phase 2–2.5 approval rates plateau. Consider for Phase 4.

**Cost**: 1–2 week setup, 2–30 sec per step (slows pipeline 4–5x), SolidWorks Simulation license ~$3k/year

**Recommendation**: Get learning loop working first (Phases 1–2.5), then pilot on 5–10 assemblies to measure ROI

---

## Timeline & Dependencies

```
This Week (May 20–24):
  ├─ Phase 2: Image Rating (2–3 days)
  └─ Phase 2.5: Document Evaluator (1–2 days, parallel OK)

Next Week (May 27–31):
  ├─ Deploy & test image + document rating
  ├─ Start Phase 3: Angle Automation
  └─ Run 10–20 pipeline cycles, collect feedback data

Early June:
  ├─ Phase 3: Complete angle automation
  ├─ Pilot SolidWorks Simulation (optional)
  └─ Gate decision: ready for Phase 4?

June–August (Phase 4):
  ├─ CAD parser + assembly sequencer
  ├─ Text generator + image generator
  └─ Full generation pipeline

Q4 2026 Launch:
  └─ Full end-to-end generation from SolidWorks ready
```

---

## Key Success Metrics

**Phase 2**: Image scores trending up (e.g., 5.2 → 7.1 over 20 runs)

**Phase 2.5**: Rejection rate by error type decreasing
- sequence_error: 15% → 5%
- impossible_operation: 10% → 2%
- Overall approval rate: 60% → 75%+

**Phase 3**: Auto-angles work, fewer manual view flags

**Phase 4**: Generate from scratch, ≥80% approval without review

---

## Reference

- **Full plan**: `/Users/abhaykorlapati/.claude/plans/learning-setup-md-in-your-project-polymorphic-reef.md`
- **Phase 1 code**: Already in `finetune/` directory
- **Learning setup**: `LEARNING_SETUP.md` and `LEARNING_GUIDE.md`

---

## Next Context: How to Use This

When starting fresh in a new context:
1. Read this document (quick overview)
2. Read the full plan file for details on Phase 2/2.5/3/4
3. Start with Phase 2 (Image Rating Agent) — most bang for buck
4. Reference `finetune/collector.py` and `stages/eval_gate.py` for patterns
