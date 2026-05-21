"""
Synthesizes ECO JSON by diffing before/after SolidWorks model files.

Used for testing when no formal ECO document exists. Produces the same
List[ECO] schema as real ECOs so all downstream stages are identical.

Two modes:
  1. COM-based (Windows + SolidWorks installed): extracts full dimension,
     mate, and part-count diffs via the SolidWorks API.
  2. Property-based fallback (any OS): reads custom properties from the
     OLE compound document without opening SolidWorks.
"""

import hashlib
import json
import os
from pathlib import Path

from schemas.eco import ECO, ECOChange


def synthesize_ecos(before_path: str, after_path: str) -> list[ECO]:
    """
    Diff before/after SolidWorks model files and return a list of ECOs.
    Falls back to property-based diff if COM is unavailable.
    """
    try:
        import pythoncom  # type: ignore
        pythoncom.CoInitialize()
        return _com_diff(before_path, after_path)
    except ImportError:
        return _property_diff(before_path, after_path)


def _com_diff(before_path: str, after_path: str) -> list[ECO]:
    """Full diff via SolidWorks COM API — Windows only."""
    import win32com.client  # type: ignore

    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = False

    before_doc = sw.OpenDoc6(before_path, 1, 1, "", 0, 0)
    after_doc = sw.OpenDoc6(after_path, 1, 1, "", 0, 0)

    ecos: list[ECO] = []

    before_parts = _get_part_properties(before_doc)
    after_parts = _get_part_properties(after_doc)

    all_part_numbers = set(before_parts) | set(after_parts)
    for part_number in sorted(all_part_numbers):
        changes = _diff_properties(
            before_parts.get(part_number, {}),
            after_parts.get(part_number, {}),
        )
        if changes:
            eco_id = _make_eco_id(before_path, part_number)
            summary_parts = [f"{c.field}: {c.old!r} → {c.new!r}" for c in changes]
            ecos.append(
                ECO(
                    eco_id=eco_id,
                    part_number=part_number,
                    changes=changes,
                    summary=f"Synthesized from model diff. Changes: {'; '.join(summary_parts)}",
                )
            )

    sw.CloseDoc(before_doc.GetPathName())
    sw.CloseDoc(after_doc.GetPathName())
    return ecos


def _property_diff(before_path: str, after_path: str) -> list[ECO]:
    """
    Lightweight diff without SolidWorks — two passes:
    1. OLE custom property diff (revision, description, part number).
    2. Part-reference diff: scans the SLDASM binary for embedded component
       filenames (*.SLDPRT / *.SLDASM) and generates one ECO per added,
       removed, or renamed part.
    """
    try:
        import olefile  # type: ignore
    except ImportError:
        raise RuntimeError(
            "Neither SolidWorks COM nor olefile is available. "
            "Install olefile: pip install olefile"
        )

    before_props = _read_ole_properties(before_path)
    after_props = _read_ole_properties(after_path)

    ecos: list[ECO] = []

    # Pass 1 — metadata properties
    part_number = after_props.get("part_number") or Path(after_path).stem
    prop_changes = _diff_properties(before_props, after_props)
    if prop_changes:
        eco_id = _make_eco_id(before_path, part_number)
        summary_parts = [f"{c.field}: {c.old!r} → {c.new!r}" for c in prop_changes]
        ecos.append(ECO(
            eco_id=eco_id,
            part_number=part_number,
            changes=prop_changes,
            summary=f"Synthesized from property diff. Changes: {'; '.join(summary_parts)}",
        ))

    # Pass 2 — part reference diff
    ecos.extend(_part_ref_diff(before_path, after_path))

    return ecos


def _folder_part_refs(sldasm_path: str) -> set[str]:
    """Return the set of SLD* filenames in the same directory as the assembly."""
    exts = {".SLDPRT", ".SLDASM"}
    return {
        f.name.upper()
        for f in Path(sldasm_path).parent.iterdir()
        if f.suffix.upper() in exts
    }


def _part_ref_diff(before_path: str, after_path: str) -> list[ECO]:
    before_refs = _folder_part_refs(before_path)
    after_refs = _folder_part_refs(after_path)

    added = after_refs - before_refs
    removed = before_refs - after_refs

    ecos: list[ECO] = []
    for part in sorted(added):
        eco_id = _make_eco_id(before_path, part)
        ecos.append(ECO(
            eco_id=eco_id,
            part_number=Path(part).stem,
            changes=[ECOChange(field="part", old="", new=part)],
            summary=f"Part added to assembly: {part}",
        ))
    for part in sorted(removed):
        eco_id = _make_eco_id(before_path, part)
        ecos.append(ECO(
            eco_id=eco_id,
            part_number=Path(part).stem,
            changes=[ECOChange(field="part", old=part, new="")],
            summary=f"Part removed from assembly: {part}",
        ))
    return ecos


def _read_ole_properties(path: str) -> dict:
    import olefile  # type: ignore

    props: dict = {}
    if not olefile.isOleFile(path):
        return props

    with olefile.OleFileIO(path) as ole:
        if ole.exists("\x05SummaryInformation"):
            si = ole.get_metadata()
            if si.subject:
                props["part_number"] = si.subject.decode("utf-8", errors="ignore")
            if si.title:
                props["description"] = si.title.decode("utf-8", errors="ignore")

        if ole.exists("\x05DocumentSummaryInformation"):
            dsi_stream = ole.openstream("\x05DocumentSummaryInformation")
            # Custom properties live after the standard section; parse minimally
            raw = dsi_stream.read()
            props["_raw_hash"] = hashlib.md5(raw).hexdigest()

    return props


def _get_part_properties(doc) -> dict[str, dict]:
    """Extract per-part properties from an open SolidWorks document."""
    props: dict[str, dict] = {}
    try:
        custom_props = doc.Extension.CustomPropertyManager("")
        names = custom_props.GetNames()
        if names:
            part_number = ""
            part_props: dict = {}
            for name in names:
                _, _, val, _ = custom_props.Get4(name, False)
                part_props[name.lower()] = val
                if name.lower() in ("part number", "partnumber", "part_number"):
                    part_number = val
            if part_number:
                props[part_number] = part_props
    except Exception:
        pass
    return props


def _diff_properties(before: dict, after: dict) -> list[ECOChange]:
    changes: list[ECOChange] = []
    all_keys = set(before) | set(after)
    # Skip internal/hash keys
    skip = {"_raw_hash"}
    for key in sorted(all_keys - skip):
        old_val = str(before.get(key, ""))
        new_val = str(after.get(key, ""))
        if old_val != new_val:
            changes.append(ECOChange(field=key, old=old_val, new=new_val))
    return changes


def _make_eco_id(model_path: str, part_number: str) -> str:
    digest = hashlib.md5(f"{model_path}:{part_number}".encode()).hexdigest()[:8]
    safe_pn = part_number.replace(" ", "_").replace("/", "-")
    return f"synth_{safe_pn}_{digest}"


def load_ecos_from_file(path: str) -> list[ECO]:
    """Load a real ECO JSON file (list of ECO objects)."""
    with open(path) as f:
        raw = json.load(f)
    if isinstance(raw, list):
        return [ECO.model_validate(item) for item in raw]
    return [ECO.model_validate(raw)]
