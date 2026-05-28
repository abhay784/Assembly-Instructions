"""Normalize MVP1Compare changeset.json AND our CAD-diff ECO list into ECOReport.

The two upstream shapes are very different — MVP1 gives per-view Change
rows with zones, our CAD diff gives ECO objects per component. Both end
up as a flat list of ECOChange rows on an ECOReport.
"""

from __future__ import annotations

import json
from pathlib import Path

from schemas.eco import ECO, ECOChange, ECOLocation, ECOReport


_MVP1_SEVERITY_MAP = {
    "CRITICAL": "CRITICAL",
    "MAJOR": "MAJOR",
    "MINOR": "MINOR",
    "UNCERTAIN": "UNCERTAIN",
    "UNCERTAIN*": "UNCERTAIN",
}


def from_mvp1_changeset(
    changeset_path: str | Path,
    document_id: str,
    eco_id: str,
) -> ECOReport:
    """Read MVP1Compare's canonical changeset.json and adapt to ECOReport."""
    data = json.loads(Path(changeset_path).read_text())

    changes: list[ECOChange] = []
    view_diffs = data.get("view_diffs") or data.get("views") or []
    for view in view_diffs:
        for ch in view.get("changes", []):
            severity = _MVP1_SEVERITY_MAP.get(ch.get("severity", "MINOR"), "MINOR")
            zone = ch.get("zone")
            location = ECOLocation(zone=str(zone)) if zone else None
            changes.append(ECOChange(
                field=str(ch.get("field", "unknown")),
                old=_str_or_empty(ch.get("orig_value")),
                new=_str_or_empty(ch.get("revised_value")),
                change_type=str(ch.get("field", "")) or None,
                severity=severity,  # type: ignore[arg-type]
                confidence=float(ch.get("confidence", 1.0)),
                rationale=ch.get("rationale"),
                location=location,
            ))

    summary = _mvp1_summary(data, len(changes))

    return ECOReport(
        eco_id=eco_id,
        document_id=document_id,
        scenario="A",
        source="drawing_diff",
        summary=summary,
        changes=changes,
    )


def from_ecos(
    ecos: list[ECO],
    document_id: str,
    eco_id: str,
    scenario: str,
    locations: dict[str, ECOLocation] | None = None,
    attachments: list[Path] | None = None,
) -> ECOReport:
    """Flatten a CAD-diff ECO list (one ECO per component) into a single ECOReport."""
    flat: list[ECOChange] = []
    locations = locations or {}

    for eco in ecos:
        loc = locations.get(eco.part_number.upper())
        # Also try the original component file name (synthesizer uses stem,
        # tree_locator keys by filename basename like "BRACKET.SLDPRT").
        if loc is None:
            for key in locations:
                if Path(key).stem.upper() == eco.part_number.upper():
                    loc = locations[key]
                    break
        for c in eco.changes:
            if c.location is None and loc is not None:
                c.location = loc
            flat.append(c)

    severities = [c.severity for c in flat]
    summary = (
        f"{len(flat)} change(s) across {len(ecos)} component(s): "
        f"{severities.count('CRITICAL')} critical, "
        f"{severities.count('MAJOR')} major, "
        f"{severities.count('MINOR')} minor"
    )

    return ECOReport(
        eco_id=eco_id,
        document_id=document_id,
        scenario=scenario,  # type: ignore[arg-type]
        source="cad_diff",
        summary=summary,
        changes=flat,
        attachments=attachments or [],
    )


def _mvp1_summary(data: dict, total: int) -> str:
    summary = data.get("summary") or {}
    critical = summary.get("critical", 0)
    significant = summary.get("significant", 0)
    minor = summary.get("minor", 0)
    uncertain = summary.get("uncertain", 0)
    return (
        f"{total} change(s) from drawing diff: "
        f"{critical} critical, {significant} significant, "
        f"{minor} minor, {uncertain} uncertain"
    )


def _str_or_empty(v) -> str:
    return "" if v is None else str(v)
