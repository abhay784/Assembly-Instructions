"""
CAD-extraction schemas (Phase 4 — generation pipeline).

These mirror what the Composer bridge's /extract_assembly endpoint returns
when it walks a SolidWorks .SLDASM file. The shapes are intentionally close
to the bridge's existing internal dicts so the stage can `model_validate`
the JSON straight through with minimal massaging.
"""

from typing import Literal
from pydantic import BaseModel


class BoMComponent(BaseModel):
    name: str
    quantity: int = 1
    mass_kg: float | None = None
    properties: dict[str, str] = {}
    dimensions: dict[str, float] = {}      # feature/dim name → mm


class MateEdge(BaseModel):
    name: str
    type: str                              # "Concentric", "Coincident", ...
    parts: list[str]                       # exactly two; list (not tuple) for JSON round-trip


class BoM(BaseModel):
    assembly_path: str
    components: list[BoMComponent]
    mates: list[MateEdge]
