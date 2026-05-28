"""LLM pass that labels each ECOChange with a semantic change_type and rationale.

Input: list[ECO] from the CAD-diff path (raw field/old/new tuples like
"face:cylinder:radius_mm: 2.5 → 3.175"). Output: same ECOs, with each
ECOChange's change_type / severity / confidence / rationale fields
populated. On LLM truncation or JSON parse failure, the original ECOs
are returned with a low-confidence MINOR label so the pipeline never
blocks on the labeler.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Iterable

from schemas.eco import ECO, ECOChange

if TYPE_CHECKING:
    from llm.client import LLMClient


_SYSTEM_PROMPT = """You are a mechanical-engineering ECO assistant. You receive a list of
raw geometric/property changes between two SolidWorks assembly revisions
and label each one with a concise semantic category, a severity, and a
one-sentence engineer-facing rationale.

Output STRICT JSON only — no prose, no markdown. Schema:

{
  "labels": [
    {
      "index": 0,
      "change_type": "string — e.g. hole_added, hole_removed, bore_enlarged,
                     bore_reduced, thread_changed, dimension_changed,
                     mass_changed, component_added, component_removed,
                     component_replaced, material_changed, quantity_changed,
                     mate_added, mate_removed, property_changed",
      "severity": "CRITICAL | MAJOR | MINOR",
      "rationale": "single sentence, ≤20 words, present tense"
    }
  ]
}

Severity guidance:
- CRITICAL: load-bearing geometry, threaded fastener size, material, part
  added or removed, mate added or removed.
- MAJOR: bore/diameter changes ≥10%, mass changes ≥5%, revision bumps.
- MINOR: cosmetic property changes, sub-percent mass drift, descriptive
  custom-property edits.

The "index" must match the index in the input list. Return exactly one
label per input change, in the original order."""


_MAX_TOKENS = 4000


def run(ecos: list[ECO], llm: "LLMClient") -> list[ECO]:
    if not ecos:
        return ecos

    for eco in ecos:
        if not eco.changes:
            continue
        labels = _label_changes(eco.part_number, eco.changes, llm)
        if labels is None:
            _apply_fallback(eco.changes)
            continue
        _apply_labels(eco.changes, labels)

    return ecos


def _label_changes(
    part_number: str,
    changes: list[ECOChange],
    llm: "LLMClient",
) -> list[dict] | None:
    user_payload = {
        "part_number": part_number,
        "changes": [
            {"index": i, "field": c.field, "was": c.old, "is": c.new}
            for i, c in enumerate(changes)
        ],
    }

    response = llm.complete(
        messages=[{"role": "user", "content": json.dumps(user_payload)}],
        system=_SYSTEM_PROMPT,
        max_tokens=_MAX_TOKENS,
    )

    if response.truncated:
        print(f"  semantic_labeler: response truncated for {part_number} — using fallback")
        return None

    raw = response.content.strip()
    # Strip any accidental code fences.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()

    try:
        parsed = json.loads(raw)
        labels = parsed.get("labels")
        if not isinstance(labels, list):
            return None
        return labels
    except json.JSONDecodeError as exc:
        print(f"  semantic_labeler: bad JSON from LLM for {part_number} ({exc}) — using fallback")
        return None


def _apply_labels(changes: list[ECOChange], labels: Iterable[dict]) -> None:
    by_index = {int(lbl.get("index", -1)): lbl for lbl in labels if "index" in lbl}
    for i, c in enumerate(changes):
        lbl = by_index.get(i)
        if not lbl:
            _apply_fallback_one(c)
            continue
        change_type = lbl.get("change_type")
        severity = lbl.get("severity", "MINOR")
        rationale = lbl.get("rationale")
        c.change_type = str(change_type) if change_type else None
        if severity in ("CRITICAL", "MAJOR", "MINOR", "UNCERTAIN"):
            c.severity = severity  # type: ignore[assignment]
        else:
            c.severity = "MINOR"
        c.rationale = str(rationale) if rationale else None
        c.confidence = 0.9


def _apply_fallback(changes: list[ECOChange]) -> None:
    for c in changes:
        _apply_fallback_one(c)


def _apply_fallback_one(c: ECOChange) -> None:
    if c.change_type is None:
        c.change_type = _heuristic_change_type(c.field)
    c.severity = c.severity or "MINOR"
    c.confidence = min(c.confidence, 0.4)
    c.rationale = c.rationale or f"Raw diff field {c.field!r} (unlabeled)."


def _heuristic_change_type(field: str) -> str:
    f = field.lower()
    if f == "part":
        return "component_added_or_removed"
    if f.startswith("mate:"):
        return "mate_changed"
    if f.startswith("dim:"):
        return "dimension_changed"
    if f.startswith("face:") and "radius" in f:
        return "bore_changed"
    if f.startswith("face:") and "count" in f:
        return "feature_count_changed"
    if "mass" in f:
        return "mass_changed"
    if f == "quantity":
        return "quantity_changed"
    return "property_changed"
