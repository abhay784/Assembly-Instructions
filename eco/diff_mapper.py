"""
Convert a SolidWorks API diff JSON (from POST /diff) into a list[ECO].

The diff JSON shape produced by the COM-mode server:
  {
    "_source": "sw_com_api",          # or "folder_scan"
    "before_path": str,
    "after_path":  str,

    "components_added": [{
        "name": str, "quantity": int,
        "mass_kg": float | null,
        "properties": {str: str}
    }],
    "components_removed": [{"name": str, "quantity": int}],
    "components_changed": [{
        "name": str,
        "quantity_before": int, "quantity_after": int,
        "property_changes": [{"property": str, "before": str, "after": str}],
        "geometry_changes": [{"property": str, "before": str, "after": str}]
    }],
    "mates_added":   [{"name": str, "type": str}],   # only present in sw_com_api
    "mates_removed": [{"name": str, "type": str}],
  }

Rules:
  • Each component that genuinely changed produces one ECO.
  • Both property_changes AND geometry_changes are captured as ECOChanges.
  • Mates added/removed are grouped into a single "assembly_mates" ECO so
    downstream stages can decide which steps reference those mated parts.
  • Unchanged components produce no ECO.
"""

import hashlib
import json
from pathlib import Path

from schemas.eco import ECO, ECOChange


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sw_diff_to_ecos(diff: dict) -> list[ECO]:
    """
    Convert a SolidWorks /diff API response to a list[ECO].

    Handles both sw_com_api (full geometric diff) and folder_scan (approximate)
    sources transparently.
    """
    ecos: list[ECO] = []
    before_path = diff.get("before_path", "")
    source      = diff.get("_source", "unknown")

    # ── Added components ──────────────────────────────────────────────────────
    for comp in diff.get("components_added", []):
        name    = comp["name"]
        qty     = comp.get("quantity", 1)
        part_number = Path(name).stem

        changes: list[ECOChange] = [ECOChange(field="part", old="", new=name)]

        if qty != 1:
            changes.append(ECOChange(field="quantity", old="0", new=str(qty)))

        # Include mass/properties from COM diff so downstream has context
        mass_kg = comp.get("mass_kg")
        if mass_kg is not None:
            changes.append(ECOChange(field="mass_kg", old="", new=str(mass_kg)))

        for prop, val in sorted((comp.get("properties") or {}).items()):
            if val:
                changes.append(ECOChange(field=prop, old="", new=str(val)))

        suffix = f" (qty: {qty})" if qty != 1 else ""
        ecos.append(ECO(
            eco_id=_make_eco_id(before_path, name),
            part_number=part_number,
            changes=changes,
            summary=f"[{source}] Part added: {name}{suffix}",
        ))

    # ── Removed components ────────────────────────────────────────────────────
    for comp in diff.get("components_removed", []):
        name    = comp["name"]
        qty     = comp.get("quantity", 1)
        part_number = Path(name).stem

        changes = [ECOChange(field="part", old=name, new="")]
        if qty != 1:
            changes.append(ECOChange(field="quantity", old=str(qty), new="0"))

        ecos.append(ECO(
            eco_id=_make_eco_id(before_path, name),
            part_number=part_number,
            changes=changes,
            summary=f"[{source}] Part removed: {name}",
        ))

    # ── Changed components — properties + geometry + dimensions + faces ────────
    for comp in diff.get("components_changed", []):
        name        = comp["name"]
        part_number = Path(name).stem
        changes: list[ECOChange] = []

        # Custom / metadata property changes
        for pc in comp.get("property_changes", []):
            changes.append(ECOChange(
                field=pc["property"],
                old=str(pc.get("before", "")),
                new=str(pc.get("after", "")),
            ))

        # Shallow geometry changes (mass, volume, surface_area)
        _geo_labels = {
            "mass_kg":         "mass (kg)",
            "volume_m3":       "volume (m³)",
            "surface_area_m2": "surface area (m²)",
        }
        for gc in comp.get("geometry_changes", []):
            prop = gc["property"]
            changes.append(ECOChange(
                field=_geo_labels.get(prop, prop),
                old=str(gc.get("before", "")),
                new=str(gc.get("after", "")),
            ))

        # Deep dimension changes  e.g. "D1@Sketch1@part.SLDPRT: 5.0 → 6.0 mm"
        for dc in comp.get("dimension_changes", []):
            dim_name = dc.get("dimension", "?")
            unit     = dc.get("unit", "")
            changes.append(ECOChange(
                field=f"dim:{dim_name}",
                old=f"{dc.get('before', '')} {unit}".strip(),
                new=f"{dc.get('after',  '')} {unit}".strip(),
            ))

        # Deep face/surface topology changes  e.g. cylinder radius 3.0 → 4.0 mm
        for fc in comp.get("face_changes", []):
            ftype  = fc.get("surface_type", "face")
            change = fc.get("change", "")
            if change == "radius":
                changes.append(ECOChange(
                    field=f"face:{ftype}:radius_mm",
                    old=str(fc.get("before_mm", "")),
                    new=str(fc.get("after_mm",  "")),
                ))
            elif change == "count":
                changes.append(ECOChange(
                    field=f"face:{ftype}:count",
                    old=str(fc.get("before", "")),
                    new=str(fc.get("after",  "")),
                ))
            elif change == "total_area_mm2":
                changes.append(ECOChange(
                    field=f"face:{ftype}:area_mm2",
                    old=str(fc.get("before", "")),
                    new=str(fc.get("after",  "")),
                ))
            else:
                changes.append(ECOChange(
                    field=f"face:{ftype}:{change}",
                    old=str(fc.get("before", fc.get("before_mm", ""))),
                    new=str(fc.get("after",  fc.get("after_mm",  ""))),
                ))

        if not changes:
            continue

        # Build a readable summary
        n_prop = len(comp.get("property_changes", []))
        n_geo  = len(comp.get("geometry_changes", []))
        n_dim  = len(comp.get("dimension_changes", []))
        n_face = len(comp.get("face_changes", []))
        detail = []
        if n_prop: detail.append(f"{n_prop} property")
        if n_geo:  detail.append(f"{n_geo} mass/volume")
        if n_dim:  detail.append(f"{n_dim} dimension")
        if n_face: detail.append(f"{n_face} face/surface")
        detail_str = ", ".join(detail) + " change(s)" if detail else "changes"

        prop_strs = [f"{c.field}: {c.old!r} → {c.new!r}" for c in changes[:6]]
        if len(changes) > 6:
            prop_strs.append(f"… +{len(changes)-6} more")

        ecos.append(ECO(
            eco_id=_make_eco_id(before_path, name),
            part_number=part_number,
            changes=changes,
            summary=(
                f"[{source}] Part modified: {name} ({detail_str}). "
                f"{'; '.join(prop_strs)}"
            ),
        ))

    # ── Mate changes — one consolidated ECO for all added/removed mates ───────
    mates_added   = diff.get("mates_added",   [])
    mates_removed = diff.get("mates_removed", [])

    if mates_added or mates_removed:
        mate_changes: list[ECOChange] = []
        for m in mates_added:
            mate_changes.append(ECOChange(
                field=f"mate:{m['type']}",
                old="",
                new=m["name"],
            ))
        for m in mates_removed:
            mate_changes.append(ECOChange(
                field=f"mate:{m['type']}",
                old=m["name"],
                new="",
            ))

        added_names   = [m["name"] for m in mates_added[:3]]
        removed_names = [m["name"] for m in mates_removed[:3]]
        summary_parts = []
        if added_names:
            summary_parts.append(f"added: {', '.join(added_names)}"
                                  + (f" +{len(mates_added)-3} more"
                                     if len(mates_added) > 3 else ""))
        if removed_names:
            summary_parts.append(f"removed: {', '.join(removed_names)}"
                                  + (f" +{len(mates_removed)-3} more"
                                     if len(mates_removed) > 3 else ""))

        ecos.append(ECO(
            eco_id=_make_eco_id(before_path, "assembly_mates"),
            part_number="assembly_mates",
            changes=mate_changes,
            summary=(
                f"[{source}] Mate constraints changed "
                f"({len(mates_added)} added, {len(mates_removed)} removed). "
                + "; ".join(summary_parts)
            ),
        ))

    return ecos


def load_diff_json(path: str) -> dict:
    """Load a pre-computed diff JSON file from disk."""
    with open(path) as f:
        return json.load(f)


def _make_eco_id(model_path: str, part_name: str) -> str:
    digest = hashlib.md5(f"{model_path}:{part_name}".encode()).hexdigest()[:8]
    safe   = part_name.replace(" ", "_").replace("/", "-")
    return f"diff_{safe}_{digest}"
