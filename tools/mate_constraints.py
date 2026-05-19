"""
Tool: query_mate_constraints

Returns the mate constraints for a part — what it connects to and how.
In production this calls the Composer FastAPI bridge to read the mate
graph from the .smg file. For testing, reads from MATE_GRAPH_DB env var.
"""

import json
import os

import httpx


_TOOL_SCHEMA = {
    "name": "query_mate_constraints",
    "description": (
        "Query the mate constraints for a part number. Returns what other parts "
        "it is mated to, the mate type (concentric, coincident, etc.), and any "
        "relevant dimensions or tolerances on the constraint."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "part_number": {
                "type": "string",
                "description": "The part number to query mate constraints for.",
            }
        },
        "required": ["part_number"],
    },
}


def schema() -> dict:
    return _TOOL_SCHEMA


def execute(part_number: str) -> dict:
    # Try local test DB first
    db_path = os.environ.get("MATE_GRAPH_DB", "")
    if db_path and os.path.exists(db_path):
        with open(db_path) as f:
            db = json.load(f)
        return db.get(
            part_number,
            {"part_number": part_number, "mates": [], "note": "No mate data found."},
        )

    # Try Composer FastAPI bridge
    api_base = os.environ.get("SOLIDWORKS_API", "http://localhost:8000")
    try:
        resp = httpx.get(
            f"{api_base}/mates/{part_number}",
            timeout=5.0,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {
            "part_number": part_number,
            "mates": [],
            "note": "Composer bridge unavailable — no mate data.",
        }
