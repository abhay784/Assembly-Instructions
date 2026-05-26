"""
Stage 0 — ECO / Assembly-Diff Ingest

Three input modes (in priority order):

  1. --diff <diff.json>          Pre-computed SolidWorks diff JSON (fastest,
                                  no SolidWorks needed).  Produced by the
                                  composer /diff endpoint or saved manually.

  2. --before-model / --after-model
                                  Paths to two SolidWorks assemblies.  The
                                  stage calls the Composer bridge's POST /diff
                                  endpoint to get a real component-level diff.
                                  Falls back to the local folder-scan synthesizer
                                  only when the bridge is unreachable.

  3. --eco <eco.json>            A hand-authored ECO JSON file.

All three modes produce a validated List[ECO] consumed by downstream stages.
"""

from __future__ import annotations

from schemas.eco import ECO


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(
    eco_json_path: str | None = None,
    before_model_path: str | None = None,
    after_model_path: str | None = None,
    diff_json_path: str | None = None,
) -> list[ECO]:
    """
    Parameters
    ----------
    eco_json_path:       path to a hand-authored ECO JSON file
    before_model_path:   path to the old SolidWorks assembly
    after_model_path:    path to the new SolidWorks assembly
    diff_json_path:      path to a pre-computed /diff response JSON
    """
    if diff_json_path:
        ecos = _ingest_from_diff_file(diff_json_path)
    elif eco_json_path:
        ecos = _load_ecos_from_file(eco_json_path)
    elif before_model_path and after_model_path:
        ecos = _ingest_from_model_diff(before_model_path, after_model_path)
    else:
        raise ValueError(
            "Provide one of: --diff, --eco, or both --before-model and --after-model"
        )

    if not ecos:
        raise ValueError("ECO ingest produced zero changes — nothing to process")

    return ecos


# ---------------------------------------------------------------------------
# Mode 1: pre-computed diff JSON
# ---------------------------------------------------------------------------

def _ingest_from_diff_file(path: str) -> list[ECO]:
    from eco.diff_mapper import load_diff_json, sw_diff_to_ecos
    diff = load_diff_json(path)
    ecos = sw_diff_to_ecos(diff)
    print(f"  Diff JSON ({path}): {len(ecos)} change(s)")
    return ecos


# ---------------------------------------------------------------------------
# Mode 2: before/after model paths → call SW API, then fall back
# ---------------------------------------------------------------------------

def _ingest_from_model_diff(before_path: str, after_path: str) -> list[ECO]:
    """
    Try the Composer bridge's /diff endpoint first.  If the bridge is not
    running or returns an error, fall back to the local folder-scan synthesizer
    (which is less accurate but works without SolidWorks).
    """
    from eco.diff_mapper import sw_diff_to_ecos

    # ── Try SW API bridge ────────────────────────────────────────────────────
    try:
        from composer.client import ComposerClient
        with ComposerClient() as client:
            if client.health():
                diff = client.diff(before_path, after_path)
                ecos = sw_diff_to_ecos(diff)
                source = diff.get("_source", "sw_api")
                print(f"  SW API diff ({source}): {len(ecos)} change(s) detected")
                return ecos
            else:
                print("  Composer bridge not running — falling back to local synthesizer")
    except Exception as exc:
        print(f"  SW API diff failed ({exc}) — falling back to local synthesizer")

    # ── Fallback: local folder-scan synthesizer ──────────────────────────────
    # NOTE: This is less accurate; it treats every file in the assembly folder
    # as a potential change rather than inspecting the actual component tree.
    from eco.synthesizer import synthesize_ecos
    ecos = synthesize_ecos(before_path, after_path)
    print(f"  Local synthesizer (fallback): {len(ecos)} change(s)")
    return ecos


# ---------------------------------------------------------------------------
# Mode 3: hand-authored ECO JSON
# ---------------------------------------------------------------------------

def _load_ecos_from_file(path: str) -> list[ECO]:
    from eco.synthesizer import load_ecos_from_file
    return load_ecos_from_file(path)
