"""
Stage 0 — ECO Ingest / Synthesis

Accepts either:
  - A path to a real eco.json file, or
  - Paths to before/after SolidWorks model files (synthesizes ECOs from diff)

Returns a validated List[ECO] consumed by all downstream stages.
"""

from eco.synthesizer import load_ecos_from_file, synthesize_ecos
from schemas.eco import ECO


def run(
    eco_json_path: str | None = None,
    before_model_path: str | None = None,
    after_model_path: str | None = None,
) -> list[ECO]:
    if eco_json_path:
        ecos = load_ecos_from_file(eco_json_path)
    elif before_model_path and after_model_path:
        ecos = synthesize_ecos(before_model_path, after_model_path)
    else:
        raise ValueError(
            "Provide either eco_json_path or both before_model_path and after_model_path"
        )

    if not ecos:
        raise ValueError("ECO ingest produced zero ECOs — nothing to process")

    return ecos
