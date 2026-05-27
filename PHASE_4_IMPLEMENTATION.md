# Phase 4 Implementation Plan: Full Assembly Generation

**Status**: 📋 Design — no code yet
**Date**: 2026-05-27
**Author**: Generated from roadmap; needs engineer review before build.

## What this phase does

Today the pipeline **revises** an existing manual against an ECO. Phase 4 adds a parallel mode that **generates** a manual from scratch given only a SolidWorks assembly: BoM extraction → step sequencing → prose generation → image rendering → eval → PDF.

The existing 8-stage revision pipeline is preserved unchanged. Phase 4 ships as a sibling entry point (`pipeline_generate.py`) sharing the same LLM client, schemas where applicable, Composer bridge, image renderer, angle optimizer, eval gate, doc stitcher, PDF generator, and finetune loop.

## Design principles (carried from the existing pipeline)

These are not optional — every Phase 4 stage must follow them, because they're what makes the revision pipeline resilient on long real-world runs:

1. **Stage-checkpointed via `_run_stage(run_id, name, fn)`.** Failures lose only in-flight work for one stage.
2. **Intra-stage checkpoints for slow per-unit LLM stages** (parser pattern: one file per work-unit, optional `checkpoint_dir` kwarg, threaded from the entrypoint). Apply this to the text generator and image generator.
3. **Every LLM call sets explicit `max_tokens` AND checks `response.truncated`.** No silent truncation.
4. **Annotate, don't block.** LLM-derived failures become flags (`EvalFlag`), not exceptions. Only deterministic shape violations crash.
5. **Stages take typed lists in and return typed lists out.** Disk IO lives in the entrypoint, not the stage modules (except intra-stage caches).
6. **Bridge-first with deterministic fallback.** Mirror `eco_ingest`'s pattern: live Composer/SW bridge preferred, pre-extracted JSON fallback for offline development.
7. **All step references are by `step_id`.** Cross-step pointers in prose use `[[see: <step_id>]]`, same as the revision pipeline.

## New stages

```
Stage G0 — CAD Extract          stages/cad_extract.py
Stage G1 — Assembly Sequencer   stages/assembly_sequencer.py
Stage G2 — Step Planner         stages/step_planner.py
Stage G3 — Text Generator       stages/text_generator.py
Stage G3.5 — Angle Optimizer    (reuses finetune/angle_optimizer.py)
Stage G4 — Image Generator      (reuses stages/image_renderer.py with a new wrapper)
Stage G5 — Eval Gate            (reuses stages/eval_gate.py — see "Eval reuse")
Stage G6 — Doc Stitcher         (reuses stages/doc_stitcher.py with a generate-mode flag)
Stage G7 — PDF Generator        (reuses stages/pdf_generator.py, unchanged)
```

Naming uses a `G` prefix to keep checkpoint filenames unambiguous when both pipelines run in the same `.pipeline_state/<run_id>/` directory.

---

### Stage G0 — CAD Extract

**Module**: `stages/cad_extract.py`
**Inputs**: `--assembly <path/to.SLDASM>` OR `--bom-json <path/to/bom.json>`
**Output**: `BoM` (see schemas below)

**Behavior**: Mirrors `eco_ingest`:
1. If `--bom-json`, load and validate. Done.
2. Else call `POST /extract_assembly` on the Composer bridge.
3. The bridge handler reuses the existing helpers in `composer/server.py`:
   - `_get_assembly_components(doc)` → BoM rows (already returns name, quantity, mass, properties)
   - `_get_assembly_mates(doc)` → mate graph edges
   - `_get_part_dimensions(doc)` → per-part dimension dict
4. No bridge available → raise. Generation cannot proceed offline without a `bom.json`; the synthesizer used by `eco_ingest` doesn't help here (it diffs folders, doesn't model topology).

**Why not reuse `/diff`**: `/diff` returns *deltas* between two assemblies. We need absolute state of one assembly.

**New bridge endpoint** (Windows-only, gated behind `_use_sw_for_diff()` since SolidWorks itself must be open, not Composer):

```python
@app.post("/extract_assembly")
def extract_assembly(req: ExtractRequest) -> dict:
    """
    Returns:
        {
          "assembly_path": str,
          "components": [
            {"name": str, "quantity": int, "mass_kg": float | null,
             "properties": dict, "dimensions": dict[str, float]}
          ],
          "mates": [
            {"name": str, "type": str, "parts": [str, str]}
          ],
        }
    """
```

The existing component/mate helpers already return most of this; the only new work is attaching `dimensions` per component (call `_get_part_dimensions` against each component's part doc) and including the `parts` field in mates (today's `_get_assembly_mates` returns `name` and `type` only — needs a small extension to record which two components each mate joins).

**Checkpointing**: stage-level only. CAD extract is one bridge round-trip, not per-unit.

---

### Stage G1 — Assembly Sequencer

**Module**: `stages/assembly_sequencer.py`
**Inputs**: `BoM`
**Output**: `AssemblyGraph` — components annotated with build order

**Deterministic, no LLM.** Algorithm:

1. Build an undirected mate graph: nodes = components, edges = mates.
2. **Root selection**: the heaviest connected component is the base part. Tie-break: highest mate degree.
3. **BFS from the root**, emitting components in order. Within each BFS layer, sort by `(role_priority, -mass)` where `role_priority` is:
   - 0: structural (heaviest, lowest in BoM)
   - 1: subassembly mounts
   - 2: dynamic / moving parts
   - 3: fasteners (always last in their layer)
   Role is heuristic-classified by part-name keywords (`bolt`, `screw`, `washer`, `nut`, `pin` → fastener; `bracket`, `frame`, `plate` → structural; otherwise default `2`). The classifier lives in a small helper so it can be swapped for a learned model later.
4. **Cycle handling**: mate graphs are usually acyclic when treated as a parent/child tree of "what's mated to what was mated before." Real cycles (a part touching three others equally) get resolved by picking the lowest-mass cycle edge as a "secondary mate" deferred to a later step.

**Output schema** (one entry per BFS visit):

```python
class AssemblyNode(BaseModel):
    component_name: str
    build_index: int                      # 0-based, global order
    layer: int                             # BFS depth from root
    role: Literal["structural", "subassembly", "dynamic", "fastener"]
    mates_used: list[str]                  # mate names connecting this to already-built parts
    parent_components: list[str]           # what it attaches to (must be earlier in order)

class AssemblyGraph(BaseModel):
    root: str
    nodes: list[AssemblyNode]
```

**Why deterministic**: sequencing is exactly the kind of operation where LLM hallucination is catastrophic ("attach the wheel before the axle"). Phase 2.5's document evaluator will *audit* the sequence, but generation of the sequence stays mechanical.

---

### Stage G2 — Step Planner

**Module**: `stages/step_planner.py`
**Inputs**: `AssemblyGraph`
**Output**: `list[StepPlan]`

**Mostly deterministic + one LLM call for grouping.** Steps don't always map 1:1 to components — five identical bolts in one bracket are one step, not five. Heuristic groupings:

1. **Fastener groups**: consecutive fasteners with the same `parent_components` collapse into one step ("Install four M6 bolts to mount the bracket").
2. **Sub-assembly groups**: when a layer-N component and several of its layer-N+1 children form a self-contained subassembly (judged by a section threshold — e.g. all are fasteners or small parts under 50g), group into a "Subassembly: <component>" step.
3. **Section assignment**: the LLM gets the full ordered list and is asked to suggest top-level section breaks (e.g. "Frame", "Drive Train", "Electronics"). One call, max_tokens 4000. If the LLM call fails, fall back to one section named after the root part.

```python
class StepPlan(BaseModel):
    step_id: str                          # "<section_slug>_step_<NN>"
    section: str
    step_number: int
    components: list[str]                 # what gets installed in this step
    parent_components: list[str]          # what they attach to
    mates_used: list[str]
    role_hint: Literal["structural", "subassembly", "dynamic", "fastener"]
```

`step_id` and `section` follow the same conventions as the revision pipeline so downstream stages and review tools work identically.

---

### Stage G3 — Text Generator

**Module**: `stages/text_generator.py`
**Inputs**: `list[StepPlan]`, `BoM`, `AssemblyGraph`, `LLMClient`
**Output**: `list[Step]` — the existing `schemas/instruction.Step`

This is the heaviest stage. One LLM call per step plan, with a tool-calling loop modeled on `text_revision.py`. The output `Step` is the *same schema* the instruction parser produces, so doc stitcher + PDF gen work unchanged.

**Tools available** (same pattern as text_revision):
- `query_mate_constraints(part_number)` — already exists in `tools/`
- `lookup_part_properties(part_number)` — small new tool that reads from the BoM dict (mass, dimensions, custom properties). Cheaper than blasting all properties into every prompt.
- `lookup_prior_step(step_id)` — returns heading + parts of an earlier step so the model can write `[[see: <step_id>]]` pointers accurately.

**Prompt skeleton** (full version goes in the module):

```
You are writing one step of an assembly manual from scratch.

You will receive:
- PLAN: which components to install, what they mate to, mate types
- BOM_EXCERPT: properties of the components involved in this step
- MANIFEST: step_ids already generated (with headings + sections)

Output a JSON Step object matching this schema: <schemas/instruction.Step>

RULES (violations are invalid):
1. Use ONLY part names and numeric values present in BOM_EXCERPT or PLAN.
   Never invent torques, lengths, counts, or part numbers.
2. If a torque/spec isn't provided but is needed (e.g. you have a bolt with
   no torque), emit a Callout {"type": "warning", "text": "Torque spec to
   be confirmed"} AND add the flag "torque_spec_missing" — do NOT make up
   a value.
3. Reference prior steps with `[[see: <step_id>]]` using only step_ids
   listed in MANIFEST.
4. parts_referenced must list every component in PLAN.components plus any
   parent referenced in the prose.
5. images: emit exactly one StepImage with image_id = "<step_id>_img_1",
   kind = "renderable_cad", visible_parts = PLAN.components +
   PLAN.parent_components.
```

**`max_tokens = 4000` per step.** Per-step intra-stage checkpoint at `<run_dir>/G3_text_generator_steps/<step_id>.json`.

**Truncation handling**: if `response.truncated`, the step output is replaced with a stub `Step` whose body_text is `"[generation truncated — re-run this step]"` and a flag `generation_truncated` is recorded on the plan. The pipeline continues. Eval gate surfaces the flag, the engineer triggers re-generation for just that step on the next run (checkpoints will load all other steps from disk).

---

### Stage G3.5 — Angle Optimizer

Reuse `finetune/angle_optimizer.py` unchanged. Every generated step starts with `needs_manual_view=True` (because no human authored a Composer view for it); the optimizer attempts to auto-author one via the bridge and flips the flag if successful. Same Stage-3.5 wrapper as in `pipeline.py`, copied to `pipeline_generate.py`.

---

### Stage G4 — Image Generator

**Module**: thin wrapper in `pipeline_generate.py`; reuses `stages/image_renderer.py`.

The existing renderer takes `ActionPlan`s and renders only those with `action in ("rewrite_text_and_rerender", "add_step_flagged")`. For generation, every step is "new", so we synthesize a `list[ActionPlan]` with `action="add_step_flagged"` per generated step and pass through. No changes to `image_renderer.py`. Per-step renders are skipped only when `needs_manual_view=True` survived G3.5.

---

### Stage G5 — Eval Gate

**Reuse `stages/eval_gate.py`** with one extension: the assembly-logic LLM judge needs the `AssemblyGraph` for context (so it can check "the bracket is installed before its bolts"). The existing eval gate's three checks (spec allowlist, assembly logic, image quality) all apply directly to generated steps. Add a fourth check specific to generation: **sequence audit** — invoke `finetune/document_evaluator.py` inline as a step-level pre-flight, surfacing its `sequence_error` / `impossible_operation` types as `EvalFlag`s with severity `warning`.

This makes Phase 2.5's evaluator (currently only collected during `--publish`) a first-class gate during generation. That's important because, unlike revision, there's no human-written prior to anchor sanity.

---

### Stage G6 / G7 — Doc Stitcher & PDF

Reuse unchanged. `doc_stitcher.run()` already takes `original_steps` and `evaluated_steps` and merges them; for generation we pass an empty `original_steps` list and the full evaluated set. PDF generator is content-agnostic.

The `review_required.json` flow works as-is: any step with eval flags becomes a review item. Engineers approve through the same `--publish` command, and approved generations feed `training_data.jsonl` through `finetune/collector.py` — which means **Phase 4 is what closes the learning loop**: generated → reviewed → approved → fine-tuned → better generations.

---

## New schemas

`schemas/cad.py` (new file):

```python
class BoMComponent(BaseModel):
    name: str
    quantity: int
    mass_kg: float | None = None
    properties: dict[str, str] = {}
    dimensions: dict[str, float] = {}     # feature name → mm

class MateEdge(BaseModel):
    name: str
    type: str                              # "Concentric", "Coincident", etc.
    parts: tuple[str, str]                 # ordered alphabetically for stability

class BoM(BaseModel):
    assembly_path: str
    components: list[BoMComponent]
    mates: list[MateEdge]
```

`schemas/generation.py` (new file): `AssemblyNode`, `AssemblyGraph`, `StepPlan` (see above).

`schemas/instruction.Step` is **unchanged** and is the final output type. This is intentional: doc stitcher, PDF, and review tooling all consume `Step`, so the generation pipeline funnels into the same final type.

## New CLI: `pipeline_generate.py`

```
python pipeline_generate.py \
  --assembly path/to/new_design.SLDASM \
  --document-id robot-arm-v1

# Or with pre-extracted BoM (offline / Mac)
python pipeline_generate.py \
  --bom-json bom.json \
  --document-id robot-arm-v1

# Resume
python pipeline_generate.py --run-id <existing> --bom-json bom.json --document-id robot-arm-v1
```

Outputs land in the same `output/<run_id>/` directory as the revision pipeline — same PDF, same `review_required.json`, same training-data collection on `--publish`. The two CLIs are interchangeable from the engineer's perspective downstream of generation.

The `--publish` path is shared (move `_publish` from `pipeline.py` into a small shared module `pipeline_common.py` and import from both). Both pipelines write the same checkpoint shapes for stages they share (G5/G6/G7), so publish doesn't need to know which pipeline produced the run.

## Bridge changes summary

Single new endpoint:

- `POST /extract_assembly` — returns components + mates + per-component dimensions for one `.SLDASM`. Implementation reuses `_get_assembly_components`, `_get_assembly_mates`, `_get_part_dimensions`. The only schema extension is recording `parts: [str, str]` on each mate; that requires a small change inside `_get_assembly_mates` to capture the two `MateEntity2` references.

Bridge runs unchanged on Windows. No new COM gotchas expected — all the primitives are already exercised by `/diff`.

## File layout

```
pipeline_generate.py                       NEW
pipeline_common.py                          NEW — shared _run_stage, _publish, etc.
schemas/cad.py                              NEW
schemas/generation.py                       NEW
stages/cad_extract.py                       NEW
stages/assembly_sequencer.py                NEW
stages/step_planner.py                      NEW
stages/text_generator.py                    NEW
tools/part_properties.py                    NEW — BoM lookup tool for text_generator
tools/prior_step.py                         NEW — step manifest lookup tool
composer/server.py                          EDIT — add /extract_assembly endpoint
                                                   + parts field on mate output
stages/eval_gate.py                         EDIT — accept optional assembly_graph
                                                   for sequence audit
pipeline.py                                 EDIT — move _publish to pipeline_common
finetune/collector.py                       UNCHANGED — already step-agnostic
finetune/document_evaluator.py              EDIT (small) — expose a step-level
                                                   audit helper for inline use
templates/assembly.html.j2                  UNCHANGED — Step schema unchanged
```

11 new files, 4 small edits. No existing module's contract changes; only additions and one optional kwarg on `eval_gate.run()`.

## Build order & milestones

Each milestone is independently testable. Stop at any milestone if results don't justify the next.

| M | Scope | Test | Effort |
|---|---|---|---|
| M1 | Bridge `/extract_assembly` + `stages/cad_extract.py` + `schemas/cad.py` | Run against a known assembly; diff output against a hand-written BoM | 2–3 days |
| M2 | `assembly_sequencer.py` + `schemas/generation.py` | Sequence the same assembly; engineer review of the build order on paper | 2 days |
| M3 | `step_planner.py` | Plan output is human-readable and groupings look right | 1–2 days |
| M4 | `text_generator.py` + new tools + intra-stage checkpoints | Generate steps for a 10-part assembly; spec-allowlist eval gate must pass with zero hallucinated torques | 4–6 days |
| M5 | Wire G3.5 + G4 (angle + image) into `pipeline_generate.py` | Full pipeline produces a PDF; engineer reviews end-to-end | 2 days |
| M6 | Inline sequence audit in eval gate + publish/learning loop wiring | Approved generations land in `training_data.jsonl` | 1–2 days |

Total: ~12–17 working days, matching the roadmap's 3–4 week estimate.

## Open questions for engineer review

1. **Section break detection**: LLM-suggested vs. derived from sub-assembly file structure in SolidWorks? Bridge could expose subassembly nesting and we could skip the LLM call. Cheaper, more accurate, but doesn't work for flat assemblies. **Recommendation**: try LLM first, switch to file-structure if it produces poor sections.
2. **Torque/spec gap policy**: today's plan emits warnings for missing specs and proceeds. Alternative: hard-block the run when any fastener step lacks a torque. **Recommendation**: warn-and-proceed (matches "annotate, don't block"), let eval gate flag them for engineer review.
3. **Image generation for grouped fastener steps**: one image showing all four bolts, or one image per bolt? **Recommendation**: one image per step (grouped). Per-bolt would inflate page count enormously.
4. **Multi-assembly support**: does Phase 4 need to handle nested subassemblies (assembly-of-assemblies) in v1, or flatten everything? **Recommendation**: flatten in v1; treat subassemblies as opaque grouped steps. Nested generation is a v2 feature.

## What this plan does NOT cover

- **SolidWorks Simulation / FEA validation** of the generated sequence. Roadmap flags this as optional pilot work after Phase 4 is running; not in this plan.
- **Bilingual / non-English output**. The text generator prompt is English-only.
- **In-place edits to the revision pipeline.** Phase 4 is purely additive; nothing in `stages/0_eco_ingest.py` … `stages/8_pdf_generator.py` changes except `eval_gate` gaining one optional kwarg.
- **A UI**. CLI only, matching the rest of the project.
