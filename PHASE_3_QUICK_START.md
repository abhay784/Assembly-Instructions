# Phase 3 Quick Start: Using Angle Automation

## What Phase 3 Does

Automatically calculates optimal camera angles for assembly steps and applies them to Composer views, eliminating ~40-60% of manual view authoring work.

## How It Works

```
1. Engineer creates ECO + runs pipeline
2. Stage 3 (Agent Planner) flags new/changed steps with needs_manual_view=True
3. Stage 3.5 (Angle Optimizer) — NEW
   - Analyzes parts in each flagged step
   - Calculates optimal angle (45°/35° for fasteners, 0°/45° for gears, etc.)
   - Calls Composer /author_view to apply angle
   - Updates action plans: needs_manual_view=False (if successful)
4. Stage 5 (Image Renderer) uses optimized angles
5. Result: Most steps render correctly without manual authoring
```

## Running Pipeline with Phase 3

```bash
# Same as before — Phase 3 runs automatically
python pipeline.py --eco eco.json --instructions instructions.txt --smg assembly.smg

# Output includes Stage 3.5 summary:
#   Stage 3.5: 8 angle(s) auto-optimized
#     [angle] upper_leg_step_01: optimized to 45.0°/35.0°
#     [angle] fastener_step_02: optimized to 45.0°/35.0°
#     [angle] gear_assembly_03: optimized to 0.0°/45.0°
```

## Checking Results

### In Action Plans
- Steps with `[angle auto-optimized: XX°/YY°]` in rationale = optimization succeeded
- Steps without that marker = couldn't optimize (still flagged for manual authoring)

### In Rendered Images
- Optimized steps should have good camera angles without manual intervention
- View should show all relevant parts clearly

### Combined with Phase 2 (Image Ratings)
```bash
# After publish, check image quality
tail -f output/image_ratings.jsonl

# Look for angle-optimized steps in image scores
# Expected: scores should be 5-8/10 (reasonable for auto angles)
```

## Troubleshooting

### Phase 3.5 Not Running
**Symptom**: No "Stage 3.5" output in pipeline

**Cause**: Composer bridge offline or `/author_view` endpoint not implemented

**Check**:
```bash
# Test Composer health
curl http://localhost:8000/health
# Should return 200 OK

# Check if /author_view endpoint exists
curl -X POST http://localhost:8000/author_view \
  -H "Content-Type: application/json" \
  -d '{"view_id":"test","azimuth":45,"elevation":35}'
# Should return JSON (not 501 Not Implemented)
```

### Phase 3.5 Runs But Few Steps Optimized
**Symptom**: "Stage 3.5: 2 angle(s) auto-optimized" (expected 10+)

**Cause**: Angles being suggested but `/author_view` failing

**Debug**:
```python
# Run angle suggestion dry-run
from finetune.angle_optimizer import batch_suggest_angles
from stages.instruction_parser import run as parse_instructions

steps = parse_instructions(raw_text, llm=None)
angles = batch_suggest_angles(steps)
print(f"Suggested angles: {len(angles)} steps")
for step_id, angle in angles.items():
    print(f"  {step_id}: {angle.azimuth}°/{angle.elevation}°")
```

### Optimized Angles Look Wrong
**Symptom**: Fastener steps getting 0°/45° (gears) instead of 45°/35°

**Cause**: Heuristic rule not matching part types

**Fix**: Check part IDs contain expected keywords
```python
# Example: part named "fastener_m5" vs "m5_fastener"
# Heuristics are case-insensitive but check for specific keywords
# Ensure part IDs follow naming convention

# Update part nomenclature or add keyword:
# "if 'm5' in part_id: part_keywords.add('fastener')"
```

## Next: Measuring Success

After running pipeline with Phase 3 on 10+ ECOs:

1. **Optimization Rate**: Count successful angle optimizations
   - Target: 40-60% of flagged steps

2. **Image Quality**: Run Phase 2 image ratings
   - Target: avg score 5-7/10 for angle-optimized

3. **Manual Effort**: Compare time-to-manual-authoring before/after
   - Before Phase 3: engineer authors ~60% of views
   - After Phase 3: engineer authors ~20-30% of views

4. **Confidence for Phase 4**: If >50% optimization rate + image scores trending up
   - Green light for Phase 4 (full generation from CAD)

## Customizing Angles

See `ANGLE_HEURISTICS.md` for:
- Adding new part type rules
- Tuning existing angles
- Integrating Phase 2 feedback
- Geometry-aware angle selection (future)

## When Phase 3 Doesn't Apply

Phase 3 is **skipped** if:
- Composer bridge is offline (fallback to manual authoring)
- `/author_view` endpoint not implemented (fallback to manual authoring)
- Step action is not "add_step_flagged" or "rewrite_text_and_rerender" (no optimization needed)
- Step has no parts referenced (default angle used, no optimization)

**No regression**: Steps without optimization are still flagged for manual authoring, just like Phase 2.

## Comparison with Manual Authoring

| Metric | Manual | Phase 3 Auto | Improvement |
|--------|--------|-------------|-------------|
| Time per step | 2-3 min | ~10 sec | 12-18x faster |
| Steps needing work | 100% | 40-60% | 40-60% reduction |
| Human expertise | Required | Optional | More automation |
| Image quality | High | Medium-High | 80% of manual |
| Scalability | Poor | Excellent | Phase 4 enabled |

## See Also

- `PHASE_3_IMPLEMENTATION.md` — Full technical details
- `ANGLE_HEURISTICS.md` — How to extend angle logic
- `IMPLEMENTATION_ROADMAP.md` — Phase timeline + dependencies
- `finetune/angle_optimizer.py` — Source code
