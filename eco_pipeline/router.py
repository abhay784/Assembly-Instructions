"""Decide which ECO scenario to run from the user's inputs.

Scenario A — both drawings provided.
Scenario B — exactly one drawing provided.
Scenario C — no drawings provided.

The router also downgrades when an upstream stage fails (e.g. SLDDRW→PDF
conversion crashes for one of the two drawings); callers re-invoke
`decide` with the surviving drawing paths.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class RouteDecision:
    scenario: Literal["A", "B", "C"]
    before_drawing: Path | None
    after_drawing: Path | None
    rationale: str


def decide(
    before_drawing: str | Path | None,
    after_drawing: str | Path | None,
) -> RouteDecision:
    b = Path(before_drawing) if before_drawing else None
    a = Path(after_drawing) if after_drawing else None

    if b and a:
        return RouteDecision("A", b, a, "both drawings present")
    if b or a:
        return RouteDecision("B", b, a, "one drawing present — fallback to CAD diff + attach drawing")
    return RouteDecision("C", None, None, "no drawings — CAD diff only")
