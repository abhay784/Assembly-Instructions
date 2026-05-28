from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


class ECOChange(BaseModel):
    field: str  # e.g. "length_mm", "torque_nm", "mate_type", "material"
    old: str
    new: str

    # ── ECO-pipeline extensions (optional; pre-existing callers ignore) ──
    change_type: str | None = None
    severity: Literal["CRITICAL", "MAJOR", "MINOR", "UNCERTAIN"] = "MINOR"
    confidence: float = 1.0
    rationale: str | None = None
    location: "ECOLocation | None" = None

    # Engineer-facing aliases — same data as old/new under ECO vocabulary.
    @property
    def was(self) -> str:
        return self.old

    @property
    def is_value(self) -> str:
        return self.new


class ECO(BaseModel):
    eco_id: str
    part_number: str
    changes: list[ECOChange]
    summary: str


class ECOLocation(BaseModel):
    """Where to find a change in the physical assembly.

    Populated differently per scenario:
      • A (drawings exist): `zone` from MVP1Compare's ASME grid.
      • B / C (no drawings): `tree_path` + `mate_neighbors` from the
        SolidWorks assembly walk.
    """
    zone: str | None = None
    tree_path: str | None = None
    mate_neighbors: list[str] = Field(default_factory=list)


class ECOReport(BaseModel):
    eco_id: str
    document_id: str
    scenario: Literal["A", "B", "C"]
    source: Literal["drawing_diff", "cad_diff"]
    summary: str
    changes: list[ECOChange]
    attachments: list[Path] = Field(default_factory=list)


# Resolve the forward reference now that ECOLocation is defined.
ECOChange.model_rebuild()
