"""
Assembly-Instructions ECO Pipeline — generates ECO PDFs from two SolidWorks
assembly revisions, with optional drawings.

Three scenarios decided automatically from the inputs:

  A — both --before-drawing and --after-drawing provided. The drawings are
      converted to PDF via the bridge (SolidWorks SaveAs), then MVP1Compare's
      CLI is invoked as a subprocess; its report.pdf is copied to the run dir.

  B — exactly one drawing provided. Falls back to the CAD-model diff, attaches
      the lone drawing PDF to the rendered ECO.

  C — no drawings. CAD-model diff only; ECO is a text diff list with
      tree-path + mate-neighbor "where to find it".

Usage:
  python eco_pipeline.py \\
    --before-model before.SLDASM --after-model after.SLDASM \\
    [--before-drawing before.SLDDRW] [--after-drawing after.SLDDRW] \\
    --document-id ECO-2026-0042

Resume:
  python eco_pipeline.py --run-id <existing> [same args]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from eco_pipeline import (
    cad_diff,
    changeset_adapter,
    eco_renderer,
    mvp1_runner,
    router,
    semantic_labeler,
    slddrw_to_pdf,
    tree_locator,
)
from eco.diff_mapper import sw_diff_to_ecos
from llm import get_client
from schemas.eco import ECOReport


_STATE_DIR = Path(".eco_state")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ECO PDF generator")
    parser.add_argument("--before-model", required=True, help="Path to the old assembly (.SLDASM)")
    parser.add_argument("--after-model",  required=True, help="Path to the new assembly (.SLDASM)")
    parser.add_argument("--before-drawing", help="Optional .SLDDRW or .pdf for the old revision")
    parser.add_argument("--after-drawing",  help="Optional .SLDDRW or .pdf for the new revision")
    parser.add_argument("--document-id", default="ECO", help="Identifier stamped into the ECO")
    parser.add_argument("--output-dir", default="output_eco", help="Where to write the final ECO")
    parser.add_argument("--run-id", help="Resume a previous run")
    parser.add_argument("--no-pdf", action="store_true",
                        help="Skip PDF rendering — write HTML + JSON only (useful for testing)")
    args = parser.parse_args(argv)

    safe_doc_id = args.document_id.replace("\\", "").replace("/", "")
    run_id = args.run_id or f"{safe_doc_id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
    print(f"Run ID: {run_id}")

    run_dir = _STATE_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # ── Stage 0: route ─────────────────────────────────────────────────────
    decision = _run_stage(run_id, "0_route", lambda: router.decide(
        args.before_drawing, args.after_drawing,
    ).__dict__)
    print(f"  Stage 0: scenario {decision['scenario']} — {decision['rationale']}")

    # ── Stage 1: SLDDRW → PDF (Scenarios A, B) ─────────────────────────────
    before_pdf = after_pdf = None
    if decision["scenario"] in ("A", "B"):
        before_pdf, after_pdf = _stage_convert_drawings(
            run_id, run_dir, args.before_drawing, args.after_drawing,
        )
        if decision["scenario"] == "A" and not (before_pdf and after_pdf):
            print("  Stage 1: at least one conversion failed — downgrading to Scenario B")
            decision["scenario"] = "B" if (before_pdf or after_pdf) else "C"

    scenario = decision["scenario"]

    # ── Scenario A path ────────────────────────────────────────────────────
    if scenario == "A":
        report = _run_scenario_a(
            run_id, run_dir, before_pdf, after_pdf, args.document_id,
        )
        if report is None:
            print("  Scenario A failed — downgrading to Scenario B")
            scenario = "B"

    # ── Scenarios B / C path ───────────────────────────────────────────────
    if scenario in ("B", "C"):
        attachments: list[Path] = []
        if before_pdf:
            attachments.append(before_pdf)
        if after_pdf:
            attachments.append(after_pdf)
        report = _run_scenario_bc(
            run_id, args.before_model, args.after_model,
            args.document_id, scenario, attachments,
        )

    if report is None:
        print("ERROR: ECO generation produced no report", file=sys.stderr)
        return 1

    # ── Stage 6: render ─────────────────────────────────────────────────────
    output_dir = Path(args.output_dir) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = output_dir / "eco_report.json"
    report_path.write_text(report.model_dump_json(indent=2))

    html_path = output_dir / "eco.html"
    html_path.write_text(eco_renderer.render_html(report))

    pdf_path = None
    if not args.no_pdf:
        try:
            pdf_path = eco_renderer.render_pdf(report, output_dir / "eco.pdf")
        except Exception as exc:
            print(f"  Stage 6: PDF render failed ({exc}). HTML still written.")

    # ── Stage 7: review queue ──────────────────────────────────────────────
    review_path = output_dir / "eco_review.json"
    review = _build_review_queue(report)
    review_path.write_text(json.dumps(review, indent=2))

    print(f"\nOutputs written to {output_dir}/")
    print(f"  Report:  {report_path}")
    print(f"  HTML:    {html_path}")
    if pdf_path:
        print(f"  PDF:     {pdf_path}")
    print(f"  Review:  {review_path}")
    if review["flagged_count"]:
        print(f"\n  ⚠  {review['flagged_count']} change(s) need engineer review.")
    return 0


# ───────────────────────────────────────────────────────────────────────────
# Scenario implementations
# ───────────────────────────────────────────────────────────────────────────

def _run_scenario_a(
    run_id: str,
    run_dir: Path,
    before_pdf: Path,
    after_pdf: Path,
    document_id: str,
) -> ECOReport | None:
    """Drawings → MVP1Compare subprocess → adapt changeset."""
    mvp1_out_dir = run_dir / "mvp1"
    mvp1 = _run_stage(run_id, "2a_mvp1", lambda: mvp1_runner.run(
        before_pdf=before_pdf, after_pdf=after_pdf,
        out_dir=mvp1_out_dir, job_id=run_id, document_id=document_id,
    ))
    if mvp1 is None:
        return None

    changeset_path = Path(mvp1["changeset_json"])
    report = changeset_adapter.from_mvp1_changeset(
        changeset_path, document_id=document_id, eco_id=run_id,
    )

    # If MVP1 already produced a PDF, mirror it next to ours so engineers
    # have the rich side-by-side artifact too.
    report_pdf = mvp1.get("report_pdf")
    if report_pdf:
        report.attachments.append(Path(report_pdf))

    print(f"  Stage A: adapted MVP1 changeset → {len(report.changes)} change(s)")
    return report


def _run_scenario_bc(
    run_id: str,
    before_model: str,
    after_model: str,
    document_id: str,
    scenario: str,
    attachments: list[Path],
) -> ECOReport | None:
    # Stage 2b — CAD diff via composer bridge
    diff_blob = _run_stage(run_id, "2b_cad_diff", lambda: cad_diff.run(before_model, after_model))
    ecos = sw_diff_to_ecos(diff_blob)
    print(f"  Stage 2b: {len(ecos)} ECO(s) from CAD diff")

    if not ecos:
        # Empty diff is a valid outcome — produce an empty report.
        return changeset_adapter.from_ecos(
            ecos=[], document_id=document_id, eco_id=run_id,
            scenario=scenario, locations={}, attachments=attachments,
        )

    # Stage 3 — semantic labeler (LLM)
    try:
        llm = get_client()
        ecos = _run_stage(
            run_id, "3_semantic_labeler",
            lambda: [e.model_dump() for e in semantic_labeler.run(ecos, llm)],
        )
        # _run_stage roundtrips through JSON; re-hydrate.
        from schemas.eco import ECO
        ecos = [ECO.model_validate(d) for d in ecos]
        print(f"  Stage 3: semantic labels applied")
    except Exception as exc:
        print(f"  Stage 3: labeler unavailable ({exc}) — using raw fields")

    # Stage 4 — locator (tree path + mate neighbors)
    component_names = [eco.part_number for eco in ecos if eco.part_number != "assembly_mates"]
    try:
        locations = _run_stage(
            run_id, "4_tree_locator",
            lambda: {k: v.model_dump() for k, v in tree_locator.build_locations(
                before_model, after_model, component_names,
            ).items()},
        )
        from schemas.eco import ECOLocation
        locations = {k: ECOLocation.model_validate(v) for k, v in locations.items()}
    except Exception as exc:
        print(f"  Stage 4: tree locator failed ({exc}) — proceeding without locations")
        locations = {}

    # Stage 5 — adapt
    report = changeset_adapter.from_ecos(
        ecos=ecos,
        document_id=document_id,
        eco_id=run_id,
        scenario=scenario,
        locations=locations,
        attachments=attachments,
    )
    print(f"  Stage 5: ECOReport built with {len(report.changes)} change(s)")
    return report


# ───────────────────────────────────────────────────────────────────────────
# Stage helpers
# ───────────────────────────────────────────────────────────────────────────

def _stage_convert_drawings(
    run_id: str,
    run_dir: Path,
    before_drawing: str | None,
    after_drawing: str | None,
) -> tuple[Path | None, Path | None]:
    out = run_dir / "drawings_pdf"

    def _convert_one(path: str | None) -> str | None:
        if not path:
            return None
        p = Path(path)
        if p.suffix.lower() == ".pdf":
            return str(p.resolve())
        if p.suffix.lower() == ".slddrw":
            result = slddrw_to_pdf.convert(p, out)
            return str(result) if result else None
        print(f"  Stage 1: unrecognized drawing extension {p.suffix} — skipping")
        return None

    paths = _run_stage(run_id, "1_slddrw_to_pdf", lambda: {
        "before_pdf": _convert_one(before_drawing),
        "after_pdf":  _convert_one(after_drawing),
    })
    before_pdf = Path(paths["before_pdf"]) if paths.get("before_pdf") else None
    after_pdf  = Path(paths["after_pdf"])  if paths.get("after_pdf")  else None
    if before_pdf:
        print(f"  Stage 1: before drawing → {before_pdf}")
    if after_pdf:
        print(f"  Stage 1: after drawing → {after_pdf}")
    return before_pdf, after_pdf


def _build_review_queue(report: ECOReport) -> dict:
    flagged = [
        {
            "field": c.field,
            "change_type": c.change_type,
            "severity": c.severity,
            "confidence": c.confidence,
            "was": c.old,
            "is": c.new,
            "rationale": c.rationale,
            "location": c.location.model_dump() if c.location else None,
            "approved": False,
        }
        for c in report.changes
        if c.severity in ("CRITICAL", "MAJOR", "UNCERTAIN") or c.confidence < 0.6
    ]
    return {
        "eco_id": report.eco_id,
        "document_id": report.document_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "flagged_count": len(flagged),
        "flagged_changes": flagged,
    }


def _stage_path(run_id: str, stage_name: str) -> Path:
    return _STATE_DIR / run_id / f"{stage_name}.json"


def _run_stage(run_id: str, stage_name: str, fn):
    path = _stage_path(run_id, stage_name)
    if path.exists():
        print(f"  Stage {stage_name}: loaded from checkpoint")
        return json.loads(path.read_text())
    result = fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_serialize(result), default=str))
    return result


def _serialize(data):
    """Best-effort JSON-safe form for checkpoints — handles Pydantic, Path, dataclass."""
    if hasattr(data, "model_dump"):
        return data.model_dump()
    if isinstance(data, Path):
        return str(data)
    if isinstance(data, dict):
        return {k: _serialize(v) for k, v in data.items()}
    if isinstance(data, list):
        return [_serialize(v) for v in data]
    return data


if __name__ == "__main__":
    sys.exit(main())
