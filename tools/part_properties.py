"""
Tool: lookup_part_properties

Looks up a component's BoM entry — mass, dimensions, custom properties —
so the text generator doesn't have to receive the entire BoM in every
prompt. Backed by an in-process BoM passed to bind_bom().
"""

from __future__ import annotations

from schemas.cad import BoM, BoMComponent


_TOOL_SCHEMA = {
    "name": "lookup_part_properties",
    "description": (
        "Look up the BoM entry for a part by name. Returns mass, custom "
        "properties (material, finish, ...), and key dimensions. Use this "
        "when you need exact numeric data for a part referenced in the step plan."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "part_name": {
                "type": "string",
                "description": "The component name exactly as it appears in the step plan (e.g. 'BRACKET_R2.SLDPRT').",
            }
        },
        "required": ["part_name"],
    },
}


_bound_bom: BoM | None = None


def schema() -> dict:
    return _TOOL_SCHEMA


def bind_bom(bom: BoM) -> None:
    """Install the BoM the tool will read from. Called once per pipeline run."""
    global _bound_bom
    _bound_bom = bom


def execute(part_name: str) -> dict:
    if _bound_bom is None:
        return {"part_name": part_name, "note": "BoM not bound — tool unavailable."}
    name_upper = part_name.strip().upper()
    for comp in _bound_bom.components:
        if comp.name.upper() == name_upper:
            return _serialize(comp)
    # Try suffix match — model may pass a bare name without extension
    for comp in _bound_bom.components:
        if comp.name.upper().startswith(name_upper) or name_upper in comp.name.upper():
            return _serialize(comp)
    return {"part_name": part_name, "note": "Not found in BoM."}


def _serialize(comp: BoMComponent) -> dict:
    return {
        "part_name": comp.name,
        "quantity": comp.quantity,
        "mass_kg": comp.mass_kg,
        "properties": comp.properties,
        "dimensions_mm": comp.dimensions,
    }
