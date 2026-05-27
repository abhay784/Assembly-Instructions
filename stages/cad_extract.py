"""
Stage G0 — CAD Extract (Phase 4: generation pipeline)

Walks a SolidWorks assembly into a structured BoM + mate graph that drives
the rest of the generation pipeline.

Two input modes, mirroring `eco_ingest`:

  1. --bom-json <bom.json>   Pre-extracted BoM JSON (works offline, e.g.
                             developing on Mac when the Windows VM that
                             hosts SolidWorks is down).
  2. --assembly <path.SLDASM> Calls the Composer bridge's POST
                             /extract_assembly endpoint. Requires the
                             Windows bridge to be running with SolidWorks.

There is no "synthesizer" fallback like ECO ingest has — generation needs
absolute assembly state, not a folder scan. Failure here is fatal.
"""

from __future__ import annotations

import json
from pathlib import Path

from schemas.cad import BoM


def run(
    bom_json_path: str | None = None,
    assembly_path: str | None = None,
) -> BoM:
    if bom_json_path:
        return _load_from_json(bom_json_path)
    if assembly_path:
        return _extract_from_bridge(assembly_path)
    raise ValueError("Provide --bom-json or --assembly")


def _load_from_json(path: str) -> BoM:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    bom = BoM.model_validate(data)
    if not bom.components:
        raise ValueError(f"BoM JSON at {path} has zero components — nothing to generate")
    print(f"  BoM JSON ({path}): {len(bom.components)} component(s), {len(bom.mates)} mate(s)")
    return bom


def _extract_from_bridge(assembly_path: str) -> BoM:
    from composer.client import ComposerClient

    with ComposerClient() as client:
        if not client.health():
            raise RuntimeError(
                "Composer bridge unreachable. Start it on the Windows host:\n"
                "  python -m uvicorn composer.server:app --host 127.0.0.1 --port 8000"
            )
        data = client.extract_assembly(assembly_path)

    bom = BoM.model_validate(data)
    if not bom.components:
        raise ValueError(f"Assembly {assembly_path} yielded zero components from the bridge")
    print(f"  Bridge /extract_assembly: {len(bom.components)} component(s), {len(bom.mates)} mate(s)")
    return bom
