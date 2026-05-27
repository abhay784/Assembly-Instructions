# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python pipeline that takes a pair of SolidWorks assemblies (before / after) plus an existing instruction manual (PDF or text) and produces a revised PDF + a review queue listing flagged steps for engineer approval. The pipeline detects geometric/property changes between the two assemblies, maps them to affected instruction steps, rewrites the prose with an LLM, and re-renders only the CAD views that changed.

## Common commands

```powershell
# Full pipeline run (fresh)
python pipeline.py `
  --before-model "path/to/before.SLDASM" `
  --after-model  "path/to/after.SLDASM" `
  --instructions "path/to/manual.pdf" `
  --document-id  my-doc

# Resume a previous run from checkpoints (skips completed stages)
python pipeline.py --run-id <existing_run_id> [same args as above]

# Alternative inputs to the diff stage
python pipeline.py --diff diff.json --instructions ...        # pre-computed SW diff
python pipeline.py --eco eco.json   --instructions ...        # hand-authored ECO

# Publish after engineer edits review_required.json
python pipeline.py --publish <run_id>

# Start the SolidWorks Composer bridge (separate process, Windows + SW required)
python -m uvicorn composer.server:app --host 127.0.0.1 --port 8000

# Find the right Composer COM ProgID for this machine
python -m composer.discover_com

# Standalone angle-heuristic test (assert-based script, not pytest)
python test_angle_optimizer.py

# Fine-tuning loop (after >=100 approved examples collected)
python -m finetune.trainer
python -m finetune.trainer --poll <job_id>
```

## Pipeline architecture

`pipeline.py` orchestrates eight sequential stages, each checkpointed to `.pipeline_state/<run_id>/<NN>_<stage>.json`. On rerun with the same `--run-id`, completed stages load from disk and skip. Stage failures lose nothing committed but lose any in-flight stage work — except where intra-stage checkpointing exists (see below).

| # | Stage | Module | What it does |
|---|---|---|---|
| 0 | ECO Ingest | `stages/eco_ingest.py` | Normalize input changes into `list[ECO]` from one of three sources (pre-computed diff, before/after models, hand-authored ECO file) |
| 1 | Instruction Parser | `stages/instruction_parser.py` | Two-pass: section index (Pass 1, one LLM call) then per-section parse (Pass 2, one LLM call per section) with a running manifest of prior-section context |
| 2 | Change Mapper | `stages/change_mapper.py` | **Deterministic, no LLM.** Inverted-index match of ECO part numbers against parsed step parts |
| 3 | Agent Planner | `stages/agent_planner.py` | Classifies each affected step as `rewrite_text` / `rewrite_text_and_rerender` / `add_step_flagged` / `no_change`. Batches affected_steps in groups of 30 |
| 3.5 | Angle Optimizer | `finetune/angle_optimizer.py` | Best-effort: tries to auto-author Composer camera angles via the bridge. Falls back to `needs_manual_view=True` per step on failure (does NOT block) |
| 4 | Text Revision | `stages/text_revision.py` | Per-step tool-use loop. Tools: `lookup_part_history`, `query_mate_constraints`. Re-prompts on bad JSON, falls back to original text after `_MAX_LOOP_ITERATIONS=10` |
| 5 | Image Renderer | `stages/image_renderer.py` | Renders one PNG per step needing update. Tries Composer bridge first, then direct SolidWorks COM |
| 6 | Eval Gate | `stages/eval_gate.py` | **Annotates, never blocks.** Three checks: spec allowlist (deterministic), assembly logic (LLM judge), image quality (vision LLM). Failures become `EvalFlag` warnings on the step |
| 7 | Doc Stitcher | `stages/doc_stitcher.py` | Merges unchanged + revised steps via `templates/assembly.html.j2`, produces HTML + diff dict |
| 8 | PDF Generator | `stages/pdf_generator.py` | Playwright HTML→PDF + writes `review_required.json` for flagged steps |

**Intra-stage checkpointing** exists in Stage 1 (per section, `1_instruction_parser_sections/`) and Stage 4 (per plan, `4_text_revision_plans/`). Both are the slow stages where losing all work on a transient failure is expensive. Pattern: optional `checkpoint_dir` kwarg on `run()`, threaded from `pipeline.py`. Other stages don't need it — Stage 0 is fast, Stage 2 is deterministic, Stage 3 is large but one shot per batch, Stages 5-8 are quick.

## Cross-stage data contracts

`schemas/instruction.py` (`Step`, `PartRef`, `Spec`, `Callout`, `StepImage`), `schemas/eco.py` (`ECO`, `ECOChange`), and `schemas/pipeline_state.py` (`AffectedStep`, `ActionPlan`, `RevisedStep`, `RenderedImage`, `EvalFlag`, `EvaluatedStep`, `ReviewItem`) define every cross-stage hand-off. **All stages reference Steps by `step_id`** — every downstream stage builds `step_map = {s.step_id: s for s in steps}` and looks up by ID, so cross-step references in `body_text` use inline pointer markers `[[see: <step_id>]]` rather than inlining text.

## LLM abstraction

`llm/__init__.py:get_client()` selects backend from `LLM_BACKEND` env var (`claude` | `vllm`, default `vllm`). The `LLMClient` protocol exposes `complete()` and `stream()`. `LLMResponse` has `.content`, `.tool_calls`, `.stop_reason`, and `.truncated` (normalized to Anthropic's vocabulary across both backends). The Claude client wraps every `complete()` call in retry-with-backoff for transient transport errors (`httpx.TransportError`, `APIConnectionError`, `APITimeoutError`, `InternalServerError`); auth and bad-request errors are not retried.

**Every stage that calls the LLM must set an explicit `max_tokens` AND check `response.truncated`.** The default cap is 8096 which silently truncates long outputs; the truncation check converts that into a loud error (or a graceful fallback for annotation stages like eval_gate).

## SolidWorks integration

Two independent COM connections, gated separately ([composer/server.py](composer/server.py)):

- **`_use_com()`** — Composer (rendering, view authoring). Probes `SWComposerLib.Application` etc. Override via `COMPOSER_PROGID` env var. Embedded viewer ActiveX controls (e.g. `DSComposerPlayerActiveXCtrl`) dispatch successfully but lack the automation methods — render and `/author_view` will fail.
- **`_use_sw_for_diff()`** — SolidWorks itself (`SldWorks.Application`). Used only by `/diff`. Independent of Composer because users often have one installed without the other.

Environment: `BRIDGE_MODE=auto|com|folder` (default `auto`). `folder` skips COM entirely and serves pre-rendered PNGs from `RENDER_DIR`.

COM gotchas baked into the code (don't undo these without testing on Windows):
- FastAPI dispatches `/diff` on a worker thread; `pythoncom.CoInitialize()` is called per-request because COM apartment state is per-thread.
- `OpenDoc6` is called via `_open_doc6()` which wraps the `Errors`/`Warnings` out-params as `VARIANT(VT_BYREF | VT_I4, 0)` — bare integers raise "Type mismatch" on parameter 5 under late binding.
- `IComponent2.GetPathName` is read via `_sw_get_path_name()` which probes `callable()` — SolidWorks declares it as a property but pywin32 sometimes auto-detects it as a method depending on typelib state. Calling the string form crashes silently for every component.
- The synthesizer fallback (`eco/synthesizer.py`, used when bridge is unreachable) matches part files by fingerprint (OLE `Subject` → SHA-1 → filename), not by filename alone, so pure renames don't show up as add/remove ECOs.

## Environment configuration

`.env` at repo root (loaded by both `pipeline.py` and `composer/server.py` via `dotenv`; bridge anchors the path to `__file__` so `uvicorn`'s CWD doesn't matter):

```
LLM_BACKEND=claude               # or vllm
ANTHROPIC_API_KEY=sk-ant-...     # if LLM_BACKEND=claude
CLAUDE_MODEL=claude-sonnet-4-6   # defaults to claude-opus-4-7
VLLM_BASE_URL=http://...         # if LLM_BACKEND=vllm
VLLM_MODEL=...
FINETUNED_MODEL=...              # overrides CLAUDE_MODEL if set
BRIDGE_MODE=auto                 # auto | com | folder
COMPOSER_PROGID=...              # override discovered ProgID
SMG_PATH=...                     # Composer file to open at startup
SOLIDWORKS_API=http://localhost:8000  # client → bridge URL
```

## Fine-tuning loop

`finetune/collector.py` extracts approved `ReviewItem`s from `review_required.json` and appends them to `training_data.jsonl`, deduplicated across runs. When the file hits 100 examples, `python -m finetune.trainer` kicks off an Anthropic fine-tuning job. `finetune/auto_eval.py`, `finetune/document_evaluator.py`, and `finetune/image_rater.py` collect richer feedback signals during `--publish`.

## Conventions learned from existing code

- Long-running multi-call stages get intra-stage checkpoints (one file per work-unit) — saves work on transient network drops.
- `_run_stage(run_id, name, fn)` is the canonical wrapper; only it writes the stage-level checkpoint. Failing mid-fn writes nothing.
- Stage runners take `list[Step]` / `list[ECO]` etc. — the per-stage modules don't read from disk themselves (except for the intra-stage caches).
- The pipeline never blocks on LLM failures it can annotate around; only deterministic shape violations crash. This is why eval_gate adds `EvalFlag` instead of raising and text_revision falls back to the original text with `max_iterations_exceeded`.
