"""
Tool: lookup_prior_step

Returns metadata (heading, section, parts) for a step the model has
already generated. Used by the text generator when it needs to emit an
accurate `[[see: <step_id>]]` cross-reference.

Backed by a running manifest passed to bind_manifest().
"""

from __future__ import annotations


_TOOL_SCHEMA = {
    "name": "lookup_prior_step",
    "description": (
        "Look up an earlier generated step by its step_id. Returns the step's "
        "section, heading, and parts. Use this when you need to reference a "
        "prior step in body_text via [[see: <step_id>]]."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "step_id": {
                "type": "string",
                "description": "The step_id to look up.",
            }
        },
        "required": ["step_id"],
    },
}


_manifest: dict[str, dict] = {}


def schema() -> dict:
    return _TOOL_SCHEMA


def bind_manifest(manifest: dict[str, dict]) -> None:
    """Install the live manifest. The text generator appends each new step's
    entry to this dict, so lookups during the next iteration see prior work.
    """
    global _manifest
    _manifest = manifest


def execute(step_id: str) -> dict:
    entry = _manifest.get(step_id)
    if entry is None:
        return {"step_id": step_id, "note": "Step not yet generated or step_id not recognized."}
    return entry
