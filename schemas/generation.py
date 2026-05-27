"""
Generation-pipeline schemas (Phase 4).

These types live between CAD extract and text generation. The final
output of generation is still `schemas.instruction.Step` — these are
intermediate.
"""

from typing import Literal
from pydantic import BaseModel


Role = Literal["structural", "subassembly", "dynamic", "fastener"]


class AssemblyNode(BaseModel):
    component_name: str
    build_index: int                       # 0-based, global build order
    layer: int                             # BFS depth from root
    role: Role
    mates_used: list[str] = []             # mate names connecting to earlier nodes
    parent_components: list[str] = []      # already-built parts this attaches to


class AssemblyGraph(BaseModel):
    root: str
    nodes: list[AssemblyNode]


class StepPlan(BaseModel):
    step_id: str                           # "<section_slug>_step_<NN>"
    section: str
    step_number: int
    components: list[str]                  # components installed in this step
    parent_components: list[str] = []
    mates_used: list[str] = []
    role_hint: Role
