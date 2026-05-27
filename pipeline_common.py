"""
Shared plumbing for the revision (`pipeline.py`) and generation
(`pipeline_generate.py`) pipelines.

Stage checkpointing, the `--publish` flow, and the serialization
registries live here so both entry points share one source of truth.

The serialization helpers are intentionally tolerant: schemas evolve and
older checkpoints should still load when possible.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_STATE_DIR = ".pipeline_state"


def stage_path(run_id: str, stage_name: str) -> Path:
    return Path(_STATE_DIR) / run_id / f"{stage_name}.json"


def run_stage(run_id: str, stage_name: str, fn):
    """Checkpoint a stage's result. Loads from disk on rerun."""
    path = stage_path(run_id, stage_name)
    if path.exists():
        print(f"  Stage {stage_name}: loaded from checkpoint")
        data = json.loads(path.read_text())
        return _deserialize_stage(stage_name, data)

    result = fn()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(_serialize_stage(result), f)
    return result


def load_stage(run_id: str, stage_name: str):
    path = stage_path(run_id, stage_name)
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return _deserialize_stage(stage_name, data)


def _serialize_stage(data):
    if isinstance(data, list):
        if data and hasattr(data[0], "model_dump"):
            return [item.model_dump() for item in data]
        return data
    if isinstance(data, tuple):
        html, diff = data
        return {"html": html, "diff": diff}
    if hasattr(data, "model_dump"):
        return data.model_dump()
    return data


def _deserialize_stage(stage_name: str, data):
    # Lazy imports — keeps top-level cheap and avoids circular dependencies.
    from schemas.eco import ECO
    from schemas.instruction import Step
    from schemas.pipeline_state import (
        ActionPlan,
        AffectedStep,
        EvaluatedStep,
        RenderedImage,
        RevisedStep,
    )
    from schemas.cad import BoM
    from schemas.generation import AssemblyGraph, StepPlan

    list_schema_map = {
        # Revision pipeline
        "0_eco_ingest": ECO,
        "1_instruction_parser": Step,
        "2_change_mapper": AffectedStep,
        "3_agent_planner": ActionPlan,
        "4_text_revision": RevisedStep,
        "5_image_renderer": RenderedImage,
        "6_eval_gate": EvaluatedStep,
        # Generation pipeline
        "G1_assembly_sequencer": None,   # handled below (single object)
        "G2_step_planner": StepPlan,
        "G3_text_generator": Step,
        "G4_image_renderer": RenderedImage,
        "G5_eval_gate": EvaluatedStep,
    }

    single_object_map = {
        "G0_cad_extract": BoM,
        "G1_assembly_sequencer": AssemblyGraph,
    }

    if stage_name in single_object_map:
        return single_object_map[stage_name].model_validate(data)

    if stage_name in list_schema_map and list_schema_map[stage_name] is not None:
        model = list_schema_map[stage_name]
        return [model.model_validate(item) for item in data]

    if stage_name in ("7_doc_stitcher", "G6_doc_stitcher"):
        return data["html"], data["diff"]

    return data
