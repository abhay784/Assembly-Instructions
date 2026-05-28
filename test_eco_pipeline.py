#!/usr/bin/env python3
"""Smoke tests for the ECO pipeline.

Run: python test_eco_pipeline.py

Exercises:
  • Router decisions (A / B / C).
  • CAD-diff blob → ECOs → labeler → ECOReport → HTML.
  • MVP1 changeset → ECOReport.
  • Schema round-trips with the new ECOChange fields and ECOLocation.

Uses a fake LLM client so this runs offline.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# Import llm.client directly via importlib to bypass llm/__init__.py
# (which imports anthropic + openai eagerly and we don't need them here).
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "_llm_client_only", str(Path(__file__).resolve().parent / "llm" / "client.py"),
)
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
LLMResponse = _mod.LLMResponse

from eco_pipeline import changeset_adapter, eco_renderer, router, semantic_labeler
from eco.diff_mapper import sw_diff_to_ecos
from schemas.eco import ECO, ECOChange, ECOLocation, ECOReport


# ───────────────────────────────────────────────────────────────────────────
# Fake LLM
# ───────────────────────────────────────────────────────────────────────────

class StubLLM:
    """Returns canned semantic labels matching every input change."""

    def __init__(self, label_template: dict | None = None):
        self.label_template = label_template or {
            "change_type": "dimension_changed",
            "severity": "MAJOR",
            "rationale": "Test rationale under twenty words.",
        }

    def complete(self, messages, system, tools=None, max_tokens=8096, model=None, cache_system=False):
        payload = json.loads(messages[0]["content"])
        labels = [
            {"index": i, **self.label_template}
            for i, _ in enumerate(payload["changes"])
        ]
        return LLMResponse(
            content=json.dumps({"labels": labels}),
            stop_reason="end_turn",
        )

    def stream(self, *_args, **_kwargs):
        raise NotImplementedError


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────

def test_router_decisions():
    a = router.decide("before.SLDDRW", "after.SLDDRW")
    b = router.decide(None, "after.SLDDRW")
    c = router.decide(None, None)
    assert a.scenario == "A", a
    assert b.scenario == "B", b
    assert c.scenario == "C", c
    assert b.before_drawing is None and b.after_drawing is not None
    print("✓ router decisions")


def test_cad_diff_blob_to_report():
    diff_blob = {
        "_source": "sw_com_api",
        "before_path": "/tmp/before.SLDASM",
        "after_path": "/tmp/after.SLDASM",
        "components_added": [
            {"name": "NEW_BOLT.SLDPRT", "quantity": 2, "mass_kg": 0.01, "properties": {}},
        ],
        "components_removed": [
            {"name": "OLD_BOLT.SLDPRT", "quantity": 2},
        ],
        "components_changed": [
            {
                "name": "BRACKET.SLDPRT",
                "quantity_before": 1, "quantity_after": 1,
                "property_changes": [{"property": "material", "before": "AL6061", "after": "AL7075"}],
                "geometry_changes": [{"property": "mass_kg", "before": "0.250", "after": "0.310"}],
                "dimension_changes": [{"dimension": "D1@Sketch1", "before": 25.0, "after": 30.0, "unit": "mm"}],
                "face_changes": [{"surface_type": "cylinder", "change": "radius", "before_mm": 2.5, "after_mm": 3.175}],
            },
        ],
        "mates_added": [{"name": "Concentric7", "type": "Concentric"}],
        "mates_removed": [],
    }

    ecos = sw_diff_to_ecos(diff_blob)
    assert len(ecos) >= 3, f"expected ≥3 ECOs (added, removed, changed, +mates), got {len(ecos)}"

    # Run the labeler with a stub LLM
    semantic_labeler.run(ecos, StubLLM())
    bracket = next(e for e in ecos if e.part_number == "BRACKET")
    assert all(c.change_type == "dimension_changed" for c in bracket.changes), \
        "stub LLM labels should propagate"
    assert all(c.severity == "MAJOR" for c in bracket.changes)
    assert all(c.confidence == 0.9 for c in bracket.changes)

    # Locations
    locations = {
        "BRACKET.SLDPRT": ECOLocation(
            tree_path="after.SLDASM > BRACKET.SLDPRT",
            mate_neighbors=["HOUSING.SLDPRT"],
        ),
    }
    report = changeset_adapter.from_ecos(
        ecos=ecos, document_id="ECO-TEST", eco_id="run-xyz",
        scenario="C", locations=locations,
    )
    assert report.scenario == "C"
    assert report.source == "cad_diff"
    assert any(c.location and c.location.tree_path for c in report.changes), \
        "at least one change should have a tree_path location"
    print(f"✓ CAD diff → report ({len(report.changes)} change(s))")


def test_labeler_fallback_on_bad_json():
    """When the LLM returns garbage, the labeler must still produce ECOs with
    heuristic change_types and low confidence — never crash."""
    class BadLLM:
        def complete(self, **_kwargs):
            return LLMResponse(content="not json at all", stop_reason="end_turn")
        def stream(self, *_a, **_k): raise NotImplementedError

    ecos = [ECO(
        eco_id="x", part_number="P",
        summary="t",
        changes=[ECOChange(field="dim:D1@Sketch1", old="5 mm", new="6 mm")],
    )]
    semantic_labeler.run(ecos, BadLLM())
    c = ecos[0].changes[0]
    assert c.change_type == "dimension_changed", c.change_type
    assert c.confidence <= 0.4
    print("✓ labeler fallback")


def test_mvp1_changeset_adapter():
    mvp1 = {
        "summary": {"critical": 1, "significant": 1, "minor": 1, "uncertain": 0,
                    "total_views_compared": 2},
        "view_diffs": [
            {
                "label": "FRONT VIEW",
                "match_type": "matched",
                "changes": [
                    {"field": "dimension", "orig_value": "25 mm", "revised_value": "30 mm",
                     "severity": "CRITICAL", "confidence": 0.92,
                     "rationale": "Outer dim grew 20%.", "zone": "B5"},
                    {"field": "note", "orig_value": "M5", "revised_value": "M6",
                     "severity": "MAJOR", "confidence": 0.88,
                     "rationale": "Thread size changed.", "zone": "C2"},
                ],
            },
            {
                "label": "TITLE BLOCK",
                "match_type": "title_block",
                "changes": [
                    {"field": "revision", "orig_value": "NC", "revised_value": "A",
                     "severity": "MINOR", "confidence": 1.0,
                     "rationale": "Revision bumped.", "zone": None},
                ],
            },
        ],
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(mvp1, f)
        path = f.name

    report = changeset_adapter.from_mvp1_changeset(path, document_id="DOC", eco_id="r1")
    assert report.scenario == "A"
    assert report.source == "drawing_diff"
    assert len(report.changes) == 3
    zoned = [c for c in report.changes if c.location and c.location.zone]
    assert len(zoned) == 2, "two of three changes should carry zones"
    print(f"✓ MVP1 changeset adapter ({len(report.changes)} change(s))")


def test_html_render_contains_was_is_and_location():
    report = ECOReport(
        eco_id="r1", document_id="DOC-001",
        scenario="C", source="cad_diff",
        summary="One change.",
        changes=[ECOChange(
            field="dim:D1@Sketch1", old="25 mm", new="30 mm",
            change_type="dimension_changed", severity="MAJOR",
            confidence=0.9, rationale="Hole moved.",
            location=ECOLocation(
                tree_path="A > B > BRACKET.SLDPRT",
                mate_neighbors=["HOUSING.SLDPRT", "PIN.SLDPRT"],
            ),
        )],
    )
    html = eco_renderer.render_html(report)
    assert "25 mm" in html
    assert "30 mm" in html
    assert "BRACKET.SLDPRT" in html
    assert "HOUSING.SLDPRT" in html
    assert "MAJOR" in html
    assert "Hole moved." in html
    print("✓ HTML renderer surfaces was/is/location/severity")


def test_zone_renders_for_scenario_a():
    report = ECOReport(
        eco_id="r1", document_id="DOC",
        scenario="A", source="drawing_diff",
        summary="",
        changes=[ECOChange(
            field="dimension", old="25 mm", new="30 mm",
            severity="CRITICAL", confidence=0.95,
            location=ECOLocation(zone="B5"),
        )],
    )
    html = eco_renderer.render_html(report)
    assert "Zone B5" in html, "Scenario A should surface zone label"
    print("✓ Scenario A zone rendering")


def main():
    tests = [
        test_router_decisions,
        test_cad_diff_blob_to_report,
        test_labeler_fallback_on_bad_json,
        test_mvp1_changeset_adapter,
        test_html_render_contains_was_is_and_location,
        test_zone_renders_for_scenario_a,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as exc:
            failed += 1
            print(f"✗ {t.__name__}: {type(exc).__name__}: {exc}")
            import traceback; traceback.print_exc()
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
