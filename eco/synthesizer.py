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


_SW_DOC_TYPE = {".SLDPRT": 1, ".SLDASM": 2, ".SLDDRW": 3}  # swDocPART / swDocASSEMBLY / swDocDRAWING


def _com_diff(before_path: str, after_path: str) -> list[ECO]:
    """Shallow diff via SolidWorks COM API — Windows only.

    Reads the top-level document's custom properties from each file.  This
    is the local-synthesizer fallback for when the bridge is unreachable;
    it does NOT walk the component tree like the bridge's /diff endpoint
    does, so for assemblies it will only surface assembly-level property
    changes (not per-component geometry diffs).
    """
    import win32com.client            # type: ignore
    import pythoncom                   # type: ignore
    from win32com.client import VARIANT  # type: ignore

    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = False

    # OpenDoc6 requires an absolute path AND the correct document type for
    # the file extension — passing swDocPART for a .SLDASM raises a COM
    # "Type mismatch" before the doc is even loaded.
    before_abs = str(Path(before_path).resolve())
    after_abs  = str(Path(after_path).resolve())
    before_type = _SW_DOC_TYPE.get(Path(before_abs).suffix.upper(), 1)
    after_type  = _SW_DOC_TYPE.get(Path(after_abs).suffix.upper(),  1)

    # Late-bound OpenDoc6 needs VT_BYREF | VT_I4 for the Errors/Warnings
    # out-params — passing bare integers raises "Type mismatch" on param 5.
    def _open(path: str, dtype: int):
        errors   = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        warnings = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
        return sw.OpenDoc6(path, dtype, 1, "", errors, warnings)

    before_doc = _open(before_abs, before_type)
    after_doc  = _open(after_abs,  after_type)

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


def _part_fingerprint(file_path: Path) -> str:
    """Return a stable identity for a part file.

    A pure rename (filename changes, geometry doesn't) must collapse to a
    single fingerprint so the set diff sees "no change." Preference order:
      1. OLE Subject field — convention many CAD shops use for canonical
         "Part Number" custom property
      2. SHA-1 of file bytes — collapses true renames even when the OLE
         property is empty
      3. Filename — final fallback if the file can't be opened
    """
    try:
        import olefile  # type: ignore
        if olefile.isOleFile(str(file_path)):
            with olefile.OleFileIO(str(file_path)) as ole:
                if ole.exists("\x05SummaryInformation"):
                    si = ole.get_metadata()
                    if si.subject:
                        pn = si.subject.decode("utf-8", errors="ignore").strip()
                        if pn:
                            return f"pn:{pn}"
    except Exception:
        pass

    try:
        h = hashlib.sha1()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return f"sha1:{h.hexdigest()}"
    except Exception:
        return f"name:{file_path.name.upper()}"


def _folder_part_map(sldasm_path: str) -> dict[str, str]:
    """Map fingerprint -> filename for SLD* files alongside the assembly.

    If two files share a fingerprint (true duplicates) the first one
    encountered wins. Filename is preserved so add/remove ECOs still
    report a human-readable name.
    """
    exts = {".SLDPRT", ".SLDASM"}
    out: dict[str, str] = {}
    for f in Path(sldasm_path).parent.iterdir():
        if f.suffix.upper() not in exts:
            continue
        out.setdefault(_part_fingerprint(f), f.name)
    return out


def _part_ref_diff(before_path: str, after_path: str) -> list[ECO]:
    before_map = _folder_part_map(before_path)
    after_map = _folder_part_map(after_path)

    before_fps = set(before_map)
    after_fps = set(after_map)

    added_fps   = after_fps - before_fps
    removed_fps = before_fps - after_fps

    ecos: list[ECO] = []
    for fp in sorted(added_fps):
        name = after_map[fp]
        eco_id = _make_eco_id(before_path, name)
        ecos.append(ECO(
            eco_id=eco_id,
            part_number=Path(name).stem,
            changes=[ECOChange(field="part", old="", new=name)],
            summary=f"Part added to assembly: {name}",
        ))
    for fp in sorted(removed_fps):
        name = before_map[fp]
        eco_id = _make_eco_id(before_path, name)
        ecos.append(ECO(
            eco_id=eco_id,
            part_number=Path(name).stem,
            changes=[ECOChange(field="part", old=name, new="")],
            summary=f"Part removed from assembly: {name}",
        ))

    # Renames (same fingerprint, different filename) are not ECOs — geometry
    # didn't change. Log them so the user can see why a filename diff was
    # suppressed instead of silently dropping evidence.
    for fp in sorted(before_fps & after_fps):
        before_name = before_map[fp]
        after_name  = after_map[fp]
        if before_name != after_name:
            print(f"  [synthesizer] rename suppressed (same fingerprint): {before_name} -> {after_name}")

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
