"""
Tool: lookup_part_history

Returns the change history and known specs for a given part number.
In production this queries the client's PLM/ERP system. For testing,
it reads from a local JSON file at PART_HISTORY_DB env var path.
"""

import json
import os


_TOOL_SCHEMA = {
    "name": "lookup_part_history",
    "description": (
        "Look up the engineering history and known specifications for a part number. "
        "Returns previous ECOs, current dimensions, material, and notes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "part_number": {
                "type": "string",
                "description": "The part number to look up.",
            }
        },
        "required": ["part_number"],
    },
}


def schema() -> dict:
    return _TOOL_SCHEMA


def execute(part_number: str) -> dict:
    db_path = os.environ.get("PART_HISTORY_DB", "")
    if db_path and os.path.exists(db_path):
        with open(db_path) as f:
            db = json.load(f)
        return db.get(part_number, {"part_number": part_number, "history": [], "note": "No history found."})

    return {
        "part_number": part_number,
        "history": [],
        "note": "PART_HISTORY_DB not configured — no history available for testing.",
    }
