"""
Convert a SolidWorks API diff JSON (from POST /diff) into a list[ECO].

The diff JSON shape:
  {
    "before_path": str,
    "after_path":  str,
    "components_added":   [{"name": str, "quantity": int}],
    "components_removed": [{"name": str, "quantity": int}],
    "components_changed": [{
        "name": str,
        "quantity_before": int,
        "quantity_after":  int,
        "property_changes": [{"property": str, "before": str, "after": str}]
    }]
  }

Each component that actually changed becomes one ECO.  Parts that are in
both assemblies unchanged produce no ECO — that's the key improvement over
the folder-scan synthesizer which generated one ECO per file in the folder.
"""

import hashlib
import json
from pathlib import Path

from schemas.eco import ECO, ECOChange


def sw_diff_to_ecos(diff: dict) -> list[ECO]:
    """
    Convert a SolidWorks /diff API response to a list[ECO].

    Only parts that genuinely changed (added, removed, or property-modified)
    produce ECOs.  Unchanged parts are silently skipped.
    """
    before_path = diff.get("before_path", "")
    ecos: list[ECO] = []

    # ── Added components ──────────────────────────────────────────────────────
    for comp in diff.get("components_added", []):
        name = comp["name"]
        qty  = comp.get("quantity", 1)
        part_number = Path(name).stem

        changes: list[ECOChange] = [ECOChange(field="part", old="", new=name)]
        if qty != 1:
            changes.append(ECOChange(field="quantity", old="0", new=str(qty)))

        suffix = f" (qty: {qty})" if qty != 1 else ""
        ecos.append(ECO(
            eco_id=_make_eco_id(before_path, name),
            part_number=part_number,
            changes=changes,
            summary=f"Part added to assembly: {name}{suffix}",
        ))

    # ── Removed components ────────────────────────────────────────────────────
    for comp in diff.get("components_removed", []):
        name = comp["name"]
        qty  = comp.get("quantity", 1)
        part_number = Path(name).stem

        changes = [ECOChange(field="part", old=name, new="")]
        if qty != 1:
            changes.append(ECOChange(field="quantity", old=str(qty), new="0"))

        ecos.append(ECO(
            eco_id=_make_eco_id(before_path, name),
            part_number=part_number,
            changes=changes,
            summary=f"Part removed from assembly: {name}",
        ))

    # ── Changed components (same part, different properties/quantity) ──────────
    for comp in diff.get("components_changed", []):
        name = comp["name"]
        part_number = Path(name).stem

        changes = [
            ECOChange(
                field=pc["property"],
                old=str(pc.get("before", "")),
                new=str(pc.get("after", "")),
            )
            for pc in comp.get("property_changes", [])
        ]
        if not changes:
            continue  # nothing actually changed — skip

        summary_parts = [f"{c.field}: {c.old!r} → {c.new!r}" for c in changes]
        ecos.append(ECO(
            eco_id=_make_eco_id(before_path, name),
            part_number=part_number,
            changes=changes,
            summary=f"Part modified: {name}. Changes: {'; '.join(summary_parts)}",
        ))

    return ecos


def load_diff_json(path: str) -> dict:
    """Load a pre-computed diff JSON file from disk."""
    with open(path) as f:
        return json.load(f)


def _make_eco_id(model_path: str, part_name: str) -> str:
    digest = hashlib.md5(f"{model_path}:{part_name}".encode()).hexdigest()[:8]
    safe = part_name.replace(" ", "_").replace("/", "-")
    return f"diff_{safe}_{digest}"
