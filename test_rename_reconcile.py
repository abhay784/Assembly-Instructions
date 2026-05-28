#!/usr/bin/env python3
"""Unit tests for the rename-reconciliation logic in composer/server.py.

The reconciliation is a pure function over dicts so we can test it without
SolidWorks or the bridge. Run: python3 test_rename_reconcile.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _import_reconcile():
    """composer/server.py imports FastAPI at module load. Strip those imports
    so we can load just the helper functions for unit testing."""
    src_path = Path(__file__).resolve().parent / "composer" / "server.py"
    src = src_path.read_text()
    # Take only the slice that defines our helpers (they're standalone).
    # Easier: just exec the helper definitions directly.
    helpers_src = []
    capture = False
    for line in src.splitlines():
        if line.startswith("import re as _re"):
            capture = True
        if capture:
            helpers_src.append(line)
        if line.startswith("def _com_assembly_diff"):
            break
    helpers_src = helpers_src[:-1]  # drop the trailing _com_assembly_diff line

    ns: dict = {"Path": Path}
    exec("\n".join(helpers_src), ns)
    return ns


_R = _import_reconcile()
_normalize_stem = _R["_normalize_stem"]
_canonical_id = _R["_canonical_id"]
_mass_similar = _R["_mass_similar"]
_reconcile_renames = _R["_reconcile_renames"]


def test_normalize_stem():
    cases = [
        ("BRACKET(R2).SLDPRT",      "BRACKET"),
        ("BRACKET(R10).SLDPRT",     "BRACKET"),
        ("Bracket(r3).sldprt",      "BRACKET"),
        ("BRACKET_R2.SLDPRT",       "BRACKET"),
        ("BRACKET_REV_A.SLDPRT",    "BRACKET"),
        ("BRACKET_REVB.SLDPRT",     "BRACKET"),
        ("BRACKET_v3.SLDPRT",       "BRACKET"),
        ("BRACKET-R2.SLDPRT",       "BRACKET"),
        ("BRACKET.SLDPRT",          "BRACKET"),   # no rev → unchanged
        ("FOO_BAR.SLDPRT",          "FOO_BAR"),   # no rev → unchanged
        ("FOO_R2(R3).SLDPRT",       "FOO"),       # stacked
    ]
    for src, expected in cases:
        got = _normalize_stem(src)
        assert got == expected, f"{src}: expected {expected!r}, got {got!r}"
    print("✓ _normalize_stem")


def test_canonical_id():
    assert _canonical_id({"properties": {"part number": "ABC-123"}}) == "ABC-123"
    assert _canonical_id({"properties": {"PartNumber": "abc-123"}}) == "ABC-123"
    assert _canonical_id({"properties": {"part_number": "x"}}) == "X"
    assert _canonical_id({"properties": {}}) is None
    assert _canonical_id({"properties": {"part number": "   "}}) is None
    assert _canonical_id({}) is None
    print("✓ _canonical_id")


def test_mass_similar():
    assert _mass_similar(1.0, 1.1)
    assert _mass_similar(1.0, 1.4)       # 40% within tolerance
    assert not _mass_similar(1.0, 2.0)   # 100% > 50%
    assert _mass_similar(None, 1.0)      # unknown side passes
    assert _mass_similar(1.0, None)
    assert _mass_similar(0, 0)
    print("✓ _mass_similar")


def test_rename_by_part_number():
    """Filename changed but Part Number is the same → should reconcile."""
    added = [{"name": "BRACKET(R3).SLDPRT", "quantity": 1}]
    removed = [{"name": "BRACKET(R2).SLDPRT", "quantity": 1}]
    after = {"BRACKET(R3).SLDPRT": {
        "properties": {"part number": "ABC-123"},
        "mass_kg": 0.30,
    }}
    before = {"BRACKET(R2).SLDPRT": {
        "properties": {"part number": "ABC-123"},
        "mass_kg": 0.25,
    }}

    a, r, extra = _reconcile_renames(added, removed, before, after)
    assert a == [] and r == [], f"residual should be empty, got {a} / {r}"
    assert len(extra) == 1
    e = extra[0]
    assert e["name"] == "BRACKET(R3).SLDPRT"
    assert e["name_before"] == "BRACKET(R2).SLDPRT"
    assert e["_rename_match"] == "part_number"
    fn_change = next(p for p in e["property_changes"] if p["property"] == "filename")
    assert fn_change["before"] == "BRACKET(R2).SLDPRT"
    assert fn_change["after"] == "BRACKET(R3).SLDPRT"
    mass_change = next(p for p in e["geometry_changes"] if p["property"] == "mass_kg")
    assert mass_change["before"] == "0.25" and mass_change["after"] == "0.3"
    print("✓ rename by Part Number")


def test_rename_by_filename_stem_with_mass_gate():
    """No Part Number but stems match and masses are similar → reconcile."""
    added = [{"name": "BRACKET(R3).SLDPRT", "quantity": 1}]
    removed = [{"name": "BRACKET(R2).SLDPRT", "quantity": 1}]
    after = {"BRACKET(R3).SLDPRT": {"properties": {}, "mass_kg": 0.30}}
    before = {"BRACKET(R2).SLDPRT": {"properties": {}, "mass_kg": 0.25}}

    a, r, extra = _reconcile_renames(added, removed, before, after)
    assert a == [] and r == []
    assert len(extra) == 1
    assert extra[0]["_rename_match"] == "filename_stem"
    print("✓ rename by filename stem")


def test_filename_stem_rejected_when_mass_diverges():
    """Stems match but masses are 10× apart → keep as separate add/remove."""
    added = [{"name": "PLATE(R3).SLDPRT", "quantity": 1}]
    removed = [{"name": "PLATE(R2).SLDPRT", "quantity": 1}]
    after = {"PLATE(R3).SLDPRT": {"properties": {}, "mass_kg": 10.0}}
    before = {"PLATE(R2).SLDPRT": {"properties": {}, "mass_kg": 0.5}}

    a, r, extra = _reconcile_renames(added, removed, before, after)
    assert len(a) == 1 and len(r) == 1, "mass gate should reject this match"
    assert extra == []
    print("✓ mass-similarity gate rejects bogus stem match")


def test_truly_different_parts_left_alone():
    """Completely different filenames and Part Numbers → no reconciliation."""
    added = [{"name": "WIDGET.SLDPRT", "quantity": 1}]
    removed = [{"name": "GADGET.SLDPRT", "quantity": 1}]
    after = {"WIDGET.SLDPRT": {"properties": {"part number": "W-1"}, "mass_kg": 0.1}}
    before = {"GADGET.SLDPRT": {"properties": {"part number": "G-1"}, "mass_kg": 0.1}}

    a, r, extra = _reconcile_renames(added, removed, before, after)
    assert len(a) == 1 and len(r) == 1
    assert extra == []
    print("✓ unrelated parts remain as add/remove")


def test_part_number_beats_filename_match():
    """When both signals are available, Part Number wins (stronger evidence).
    Mixed scenario: an added part shares a Part Number with a removed part
    *and* a different added part shares a filename stem with a different removed
    part. Both renames should reconcile, independently."""
    added = [
        {"name": "BRACKET(R3).SLDPRT", "quantity": 1},   # PN match
        {"name": "PIN(R3).SLDPRT", "quantity": 1},        # stem match
    ]
    removed = [
        {"name": "OLD_NAME.SLDPRT", "quantity": 1},      # PN match (different file)
        {"name": "PIN(R2).SLDPRT", "quantity": 1},        # stem match
    ]
    after = {
        "BRACKET(R3).SLDPRT": {"properties": {"part number": "P-100"}, "mass_kg": 0.3},
        "PIN(R3).SLDPRT":     {"properties": {},                       "mass_kg": 0.05},
    }
    before = {
        "OLD_NAME.SLDPRT":    {"properties": {"part number": "P-100"}, "mass_kg": 0.28},
        "PIN(R2).SLDPRT":     {"properties": {},                       "mass_kg": 0.05},
    }

    a, r, extra = _reconcile_renames(added, removed, before, after)
    assert a == [] and r == []
    assert len(extra) == 2
    reasons = sorted(e["_rename_match"] for e in extra)
    assert reasons == ["filename_stem", "part_number"]
    # Confirm the right pairs
    pn_entry = next(e for e in extra if e["_rename_match"] == "part_number")
    assert pn_entry["name_before"] == "OLD_NAME.SLDPRT"
    assert pn_entry["name"] == "BRACKET(R3).SLDPRT"
    print("✓ mixed Part Number + stem reconciliation")


def main():
    tests = [
        test_normalize_stem,
        test_canonical_id,
        test_mass_similar,
        test_rename_by_part_number,
        test_rename_by_filename_stem_with_mass_gate,
        test_filename_stem_rejected_when_mass_diverges,
        test_truly_different_parts_left_alone,
        test_part_number_beats_filename_match,
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
