# Angle Heuristics Guide: Phase 3 Extension

This document explains how to extend and improve the angle suggestion engine in `finetune/angle_optimizer.py`.

## Current Heuristics (Phase 3 v1)

### Heuristic Scoring System

Each step is analyzed for:
1. **Part keywords** — extract from part IDs (e.g., "fastener_m5" → contains "fastener")
2. **Rule matching** — match keywords against predefined rules
3. **Angle selection** — return CameraAngle based on rule priority

### Current Rules (Priority Order)

```python
1. Fasteners (3 or fewer parts involved)
   → ISOMETRIC_STANDARD (45°/35°)
   Reasoning: Close-up angle shows bolt head, threads, and hole alignment

2. Rotational parts (gears, pulleys, bearings)
   → 0°/45° (face-on, slightly elevated)
   Reasoning: Shows tooth engagement, groove alignment perpendicular to view

3. Vertical assembly keyword in step text
   → 45°/45° (elevated isometric)
   Reasoning: Shows stacking order for vertical assembly

4. Default (everything else)
   → ISOMETRIC_STANDARD (45°/35°)
   Reasoning: Universal angle works for 80% of assembly steps
```

### Keyword Classification Examples

```
Part ID → Keywords → Category
─────────────────────────────
"bracket_upper_right" → ["bracket", "structural"] → structural
"fastener_m5_x10" → ["fastener"] → fastener
"gear_12_tooth" → ["gear", "rotational"] → rotational
"shaft_a2" → ["shaft", "linear"] → linear
"washer_metric" → ["washer", "fastener"] → fastener
```

---

## Extending Heuristics: Step-by-Step Guide

### Level 1: Add New Keywords (Easy)

**Goal**: Improve classification for new part types without changing rules.

**Example**: Add "bearing" classification

```python
# In suggest_angle(), add to keyword extraction:
if "bearing" in part_id:
    part_keywords.add("rotational")  # bearings are like gears
```

**Use when**: You see assembly steps with new part types (ball bearings, needle bearings, linear bearings) that should follow existing rules.

### Level 2: Add New Rules (Medium)

**Goal**: Handle new assembly patterns not covered by existing rules.

**Example**: Add rule for "cable/wire assembly" angles

```python
def suggest_angle(step: Step, parts_db: dict | None = None) -> Optional[CameraAngle]:
    # ... existing code ...
    
    # After existing rules, add:
    if "cable" in part_keywords or "wire" in part_keywords:
        # Cables are linear/3D - show from elevated angle to see routing
        return CameraAngle(azimuth=45, elevation=60)  # Higher elevation than fasteners
    
    # ... rest of function ...
```

**Use when**: You identify assembly patterns that current rules don't handle well (e.g., cable routing, PCB assembly, internal wiring). Add rule BEFORE the default case.

### Level 3: Add Learning from Image Ratings (Advanced)

**Goal**: Let Phase 2 image feedback drive angle selection.

**Approach**:
1. Collect image ratings (Phase 2) with angle metadata
2. Group by part type + angle combination
3. Compute average score: (part_type, angle) → avg_rating
4. Use highest-scoring angle for each part type

**Example**:
```python
def suggest_angle_with_learning(
    step: Step,
    angle_ratings: dict[tuple[str, str], float]  # {("fastener", "45/35"): 7.2, ...}
) -> Optional[CameraAngle]:
    """Suggest angle based on historical image quality ratings."""
    part_keywords = extract_keywords(step.parts_referenced)
    
    # Look up best-rated angle for this combination
    primary_type = determine_primary_type(part_keywords)  # "fastener" or "gear" etc.
    
    best_angle = None
    best_score = 0
    for (ptype, angle_str), rating in angle_ratings.items():
        if ptype == primary_type and rating > best_score:
            best_angle = parse_angle(angle_str)
            best_score = rating
    
    return best_angle or suggest_angle(step)  # Fallback to heuristic
```

**When to implement**: After Phase 2 + Phase 3 v1 running for 2-3 weeks with 50+ rated images.

### Level 4: Geometry-Aware Angles (Expert)

**Goal**: Analyze actual 3D geometry to choose optimal angles automatically.

**Requires**:
1. Access to CAD model (STEP/SLDASM file)
2. 3D geometry library (Open3D or similar)
3. Bounding box calculation of visible parts per step

**Approach**:
```python
def suggest_angle_geometry_aware(
    step: Step,
    cad_path: str,
    parts_in_step: list[str],
) -> Optional[CameraAngle]:
    """
    Analyze 3D geometry of parts in step.
    Return angle that best shows all parts without occlusion.
    """
    import open3d as o3d
    
    # Load CAD, extract geometry for parts_in_step
    geom = load_cad_geometry(cad_path, parts_in_step)
    
    # Calculate bounding box
    bbox = geom.get_axis_aligned_bounding_box()
    center = bbox.get_center()
    size = bbox.get_max_bound() - bbox.get_min_bound()
    
    # Determine primary axis (longest dimension)
    primary_axis = np.argmax(size)  # 0=X, 1=Y, 2=Z
    
    # Choose angle to maximize visibility along primary axis
    if primary_axis == 2:  # Tall (Z) → elevated view
        return CameraAngle(azimuth=45, elevation=50)
    elif primary_axis == 0:  # Wide (X) → rotated view
        return CameraAngle(azimuth=90, elevation=35)
    else:  # Deep (Y) → rotated back
        return CameraAngle(azimuth=270, elevation=35)
```

**When to implement**: Phase 3.2+, requires CAD API and geometry library integration.

---

## Testing New Heuristics

### Test Setup

```python
# test_angle_heuristics.py
from finetune.angle_optimizer import suggest_angle, batch_suggest_angles
from stages.instruction_parser import run as parse_instructions

def test_heuristics():
    # Load sample instructions
    raw_text = open("test_instructions.txt").read()
    steps = parse_instructions(raw_text, llm=None)  # No LLM needed for heuristics
    
    # Get suggestions
    suggestions = batch_suggest_angles(steps)
    
    # Inspect results
    for step_id, angle in suggestions.items():
        step = next(s for s in steps if s.step_id == step_id)
        print(f"{step_id}")
        print(f"  Parts: {[p.part_id for p in step.parts_referenced]}")
        print(f"  Angle: {angle.azimuth}°/{angle.elevation}°")
```

### Evaluation Criteria

After adding new heuristics:

1. **Coverage**: Do suggested angles cover edge cases?
   ```python
   # Check: are new part types getting suggestions?
   assert all(angle is not None for angle in suggestions.values())
   ```

2. **Consistency**: Are similar steps getting similar angles?
   ```python
   # Check: fastener steps within 10° of each other?
   fastener_steps = [s for s in steps if "fastener" in s.body_text.lower()]
   fastener_angles = [suggest_angle(s).azimuth for s in fastener_steps]
   assert max(fastener_angles) - min(fastener_angles) <= 10
   ```

3. **Feedback Loop**: Do Image Ratings (Phase 2) improve post-heuristic?
   - Baseline: run Phase 2 before heuristic change (e.g., avg score 5.8/10)
   - After: run Phase 2 again, compare avg score
   - Target: +0.5-1.0 point improvement

---

## Troubleshooting Common Issues

### Issue: All steps get same angle

**Symptom**: All steps suggest 45°/35°, no variation

**Cause**: Keyword extraction not working, part_keywords always empty

**Fix**:
```python
# Debug: print extracted keywords
print(f"Part: {part_id}, Keywords: {part_keywords}")

# Verify part ID format matches expectations
# e.g., "fastener_m5" vs "m5_fastener" — check case sensitivity
```

### Issue: Wrong angle for specific part type

**Symptom**: Gear steps should be 0°/45°, but getting 45°/35°

**Cause**: Keyword rule order — a more general rule matches first

**Fix**: Move specific rule before general rule
```python
# WRONG: matches "fastener" first, prevents "gear" rule
if "fastener" in part_keywords: ...
if "gear" in part_keywords: ...  # Never reached if gear also classified as fastener

# RIGHT: more specific first
if "gear" in part_keywords: ...
if "fastener" in part_keywords: ...
```

### Issue: Heuristics don't work for new assembly type

**Symptom**: Phase 3 optimization success rate <30% for new part types

**Cause**: Rules don't match new assembly patterns

**Fix**: Add new rule or refine keyword extraction
```python
# Example: PCB assembly pattern
if "pcb" in part_keywords or "smd" in part_keywords:
    # PCBs are flat — need top-down view
    return TOP_DOWN
```

---

## Validation Checklist Before Deployment

- [ ] New heuristic handles existing part types (backward compatible)
- [ ] New heuristic tested on 5+ sample steps
- [ ] Angle output is valid (azimuth 0-360, elevation 0-90)
- [ ] No crash on edge cases (empty parts, unknown keywords)
- [ ] Image quality scores don't regress (Phase 2 integration)
- [ ] Comment explains WHY the heuristic exists (not just WHAT it does)

---

## Measuring Heuristic Effectiveness

### KPIs to Track

1. **Success Rate**: `successful_optimizations / total_flagged_steps`
   - Target: >50% (Phase 3 v1), >75% (Phase 3.1+)

2. **Image Quality**: Average Phase 2 score for angle-optimized vs. manual
   - Target: angle-optimized ≥ 80% of manual scores

3. **Time Saved**: Minutes per step saved vs. manual authoring
   - Baseline: 2-3 min/step manual
   - Target: <30 sec/step (automated)

4. **Angle Distribution**: Histogram of suggested angles
   - Should show clear clusters (45/35 for fasteners, 0/45 for gears, etc.)
   - Wide spread → heuristics working; uniform → heuristics not discriminating

---

## Future Directions

1. **Multi-angle rendering**: Suggest 2-3 alternative angles, render all, use Phase 2 to pick best
2. **Dynamic angle adjustment**: Refine angles based on step-by-step feedback
3. **Assembly-type learning**: Different heuristics for mechanical vs. electrical vs. software
4. **Integration with Phase 4**: Use Phase 3 angles as seed for full generation

---

## Reference

- Main implementation: `finetune/angle_optimizer.py`
- Integration point: `pipeline.py` Stage 3.5
- Phase 2 feedback: `finetune/image_rater.py`
- Phase 4 dependency: Will need robust angle generation for new steps
