"""
Assembly Instructions AI Agent — Main Pipeline CLI

Usage:
  # Run full pipeline with real ECO
  python pipeline.py --eco eco.json --instructions instructions.txt

  # Run with synthesized ECO from model diff
  python pipeline.py --before-model old.sldprt --after-model new.sldprt --instructions instructions.txt

  # With PDF instructions (auto-extracts text)
  python pipeline.py --before-model old.sldasm --after-model new.sldasm --instructions instructions.pdf

  # Publish after engineer completes review
  python pipeline.py --publish <run_id>

Environment variables:
  LLM_BACKEND          vllm (default) | claude
  VLLM_BASE_URL        e.g. http://localhost:8001/v1
  VLLM_MODEL           model name served by vLLM
  ANTHROPIC_API_KEY    required if LLM_BACKEND=claude
  CLAUDE_MODEL         defaults to claude-opus-4-7
  SOLIDWORKS_API       defaults to http://localhost:8000
  PART_HISTORY_DB      path to part history JSON (optional, for testing)
  MATE_GRAPH_DB        path to mate graph JSON (optional, for testing)
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()
from pathlib import Path

from llm import get_client
from stages import (
    agent_planner,
    change_mapper,
    doc_stitcher,
    eco_ingest,
    eval_gate,
    image_renderer,
    instruction_parser,
    pdf_generator,
    text_revision,
)
from finetune.angle_optimizer import optimize_angles_for_steps, print_angle_report
from composer.client import ComposerClient

_STATE_DIR = ".pipeline_state"


def _load_instructions(path: str) -> str:
    """Load instruction text from .txt or .pdf file."""
    file_path = Path(path)

    if file_path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("pypdf not installed. Run: pip install pypdf")

        reader = PdfReader(file_path)
        text = "\n".join(page.extract_text() for page in reader.pages)
        return text
    else:
        return file_path.read_text()


def main():
    parser = argparse.ArgumentParser(description="Assembly Instructions AI Agent")
    parser.add_argument("--eco", help="Path to ECO JSON file")
    parser.add_argument("--before-model", help="Path to before SolidWorks model (for ECO synthesis)")
    parser.add_argument("--after-model", help="Path to after SolidWorks model (for ECO synthesis)")
    parser.add_argument("--instructions", help="Path to raw instruction text or JSON")
    parser.add_argument("--smg", help="Path to SolidWorks Composer .smg file")
    parser.add_argument("--document-id", default="document", help="Identifier for this document")
    parser.add_argument("--output-dir", default="output", help="Directory for final outputs")
    parser.add_argument("--publish", metavar="RUN_ID", help="Publish after review — regenerate PDF from approved review_required.json")
    parser.add_argument("--run-id", help="Resume a specific run (skip completed stages)")
    args = parser.parse_args()

    if args.publish:
        _publish(args.publish, args.output_dir)
        return

    if not args.instructions:
        parser.error("--instructions is required")
    if not args.eco and not (args.before_model and args.after_model):
        parser.error("Provide --eco or both --before-model and --after-model")

    run_id = args.run_id or f"{args.document_id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
    print(f"Run ID: {run_id}")

    llm = get_client()
    revision_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    # Stage 0 — ECO Ingest
    ecos = _run_stage(run_id, "0_eco_ingest", lambda: eco_ingest.run(
        eco_json_path=args.eco,
        before_model_path=args.before_model,
        after_model_path=args.after_model,
    ))
    print(f"  Stage 0: {len(ecos)} ECO(s) loaded")

    # Stage 1 — Instruction Parser
    raw_text = _load_instructions(args.instructions)
    steps = _run_stage(run_id, "1_instruction_parser", lambda: instruction_parser.run(raw_text, llm))
    print(f"  Stage 1: {len(steps)} steps parsed")

    # Stage 2 — Change Mapper
    affected = _run_stage(run_id, "2_change_mapper", lambda: change_mapper.run(steps, ecos))
    print(f"  Stage 2: {len(affected)} affected step(s)")

    # Stage 3 — Agent Planner
    plans = _run_stage(run_id, "3_agent_planner", lambda: agent_planner.run(affected, ecos, steps, llm))
    print(f"  Stage 3: {len(plans)} action plan(s)")

    # Stage 3.5 — Angle Optimizer (Phase 3)
    # Try to auto-calculate optimal camera angles for new/rerendered steps
    # This reduces manual view authoring by setting needs_manual_view=False when successful
    try:
        with ComposerClient() as composer:
            if composer.health():
                plans = optimize_angles_for_steps(steps, plans, composer)
                optimized_count = sum(1 for p in plans if "[angle auto-optimized:" in p.rationale)
                if optimized_count > 0:
                    print(f"  Stage 3.5: {optimized_count} angle(s) auto-optimized")
                    print_angle_report(plans, steps)
            else:
                print(f"  Stage 3.5: Composer bridge not available, skipping angle optimization")
    except Exception as e:
        print(f"  Stage 3.5: Angle optimization failed ({e}), continuing with manual views")

    # Stage 4 — Text Revision
    revised = _run_stage(run_id, "4_text_revision", lambda: text_revision.run(plans, steps, ecos, llm))
    print(f"  Stage 4: {len(revised)} step(s) revised")

    # Stage 5 — Image Renderer
    # Passes original steps so the SolidWorks backend can calculate angles
    rendered = _run_stage(run_id, "5_image_renderer", lambda: image_renderer.run(
        action_plans=plans,
        revised_steps=revised,
        document_id=args.document_id,
        revision_id=revision_id,
        smg_path=args.smg or "",
        new_cad_path=args.after_model or "",
        original_steps=steps,
    ))
    print(f"  Stage 5: {sum(1 for r in rendered if not r.skipped)} image(s) rendered")

    # Stage 6 — Eval Gate
    evaluated = _run_stage(run_id, "6_eval_gate", lambda: eval_gate.run(
        revised_steps=revised,
        rendered_images=rendered,
        ecos=ecos,
        original_steps=steps,
        llm=llm,
    ))
    flagged = sum(1 for e in evaluated if e.eval_flags)
    print(f"  Stage 6: {flagged} step(s) flagged")

    # Stage 7 — Document Stitcher
    eco_ids = [eco.eco_id for eco in ecos]
    html, diff = _run_stage(run_id, "7_doc_stitcher", lambda: doc_stitcher.run(
        original_steps=steps,
        evaluated_steps=evaluated,
        action_plans=plans,
        run_id=run_id,
        eco_ids=eco_ids,
    ))
    print(f"  Stage 7: HTML assembled, {diff['summary']['total_flags']} flag(s) in diff")

    # Stage 8 — PDF Generator
    run_output_dir = str(Path(args.output_dir) / run_id)
    outputs = pdf_generator.run(
        rendered_html=html,
        evaluated_steps=evaluated,
        diff=diff,
        output_dir=run_output_dir,
        run_id=run_id,
    )

    print(f"\nOutputs written to {run_output_dir}/")
    print(f"  PDF:    {outputs['pdf']}")
    print(f"  Diff:   {outputs['diff_json']}")
    print(f"  Review: {outputs['review_json']}")

    review_count = len(json.loads(Path(outputs["review_json"]).read_text())["flagged_steps"])
    if review_count:
        print(f"\n⚠  {review_count} step(s) require engineer review.")
        print(f"   Edit {outputs['review_json']}")
        print(f"   Then publish: python pipeline.py --publish {run_id}")

    try:
        from finetune.collector import count_examples
        example_count = count_examples(Path("training_data.jsonl")) if Path("training_data.jsonl").exists() else 0
    except Exception:
        example_count = 0
    print(f"\nLearning: {example_count} approved example(s) collected (need 100 to fine-tune)")
    if review_count == 0:
        print("  No flagged steps — run went clean, nothing to approve this cycle")


def _publish(run_id: str, output_dir: str):
    """Re-render PDF after engineer approves review_required.json."""
    run_output_dir = Path(output_dir) / run_id
    review_path = run_output_dir / "review_required.json"
    if not review_path.exists():
        # Fallback to legacy flat layout for older runs
        review_path = Path(output_dir) / "review_required.json"
    if not review_path.exists():
        print(f"Error: review_required.json not found in {run_output_dir} or {output_dir}", file=sys.stderr)
        sys.exit(1)

    review = json.loads(review_path.read_text())
    unapproved = [s for s in review["flagged_steps"] if not s.get("approved")]
    if unapproved:
        print(f"⚠  {len(unapproved)} step(s) not yet approved:")
        for s in unapproved:
            print(f"   - {s['step_id']}: {[f['flag_type'] for f in s['flags']]}")
        confirm = input("Publish anyway? [y/N] ").strip().lower()
        if confirm != "y":
            print("Publish cancelled.")
            return

    # Reload state from checkpoint and regenerate HTML/PDF with reviewer edits applied
    state = _load_stage(run_id, "7_doc_stitcher")
    if not state:
        print(f"Error: no checkpoint found for run {run_id}", file=sys.stderr)
        sys.exit(1)

    html, diff = state
    _apply_reviewer_edits(html, review)

    publish_output_dir = str(Path(output_dir) / run_id)
    outputs = pdf_generator.run(
        rendered_html=html,
        evaluated_steps=[],
        diff=diff,
        output_dir=publish_output_dir,
        run_id=run_id,
    )
    print(f"Published: {outputs['pdf']}")

    # Collect approved revisions as fine-tuning training data
    from finetune.collector import collect_approved_examples, count_examples
    from finetune.metrics import record_run_metrics
    from finetune.image_rater import collect_image_ratings, print_image_report
    from finetune.document_evaluator import collect_document_feedback, print_document_report

    training_path = Path("training_data.jsonl")
    model_used = os.environ.get("FINETUNED_MODEL") or os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
    new = collect_approved_examples(run_id, output_dir, training_path)
    total = count_examples(training_path)
    record_run_metrics(run_id, model_used, review["flagged_steps"])
    print(f"  Learning: collected {new} new training example(s). Total: {total}")

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

    if total >= 100:
        print("  Run `python -m finetune.trainer` to start fine-tuning.")


def _apply_reviewer_edits(html: str, review: dict) -> str:
    # Simple pass for now: reviewer edits live in review_required.json
    # A full implementation would substitute approved revised text back into HTML
    return html


def _stage_path(run_id: str, stage_name: str) -> Path:
    return Path(_STATE_DIR) / run_id / f"{stage_name}.json"


def _run_stage(run_id: str, stage_name: str, fn):
    path = _stage_path(run_id, stage_name)
    if path.exists():
        print(f"  Stage {stage_name}: loaded from checkpoint")
        data = json.loads(path.read_text())
        return _deserialize_stage(stage_name, data)

    result = fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(_serialize_stage(stage_name, result), f)
    return result


def _load_stage(run_id: str, stage_name: str):
    path = _stage_path(run_id, stage_name)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return _deserialize_stage(stage_name, data)


def _serialize_stage(stage_name: str, data):
    from schemas.eco import ECO
    from schemas.instruction import Step
    from schemas.pipeline_state import (
        ActionPlan,
        AffectedStep,
        EvaluatedStep,
        RenderedImage,
        RevisedStep,
    )

    if isinstance(data, list):
        if data and hasattr(data[0], "model_dump"):
            return [item.model_dump() for item in data]
    if isinstance(data, tuple):
        html, diff = data
        return {"html": html, "diff": diff}
    return data


def _deserialize_stage(stage_name: str, data):
    from schemas.eco import ECO
    from schemas.instruction import Step
    from schemas.pipeline_state import (
        ActionPlan,
        AffectedStep,
        EvaluatedStep,
        RenderedImage,
        RevisedStep,
    )

    schema_map = {
        "0_eco_ingest": ECO,
        "1_instruction_parser": Step,
        "2_change_mapper": AffectedStep,
        "3_agent_planner": ActionPlan,
        "4_text_revision": RevisedStep,
        "5_image_renderer": RenderedImage,
        "6_eval_gate": EvaluatedStep,
    }

    if stage_name in schema_map:
        model = schema_map[stage_name]
        return [model.model_validate(item) for item in data]

    if stage_name == "7_doc_stitcher":
        return data["html"], data["diff"]

    return data


if __name__ == "__main__":
    main()
