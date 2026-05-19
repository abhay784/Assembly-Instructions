from pydantic import BaseModel


class ECOChange(BaseModel):
    field: str  # e.g. "length_mm", "torque_nm", "mate_type", "material"
    old: str
    new: str


class ECO(BaseModel):
    eco_id: str
    part_number: str
    changes: list[ECOChange]
    summary: str
