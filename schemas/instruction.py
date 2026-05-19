from typing import Literal
from pydantic import BaseModel


class PartRef(BaseModel):
    part_id: str
    qty: int | None = None
    role: str | None = None
    source: Literal["explicit", "inferred"]


class Spec(BaseModel):
    key: str          # e.g. "torque_nm", "length_mm", "gap_mm"
    value: str
    unit: str | None = None
    source_span: str  # verbatim text the value was extracted from


class Callout(BaseModel):
    type: Literal["attention", "warning", "note"]
    text: str


class StepImage(BaseModel):
    image_id: str     # == Composer view_id, 1:1 with step_id
    kind: Literal["renderable_cad", "reference_photo"]
    caption: str | None = None
    camera_hint: str | None = None
    visible_parts: list[str] = []


class Step(BaseModel):
    step_id: str      # e.g. "upper_leg_step_03"
    section: str      # e.g. "Upper Leg Assembly"
    step_number: int
    heading: str
    body_text: str
    parts_referenced: list[PartRef] = []
    specs: list[Spec] = []
    callouts: list[Callout] = []
    images: list[StepImage] = []
    tools_operations: list[str] = []
