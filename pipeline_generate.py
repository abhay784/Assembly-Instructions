"""
Assembly Instructions AI Agent — Generation Pipeline (Phase 4)

Sibling to `pipeline.py`. Where the revision pipeline takes a before/after
diff plus an existing manual, this one takes a single SolidWorks assembly
(or a pre-extracted BoM JSON) and writes a manual from scratch.

Usage:

  # Preferred — call the Composer bridge to walk the .SLDASM
  python pipeline_generate.py \\
    --assembly path/to/robot-arm.SLDASM \\
    --document-id robot-arm-v1

  # Offline / Mac — feed a pre-extracted BoM JSON
  python pipeline_generate.py \\
    --bom-json path/to/bom.json \\
    --document-id robot-arm-v1

  # Resume a partially-completed run
  python pipeline_generate.py --run-id <existing> --bom-json bom.json --document-id robot-arm-v1

Outputs land in `output/<run_id>/` and feed the same `--publish` flow
exposed by `pipeline.py --publish` so the engineer review + finetune
collection loop is shared.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from llm import get_client
from stages import (
    assembly_sequencer,
    cad_extract,
    doc_stitcher,
    eval_gate,
    image_renderer,
    pdf_generator,
    step_planner,
    text_generator,
)
from schemas.instruction import Step
from schemas.pipeline_state import (
    ActionPlan,
    EvaluatedStep,
    RenderedImage,
    RevisedStep,
)
from finetune.angle_optimizer import optimize_angles_for_steps, print_angle_report
from composer.client import ComposerClient

from pipeline_common import run_stage, load_stage


_STATE_DIR = ".pipeline_state"


def main():
    parser = argparse.ArgumentParser(description="Assembly Instructions AI Agent — Generation Pipeline")
    parser.add_argument("--assembly", help="Path to a SolidWorks .SLDASM file (bridge must be reachable)")
    parser.add_argument("--bom-json", help="Path to a pre-extracted BoM JSON (offline mode)")
    parser.add_argument("--document-id", default="document", help="Identifier for this document")
    parser.add_argument("--output-dir", default="output", help="Directory for final outputs")
    parser.add_argument("--run-id", help="Resume a specific run (skip completed stages)")
    args = parser.parse_args()

    if not (args.assembly or args.bom_json):
        parser.error("Provide --assembly or --bom-json")

    run_id = args.run_id or f"gen_{args.document_id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
    print(f"Run ID: {run_id}")

    llm = get_client()
    revision_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    # Stage G0 — CAD Extract
    bom = run_stage(run_id, "G0_cad_extract", lambda: cad_extract.run(
        bom_json_path=args.bom_json,
        assembly_path=args.assembly,
    ))
    print(f"  Stage G0: {len(bom.components)} component(s), {len(bom.mates)} mate(s)")

    # Stage G1 — Assembly Sequencer (deterministic)
    graph = run_stage(run_id, "G1_assembly_sequencer", lambda: assembly_sequencer.run(bom))
    print(f"  Stage G1: root={graph.root}, {len(graph.nodes)} ordered node(s)")

    # Stage G2 — Step Planner
    plans = run_stage(run_id, "G2_step_planner", lambda: step_planner.run(graph, llm))
    print(f"  Stage G2: {len(plans)} step plan(s)")

    # Stage G3 — Text Generator
    gen_checkpoint_dir = Path(_STATE_DIR) / run_id / "G3_text_generator_plans"
    steps = run_stage(
        run_id,
        "G3_text_generator",
        lambda: text_generator.run(plans, bom, graph, llm, checkpoint_dir=gen_checkpoint_dir),
    )
    print(f"  Stage G3: {len(steps)} step(s) generated")

    # Synthesize ActionPlans + RevisedSteps so the renderer / eval gate /
    # doc stitcher can be reused unchanged.
    action_plans = _synthesize_action_plans(steps)
    revised = _wrap_as_revised(steps)

    # Stage G3.5 — Angle Optimizer (best-effort, same as revision pipeline)
    try:
        with ComposerClient() as composer:
            if composer.health():
                action_plans = optimize_angles_for_steps(steps, action_plans, composer)
                optimized = sum(1 for p in action_plans if "[angle auto-optimized:" in p.rationale)
                if optimized:
                    print(f"  Stage G3.5: {optimized} angle(s) auto-optimized")
                    print_angle_report(action_plans, steps)
            else:
                print("  Stage G3.5: Composer bridge not available, skipping angle optimization")
    except Exception as e:
        print(f"  Stage G3.5: angle optimization failed ({e}), continuing")

    # Stage G4 — Image Generator (reuses image_renderer)
    rendered = run_stage(run_id, "G4_image_renderer", lambda: image_renderer.run(
        action_plans=action_plans,
        revised_steps=revised,
        document_id=args.document_id,
        revision_id=revision_id,
        smg_path="",
        new_cad_path=args.assembly or "",
        original_steps=steps,
    ))
    print(f"  Stage G4: {sum(1 for r in rendered if not r.skipped)} image(s) rendered")

    # Stage G5 — Eval Gate (with sequence audit against AssemblyGraph)
    evaluated = run_stage(run_id, "G5_eval_gate", lambda: eval_gate.run(
        revised_steps=revised,
        rendered_images=rendered,
        ecos=[],                      # no ECOs in generation mode
        original_steps=steps,         # generated steps act as their own "originals"
        llm=llm,
        assembly_graph=graph,
    ))
    flagged = sum(1 for e in evaluated if e.eval_flags)
    print(f"  Stage G5: {flagged} step(s) flagged")

    # Stage G6 — Document Stitcher
    html, diff = run_stage(run_id, "G6_doc_stitcher", lambda: doc_stitcher.run(
        original_steps=steps,
        evaluated_steps=evaluated,
        action_plans=action_plans,
        run_id=run_id,
        eco_ids=[],
    ))
    print(f"  Stage G6: HTML assembled, {diff['summary']['total_flags']} flag(s) in diff")

    # Stage G7 — PDF Generator
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


def _synthesize_action_plans(steps: list[Step]) -> list[ActionPlan]:
    """Every generated step is a freshly-added step that needs a fresh render."""
    return [
        ActionPlan(
            step_id=s.step_id,
            eco_id="",                        # no ECO in generation mode
            action="add_step_flagged",
            needs_manual_view=True,           # angle_optimizer flips this on success
            rationale="Phase 4 generation",
        )
        for s in steps
    ]


def _wrap_as_revised(steps: list[Step]) -> list[RevisedStep]:
    """Wrap each generated Step as a RevisedStep so downstream stages
    (image_renderer, eval_gate, doc_stitcher) work unchanged.

    The wrapper carries the generated Step as its own ``original_step`` so
    the doc stitcher's image resolver and headings find what they expect.
    revised_body_text is the generated prose verbatim.
    """
    return [
        RevisedStep(
            step_id=s.step_id,
            original_step=s,
            revised_body_text=s.body_text,
            confidence="medium",              # generation defaults to medium until eval gate decides otherwise
            flags=[],
            revision_source="",               # no ECO
            needs_manual_view=True,
            is_new_step=True,
        )
        for s in steps
    ]


if __name__ == "__main__":
    main()
