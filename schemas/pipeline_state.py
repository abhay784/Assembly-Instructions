from typing import Any, Literal
from pydantic import BaseModel

from schemas.eco import ECO
from schemas.instruction import Step


class StageOutput(BaseModel):
    stage: str
    run_id: str
    data: Any
    completed_at: str


class AffectedStep(BaseModel):
    step_id: str
    eco_id: str
    change_types: list[str]   # e.g. ["dimension", "mate_constraint", "part_swap"]
    impact: Literal["direct", "indirect"]


class ActionPlan(BaseModel):
    step_id: str
    eco_id: str
    action: Literal[
        "rewrite_text",
        "rewrite_text_and_rerender",
        "add_step_flagged",
        "no_change",
    ]
    needs_manual_view: bool = False
    rationale: str = ""


class EvalFlag(BaseModel):
    flag_type: Literal["spec_unverified", "assembly_logic_uncertain", "image_quality"]
    detail: str
    severity: Literal["warning", "info"]


class RevisedStep(BaseModel):
    step_id: str
    original_step: Step
    revised_body_text: str
    confidence: Literal["high", "medium", "low"]
    flags: list[str] = []
    revision_source: str   # eco_id
    needs_manual_view: bool = False
    is_new_step: bool = False


class RenderedImage(BaseModel):
    step_id: str
    view_id: str
    old_path: str | None
    new_path: str | None
    skipped: bool = False   # True when needs_manual_view


class EvaluatedStep(BaseModel):
    revised_step: RevisedStep
    rendered_image: RenderedImage | None
    eval_flags: list[EvalFlag] = []


class ReviewItem(BaseModel):
    step_id: str
    flags: list[EvalFlag]
    original_text: str
    revised_text: str
    confidence: Literal["high", "medium", "low"]
    approved: bool = False
    reviewer_notes: str = ""
