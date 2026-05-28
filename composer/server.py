"""
SolidWorks Composer FastAPI Bridge Server

Run this on the Windows machine where Composer is installed:

    pip install fastapi uvicorn pywin32
    python -m uvicorn composer.server:app --host 127.0.0.1 --port 8000

Environment variables (.env or shell):

    SMG_PATH        Path to your .smg file (required for COM mode)
    RENDER_DIR      Where rendered PNGs are saved (default: assets/composer_renders)
    BRIDGE_MODE     auto | com | folder  (default: auto)
                      auto   — tries COM first; falls back to folder mode
                      com    — force COM automation (Composer must be open)
                      folder — read pre-rendered PNGs from RENDER_DIR
    COMPOSER_EXE    Full path to SolidWorksComposer.exe (auto-detected if unset)
    COMPOSER_PROGID Override COM ProgID (run discover_com.py to find yours)

Folder mode workflow (no COM needed):
  1. Open your assembly in Composer
  2. For each step, export the view as PNG named <step_id>.png
  3. Drop all PNGs into RENDER_DIR (default: assets/composer_renders/)
  4. Run this server — it serves those images to the pipeline
"""

import os
import shutil
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
# Anchor to project root (one level up from composer/) so .env loads
# regardless of where uvicorn is launched from.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_ENV_PATH)
print(f"  Bridge env: loaded {_ENV_PATH} (exists={_ENV_PATH.exists()})")

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Composer Bridge", version="1.0")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SMG_PATH = os.environ.get("SMG_PATH", "")
RENDER_DIR = Path(os.environ.get("RENDER_DIR", "assets/composer_renders"))
BRIDGE_MODE = os.environ.get("BRIDGE_MODE", "auto").lower()
COMPOSER_PROGID_OVERRIDE = os.environ.get("COMPOSER_PROGID", "")


def _find_composer_exe() -> Optional[str]:
    env = os.environ.get("COMPOSER_EXE")
    if env and Path(env).exists():
        return env
    for candidate in [
        r"C:\Program Files\SolidWorks Corp\SolidWorks Composer\SolidWorksComposer.exe",
        r"C:\Program Files\SolidWorks Corp\SolidWorks Composer\Composer.exe",
        r"C:\Program Files (x86)\SolidWorks Corp\SolidWorks Composer\Composer.exe",
        r"C:\Program Files\Dassault Systemes\SolidWorks Composer\SolidWorksComposer.exe",
    ]:
        if Path(candidate).exists():
            return candidate
    return None


COMPOSER_EXE = _find_composer_exe()

# ---------------------------------------------------------------------------
# COM backend
# ---------------------------------------------------------------------------

_com_app = None
_active_progid = None
_sw_app = None
_sw_available: bool | None = None


def _try_init_com() -> bool:
    global _com_app, _active_progid
    try:
        import win32com.client
        import pythoncom
        pythoncom.CoInitialize()

        prog_ids = (
            [COMPOSER_PROGID_OVERRIDE] if COMPOSER_PROGID_OVERRIDE
            else [
                "SWComposerLib.Application",
                "Composer.Application",
                "SolidWorksComposer.Application",
                "SWComposer.Application",
            ]
        )

        if COMPOSER_PROGID_OVERRIDE:
            print(f"  Composer COM: using override ProgID {COMPOSER_PROGID_OVERRIDE!r}")
        else:
            print("  Composer COM: no COMPOSER_PROGID set, trying default ProgIDs")

        last_error: Exception | None = None
        for prog_id in prog_ids:
            try:
                _com_app = win32com.client.Dispatch(prog_id)
            except Exception as e:
                last_error = e
                print(f"    {prog_id}: dispatch failed ({type(e).__name__}: {e})")
                continue

            _active_progid = prog_id
            print(f"  Composer COM: connected via {prog_id}")

            # OpenFile is a separate, optional step — failing here should not
            # invalidate the COM connection (which is already live).
            if SMG_PATH and Path(SMG_PATH).exists():
                try:
                    # NOTE: method name may differ — check your Composer version
                    _com_app.OpenFile(SMG_PATH)
                except Exception as e:
                    print(f"  Composer COM: OpenFile({SMG_PATH!r}) failed — {type(e).__name__}: {e}")
            return True

        msg = "  Composer COM: no ProgID matched. Run: python -m composer.discover_com"
        if last_error is not None:
            msg += f"\n    last error: {type(last_error).__name__}: {last_error}"
        print(msg)
    except ImportError:
        print("  Composer COM: pywin32 not installed — pip install pywin32")
    return False


def _try_init_sw() -> bool:
    """Probe SolidWorks COM availability — independent of Composer.

    The /diff endpoint needs SolidWorks (SldWorks.Application) but NOT
    Composer. Many machines have one without the other, so each backend
    probes separately. Holding a module-level reference keeps the SW
    process alive across /diff requests instead of starting/stopping
    on each call.
    """
    global _sw_app, _sw_available
    try:
        import win32com.client  # type: ignore
        import pythoncom        # type: ignore
        pythoncom.CoInitialize()
        _sw_app = win32com.client.Dispatch("SldWorks.Application")
        _sw_app.Visible = False
        print("  SolidWorks COM: connected via SldWorks.Application")
        _sw_available = True
        return True
    except ImportError:
        print("  SolidWorks COM: pywin32 not installed — pip install pywin32")
    except Exception as e:
        print(f"  SolidWorks COM: not available ({e}) — /diff will use folder scan")
    _sw_available = False
    return False


def _com_list_views() -> list[dict]:
    # NOTE: adjust property names to match your Composer version.
    # Common candidates: .Views / .Bookmarks / .Viewpoints / .Pages
    doc = _com_app.ActiveDocument  # type: ignore
    result = []
    for v in doc.Views:  # type: ignore
        name = v.Name  # type: ignore
        result.append({"view_id": name, "step_id": name, "name": name})
    return result


def _com_render(view_id: str, out_path: str) -> str:
    doc = _com_app.ActiveDocument  # type: ignore
    for v in doc.Views:  # type: ignore
        if v.Name == view_id:  # type: ignore
            # NOTE: adjust activation method: .Activate() / .SetActive() / .Select()
            v.Activate()  # type: ignore
            # NOTE: adjust export method: .ExportImage() / .SaveImage() / .Publish()
            doc.ExportImage(out_path, "PNG")  # type: ignore
            return out_path
    raise ValueError(f"View '{view_id}' not found in open Composer document")


def _com_author_view(view_id: str, azimuth: float, elevation: float) -> dict:
    doc = _com_app.ActiveDocument  # type: ignore
    for v in doc.Views:  # type: ignore
        if v.Name == view_id:  # type: ignore
            v.Activate()  # type: ignore
            # NOTE: camera access may be doc.Camera or _com_app.Camera
            cam = doc.Camera  # type: ignore
            cam.Azimuth = azimuth  # type: ignore
            cam.Elevation = elevation  # type: ignore
            return {"status": "ok", "azimuth": azimuth, "elevation": elevation}
    raise ValueError(f"View '{view_id}' not found")


# ---------------------------------------------------------------------------
# Folder backend — user drops pre-rendered PNGs into RENDER_DIR
# ---------------------------------------------------------------------------

def _folder_list_views() -> list[dict]:
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    views = []
    for png in sorted(RENDER_DIR.glob("*.png")):
        name = png.stem
        views.append({"view_id": name, "step_id": name, "name": name})
    if not views:
        print(f"  Folder mode: no PNGs found in {RENDER_DIR} — add <step_id>.png files")
    return views


def _folder_render(view_id: str, out_path: str) -> str:
    src = RENDER_DIR / f"{view_id}.png"
    if not src.exists():
        raise FileNotFoundError(
            f"No image found at {src}. "
            f"Export '{view_id}.png' from Composer and place it in {RENDER_DIR}/"
        )
    if str(src.resolve()) != str(Path(out_path).resolve()):
        shutil.copy2(src, out_path)
    return out_path


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def _use_com() -> bool:
    """Gate for Composer-backed endpoints (views, render, author_view)."""
    if BRIDGE_MODE == "folder":
        return False
    if BRIDGE_MODE == "com":
        return _com_app is not None
    return _com_app is not None  # auto: prefer COM when available


def _use_sw_for_diff() -> bool:
    """Gate for the /diff endpoint — needs SolidWorks, not Composer."""
    if BRIDGE_MODE == "folder":
        return False
    return bool(_sw_available)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    if BRIDGE_MODE != "folder":
        _try_init_com()
        _try_init_sw()
    composer_mode = "COM" if _use_com() else f"folder ({RENDER_DIR}/)"
    diff_mode = "SW COM" if _use_sw_for_diff() else "folder scan"
    print(f"  Composer Bridge ready — composer: {composer_mode}, diff: {diff_mode}")
    if not _use_com():
        print(f"  Drop <step_id>.png exports from Composer into {RENDER_DIR}/")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    mode = "com" if _use_com() else "folder"
    if mode == "com" and _com_app is None:
        raise HTTPException(503, "COM requested but Composer not connected")
    return {
        "status": "ok",
        "mode": mode,
        "render_dir": str(RENDER_DIR),
        "smg": SMG_PATH,
        "progid": _active_progid,
    }


@app.get("/views")
def list_views():
    try:
        if _use_com():
            return _com_list_views()
        return _folder_list_views()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/render/{view_id}")
def render_view(view_id: str):
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    out_path = str(RENDER_DIR / f"{view_id}.png")
    try:
        if _use_com():
            path = _com_render(view_id, out_path)
        else:
            path = _folder_render(view_id, out_path)
        return {"png_path": path}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


class SyncRequest(BaseModel):
    smg_path: str
    new_cad_path: str


@app.post("/sync")
def sync(req: SyncRequest):
    global SMG_PATH
    SMG_PATH = req.smg_path
    if _com_app is not None and req.smg_path:
        try:
            _com_app.OpenFile(req.smg_path)  # type: ignore
        except Exception as e:
            print(f"  Sync warning: {e}")
    return {"status": "ok", "smg_path": SMG_PATH}


@app.get("/mates/{part_number}")
def get_mates(part_number: str):
    return {"part_number": part_number, "mates": []}


# ---------------------------------------------------------------------------
# /diff — compare two SolidWorks assemblies and return component-level diff
# ---------------------------------------------------------------------------

class DiffRequest(BaseModel):
    before_path: str
    after_path: str


class ExtractRequest(BaseModel):
    assembly_path: str


@app.post("/extract_assembly")
def extract_assembly(req: ExtractRequest):
    """
    Walk one SolidWorks assembly and return its full BoM + mate graph.

    Used by the Phase 4 generation pipeline (stages/cad_extract.py) as the
    single source of truth about the design that's being documented.

    Response shape (matches schemas.cad.BoM):
      {
        "assembly_path": str,
        "components": [
          {"name": str, "quantity": int, "mass_kg": float | null,
           "properties": dict[str, str], "dimensions": dict[str, float]}
        ],
        "mates": [{"name": str, "type": str, "parts": [str, str]}]
      }
    """
    if not _use_sw_for_diff():
        raise HTTPException(503, "SolidWorks COM unavailable — /extract_assembly requires SldWorks")
    try:
        return _com_extract_assembly(req.assembly_path)
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


def _com_extract_assembly(assembly_path: str) -> dict:
    import pythoncom  # type: ignore
    pythoncom.CoInitialize()

    sw = _sw_app
    if sw is None:
        raise RuntimeError("SldWorks COM application not initialized")

    # swDocASSEMBLY = 2
    doc = _open_doc6(sw, assembly_path, 2)
    try:
        comp_map = _get_assembly_components(doc)
        mates = _get_assembly_mates_with_parts(doc)

        # Attach per-component dimensions. The cheap path: walk each unique
        # part file once and call _get_part_dimensions on its open doc.
        # Failures are non-fatal — dimensions are nice-to-have.
        components_out: list[dict] = []
        for name, data in comp_map.items():
            dims: dict[str, float] = {}
            part_path = data.get("actual_path") or ""
            if part_path and part_path.lower().endswith((".sldprt", ".prt")):
                try:
                    # swDocPART = 1
                    part_doc = _open_doc6(sw, part_path, 1)
                    dims = _get_part_dimensions(part_doc)
                    try:
                        sw.CloseDoc(Path(part_path).name)
                    except Exception:
                        pass
                except Exception:
                    dims = {}
            components_out.append({
                "name": name,
                "quantity": data.get("quantity", 1),
                "mass_kg": data.get("mass_kg"),
                "properties": data.get("properties", {}),
                "dimensions": dims,
            })

        return {
            "assembly_path": assembly_path,
            "components": components_out,
            "mates": mates,
        }
    finally:
        try:
            sw.CloseDoc(Path(assembly_path).name)
        except Exception:
            pass


def _get_assembly_mates_with_parts(doc) -> list[dict]:
    """
    Like _get_assembly_mates, but also records which two components each
    mate joins. Falls back to an empty parts list when entity walking fails;
    callers must tolerate that — the sequencer treats parts-less mates as
    metadata-only and won't use them for graph edges.
    """
    mates: list[dict] = []
    try:
        feat = doc.FirstFeature()
        while feat:
            try:
                if feat.GetTypeName2() in ("MateGroup", "MateGroup1"):
                    sub = feat.GetFirstSubFeature()
                    while sub:
                        try:
                            sf = sub.GetSpecificFeature2()
                            if sf is not None:
                                try:
                                    mate_type = _MATE_TYPE_NAMES.get(
                                        int(sf.Type), sub.GetTypeName2()
                                    )
                                except Exception:
                                    mate_type = sub.GetTypeName2()
                                parts = _read_mate_parts(sf)
                                mates.append({
                                    "name": sub.Name,
                                    "type": mate_type,
                                    "parts": parts,
                                })
                        except Exception:
                            pass
                        try:
                            sub = sub.GetNextSubFeature()
                        except Exception:
                            break
            except Exception:
                pass
            try:
                feat = feat.GetNextFeature()
            except Exception:
                break
    except Exception:
        pass
    return mates


def _read_mate_parts(mate) -> list[str]:
    """Pull the two component names off a Mate2 specific-feature object."""
    parts: list[str] = []
    try:
        count = mate.GetMateEntityCount()
    except Exception:
        return parts
    for i in range(min(count, 4)):
        try:
            ent = mate.MateEntity(i)
            comp = ent.ReferenceComponent
            path = _sw_get_path_name(comp)
            if path:
                parts.append(Path(path).name.upper())
        except Exception:
            continue
    # Deduplicate but preserve order; a mate occasionally lists the same
    # component twice when both entities are on one part.
    seen: set[str] = set()
    unique: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique[:2]


@app.post("/diff")
def assembly_diff(req: DiffRequest):
    """
    Compare two SolidWorks assemblies and return all component differences.

    Response shape:
      {
        "before_path": str,
        "after_path": str,
        "components_added":   [{"name": str, "quantity": int}],
        "components_removed": [{"name": str, "quantity": int}],
        "components_changed": [{
            "name": str,
            "quantity_before": int, "quantity_after": int,
            "property_changes": [{"property": str, "before": str, "after": str}]
        }]
      }
    """
    try:
        if _use_sw_for_diff():
            return _com_assembly_diff(req.before_path, req.after_path)
        return _folder_assembly_diff(req.before_path, req.after_path)
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# SolidWorks mate type name lookup (swMateType_e enum)
# ---------------------------------------------------------------------------

_MATE_TYPE_NAMES: dict[int, str] = {
    0: "Coincident",       1: "Concentric",      2: "Perpendicular",
    3: "Parallel",         4: "Tangent",          5: "Distance",
    6: "Angle",            7: "Lock",             8: "Gear",
    9: "Rack_Pinion",     10: "Symmetric",        11: "Universal_Joint",
    12: "Cam",            13: "Profile_Center",   14: "Width",
    15: "Hinge",          16: "Linear_Coupler",   17: "Slot",
    18: "Path",           19: "Custom",
}


def _sw_get_path_name(com_obj) -> str:
    """Read GetPathName from an IComponent2 or IModelDoc2.

    SolidWorks declares GetPathName as a property, but late-bound pywin32
    sometimes auto-detects it as a method and other times as a property —
    depending on the typelib state and the COM class. Calling it the wrong
    way raises ``TypeError: 'str' object is not callable`` (when it returned
    the value directly) or fails to bind. Probe ``callable`` and dispatch
    accordingly so this works in both modes.
    """
    try:
        attr = com_obj.GetPathName
    except Exception:
        return ""
    try:
        return (attr() if callable(attr) else attr) or ""
    except Exception:
        return ""


def _read_component_data(comp) -> dict:
    """
    Extract all available data from a single IComponent2:
      - actual_path: real on-disk path (original case) for deep-diff use
      - custom properties (all keys, lower-cased)
      - mass_kg / volume_m3 / surface_area_m2 from IModelDoc2.GetMassProperties
      - active configuration name
      - suppression state

    GetMassProperties returns a 0-indexed variant array:
      [status, mass(kg), volume(m³), surfaceArea(m²), cx, cy, cz, ...]
    status == 1 means success.
    """
    data: dict = {
        "actual_path":     "",
        "properties":      {},
        "mass_kg":         None,
        "volume_m3":       None,
        "surface_area_m2": None,
        "configuration":   "",
        "suppressed":      False,
    }
    try:
        data["suppressed"] = bool(comp.IsSuppressed())
    except Exception:
        pass

    data["actual_path"] = _sw_get_path_name(comp)

    try:
        mdoc = comp.GetModelDoc2()
        if mdoc is None:
            return data

        # Active configuration name
        try:
            cfg = mdoc.GetActiveConfiguration()
            if cfg:
                data["configuration"] = str(cfg.Name)
        except Exception:
            pass

        # Custom properties from the component's model document
        try:
            mgr = mdoc.Extension.CustomPropertyManager("")
            pnames = mgr.GetNames()
            if pnames:
                for pn in pnames:
                    _, _, val, _ = mgr.Get4(pn, False)
                    data["properties"][pn.lower()] = str(val)
        except Exception:
            pass

        # Mass properties — geometry signature for change detection.
        # Calling on the model doc gives per-part values regardless of assembly
        # context, which lets us detect whether the part file itself changed.
        try:
            mp = mdoc.GetMassProperties(0)  # 0 = default coordinate system
            # mp is a variant array; index 0 is status (1 = success)
            if mp and len(mp) >= 4 and mp[0] == 1:
                data["mass_kg"]         = round(float(mp[1]), 6)
                data["volume_m3"]       = round(float(mp[2]), 10)
                data["surface_area_m2"] = round(float(mp[3]), 8)
        except Exception:
            pass

    except Exception:
        pass

    return data


def _get_assembly_components(doc) -> dict[str, dict]:
    """
    Walk the FULL assembly component tree (all sub-assembly levels) and
    return a dict keyed by component filename (upper-cased), e.g.
    'EXTRUDER-BODY_R4.SLDPRT'.

    GetComponents(False) = all levels, topLevelOnly=False.

    Each value:
      {
        "quantity":        int,
        "actual_path":     str,            # real on-disk path (first occurrence)
        "properties":      dict[str, str], # all custom properties
        "mass_kg":         float | None,   # None = lightweight / unavailable
        "volume_m3":       float | None,
        "surface_area_m2": float | None,
        "configuration":   str,
        "suppressed":      bool,
      }
    """
    components: dict[str, dict] = {}
    try:
        comp_array = doc.GetComponents(False)  # False = all levels (recursive)
    except Exception as e:
        print(f"  GetComponents failed on {getattr(doc, 'GetPathName', lambda: '?')()}: "
              f"{type(e).__name__}: {e}")
        return components

    if not comp_array:
        print(f"  GetComponents returned empty for {getattr(doc, 'GetPathName', lambda: '?')()}")
        return components

    per_comp_errors = 0
    first_error: Exception | None = None
    first_error_repr: str = ""
    for comp in comp_array:
        try:
            path = _sw_get_path_name(comp)
            if not path:
                continue
            name = Path(path).name.upper()
            if name not in components:
                cd = _read_component_data(comp)
                components[name] = {
                    "quantity":        0,
                    "actual_path":     cd["actual_path"],
                    "properties":      cd["properties"],
                    "mass_kg":         cd["mass_kg"],
                    "volume_m3":       cd["volume_m3"],
                    "surface_area_m2": cd["surface_area_m2"],
                    "configuration":   cd["configuration"],
                    "suppressed":      cd["suppressed"],
                }
            components[name]["quantity"] += 1
        except Exception as e:
            per_comp_errors += 1
            if first_error is None:
                first_error = e
                try:
                    first_error_repr = (
                        f"type={type(comp).__name__} "
                        f"attrs_sample={sorted(dir(comp))[:8]}"
                    )
                except Exception:
                    first_error_repr = "<repr unavailable>"
            continue
    if per_comp_errors:
        msg = f"  GetComponents: {per_comp_errors} component(s) raised during walk (skipped)"
        if first_error is not None:
            msg += (
                f"\n    first error: {type(first_error).__name__}: {first_error}"
                f"\n    first comp: {first_error_repr}"
            )
        print(msg)
    return components


# ---------------------------------------------------------------------------
# Deep geometric analysis — dimensions and face topology
# ---------------------------------------------------------------------------

# swSurfaceTypes_e
_SURFACE_TYPE_NAMES: dict[int, str] = {
    0: "plane", 1: "cylinder", 2: "cone", 3: "sphere",
    4: "torus", 5: "b_surface", 6: "blend", 7: "offset",
    8: "extruded", 9: "revolved",
}


def _get_part_dimensions(doc) -> dict[str, float]:
    """
    Walk the feature tree and collect every display dimension.

    Returns: {fullName: value_in_SI}
      Lengths are in metres, angles in radians (SolidWorks internal units).
    """
    dims: dict[str, float] = {}
    try:
        feat = doc.FirstFeature()
        while feat:
            try:
                dd = feat.GetFirstDisplayDimension()
                while dd:
                    try:
                        dim = dd.GetDimension2(0)
                        if dim:
                            name = dim.FullName
                            val  = dim.Value
                            if name and val is not None:
                                dims[str(name)] = float(val)
                    except Exception:
                        pass
                    try:
                        dd = dd.GetNext()
                    except Exception:
                        break
            except Exception:
                pass
            try:
                feat = feat.GetNextFeature()
            except Exception:
                break
    except Exception:
        pass
    return dims


def _diff_dimensions(before: dict[str, float], after: dict[str, float]) -> list[dict]:
    """
    Compare two dimension dicts. Lengths converted m→mm, angles rad→deg.
    Threshold: 0.001 mm for lengths, 0.01° for angles.
    """
    changes: list[dict] = []
    all_names = set(before) | set(after)
    for name in sorted(all_names):
        bv = before.get(name)
        av = after.get(name)
        if bv is None or av is None:
            continue  # dimension added/removed — skip (feature diff territory)
        if abs(bv - av) < 1e-9:
            continue  # identical

        # Heuristic: if value < 0.1 it's likely an angle in radians
        is_angle = abs(bv) < 0.5 and abs(av) < 0.5
        if is_angle:
            import math
            b_disp = round(math.degrees(bv), 4)
            a_disp = round(math.degrees(av), 4)
            unit   = "deg"
            threshold = 0.01
        else:
            b_disp = round(bv * 1000, 4)   # m → mm
            a_disp = round(av * 1000, 4)
            unit   = "mm"
            threshold = 0.001

        if abs(b_disp - a_disp) >= threshold:
            changes.append({
                "dimension": name,
                "before":    b_disp,
                "after":     a_disp,
                "unit":      unit,
            })
    return changes


def _get_face_signatures(doc) -> list[dict]:
    """
    Return a compact signature for every face in every solid body of a part doc.

    Each entry: {"type": str, "area_mm2": float, "radius_mm"?: float}
    Cylinders carry radius so we can detect bore/shaft changes specifically.
    """
    sigs: list[dict] = []
    try:
        # 0 = swSolidBody; False = don't include hidden bodies
        bodies = doc.GetBodies2(0, False)
        if not bodies:
            return sigs
        for body in bodies:
            try:
                faces = body.GetFaces()
                if not faces:
                    continue
                for face in faces:
                    try:
                        surf      = face.GetSurface()
                        surf_type = surf.GetType()
                        type_name = _SURFACE_TYPE_NAMES.get(surf_type, f"type_{surf_type}")
                        area_mm2  = round(face.GetArea() * 1e6, 4)  # m² → mm²

                        entry: dict = {"type": type_name, "area_mm2": area_mm2}

                        if surf_type == 1:  # cylinder — extract radius
                            try:
                                # CylinderParams: [ax, ay, az, px, py, pz, radius]
                                params = surf.CylinderParams
                                if params and len(params) >= 7:
                                    entry["radius_mm"] = round(float(params[6]) * 1000, 4)
                            except Exception:
                                pass
                        elif surf_type == 2:  # cone — half-angle
                            try:
                                # ConeParams: [ax, ay, az, px, py, pz, radius, half_angle]
                                params = surf.ConeParams
                                if params and len(params) >= 8:
                                    entry["radius_mm"]    = round(float(params[6]) * 1000, 4)
                                    entry["half_angle_deg"] = round(
                                        float(params[7]) * 57.2958, 4
                                    )
                            except Exception:
                                pass

                        sigs.append(entry)
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    return sigs


def _diff_face_signatures(before: list[dict], after: list[dict]) -> list[dict]:
    """
    Compare face signature lists.

    Strategy per surface type:
      • cylinder: sort by radius, match pairs, flag radius changes ≥ 0.01 mm
        and count changes (new holes / removed holes).
      • others: compare count and total area per type.

    Returns a list of human-readable change dicts.
    """
    changes: list[dict] = []

    def _group(sigs: list[dict]) -> dict[str, list[dict]]:
        g: dict[str, list[dict]] = {}
        for s in sigs:
            g.setdefault(s["type"], []).append(s)
        return g

    bg = _group(before)
    ag = _group(after)
    all_types = set(bg) | set(ag)

    for ftype in sorted(all_types):
        bf = sorted(bg.get(ftype, []),
                    key=lambda x: (x.get("radius_mm", 0), x["area_mm2"]))
        af = sorted(ag.get(ftype, []),
                    key=lambda x: (x.get("radius_mm", 0), x["area_mm2"]))

        # Count change
        if len(bf) != len(af):
            changes.append({
                "surface_type": ftype,
                "change":       "count",
                "before":       len(bf),
                "after":        len(af),
            })

        # Cylinder radius comparison (holes / shafts)
        if ftype == "cylinder":
            for b_face, a_face in zip(bf, af):
                br = b_face.get("radius_mm")
                ar = a_face.get("radius_mm")
                if br is not None and ar is not None:
                    if abs(br - ar) >= 0.01:
                        changes.append({
                            "surface_type": "cylinder",
                            "change":       "radius",
                            "before_mm":    br,
                            "after_mm":     ar,
                        })
        else:
            # For planar/other faces: flag if total area changed by >1%
            b_area = sum(f["area_mm2"] for f in bf)
            a_area = sum(f["area_mm2"] for f in af)
            if b_area > 0 and abs(b_area - a_area) / b_area > 0.01:
                changes.append({
                    "surface_type": ftype,
                    "change":       "total_area_mm2",
                    "before":       round(b_area, 2),
                    "after":        round(a_area, 2),
                })

    return changes


def _deep_part_diff(before_path: str, after_path: str, sw) -> dict:
    """
    Open two individual part/assembly files in SolidWorks and compare:
      1. All display dimensions (feature tree walk)
      2. Face/surface topology (bodies → faces → surface type + cylinder radii)

    Called only for parts flagged by the shallow mass/volume diff, so the
    total number of expensive opens is small (typically 3-8 for a real ECO).

    Returns: {"dimension_changes": [...], "face_changes": [...]}
    """
    result: dict = {"dimension_changes": [], "face_changes": []}

    b_suffix = Path(before_path).suffix.upper()
    a_suffix = Path(after_path).suffix.upper()

    # swDocPART=1, swDocASSEMBLY=2, swOpenDocOptions_Silent=1
    b_type = 2 if b_suffix == ".SLDASM" else 1
    a_type = 2 if a_suffix == ".SLDASM" else 1

    b_doc = a_doc = None
    try:
        b_doc = _open_doc6(sw, before_path, b_type)
        a_doc = _open_doc6(sw, after_path,  a_type)

        if b_doc is None or a_doc is None:
            return result

        # Dimensions — only meaningful for part files
        if b_type == 1 and a_type == 1:
            b_dims = _get_part_dimensions(b_doc)
            a_dims = _get_part_dimensions(a_doc)
            result["dimension_changes"] = _diff_dimensions(b_dims, a_dims)
            print(f"    deep dim diff: {len(b_dims)} → {len(a_dims)} dims, "
                  f"{len(result['dimension_changes'])} changed")

        # Face topology — works for both parts and assemblies
        b_faces = _get_face_signatures(b_doc)
        a_faces = _get_face_signatures(a_doc)
        result["face_changes"] = _diff_face_signatures(b_faces, a_faces)
        print(f"    deep face diff: {len(b_faces)} → {len(a_faces)} faces, "
              f"{len(result['face_changes'])} changed")

    except Exception as exc:
        print(f"    deep diff failed for {Path(before_path).name}: {exc}")
    finally:
        for path in (before_path, after_path):
            try:
                sw.CloseDoc(path)
            except Exception:
                pass

    return result


def _get_assembly_mates(doc) -> list[dict]:
    """
    Walk the feature tree and collect every mate constraint.

    SolidWorks stores mates inside a 'MateGroup' feature; each mate is a
    sub-feature with a type name like 'Coincident', 'Distance', etc.

    Returns list of {"name": str, "type": str}.
    """
    mates: list[dict] = []
    try:
        feat = doc.FirstFeature()
        while feat:
            try:
                if feat.GetTypeName2() in ("MateGroup", "MateGroup1"):
                    sub = feat.GetFirstSubFeature()
                    while sub:
                        try:
                            sf = sub.GetSpecificFeature2()
                            if sf is not None:
                                try:
                                    mate_type = _MATE_TYPE_NAMES.get(
                                        int(sf.Type), sub.GetTypeName2()
                                    )
                                except Exception:
                                    mate_type = sub.GetTypeName2()
                                mates.append({"name": sub.Name, "type": mate_type})
                        except Exception:
                            pass
                        try:
                            sub = sub.GetNextSubFeature()
                        except Exception:
                            break
            except Exception:
                pass
            try:
                feat = feat.GetNextFeature()
            except Exception:
                break
    except Exception:
        pass
    return mates


def _open_doc6(sw, path: str, doc_type: int):
    """Call SldWorks.OpenDoc6 with proper VARIANT byref out-params.

    Late-bound COM (no typelib loaded) can't infer that the trailing
    Errors/Warnings args are out-params, so passing bare ``0, 0`` raises
    "Type mismatch" on parameter 5. Wrapping them as VT_BYREF | VT_I4
    VARIANTs lets pywin32 marshal them correctly.

    Returns the opened ModelDoc2 (or raises if SldWorks refused to open
    the file — silently returning None caused the upstream "0 changes"
    bug because the assembly walker swallowed the AttributeError).
    """
    import pythoncom            # type: ignore
    from win32com.client import VARIANT  # type: ignore
    errors   = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    warnings = VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    result = sw.OpenDoc6(path, doc_type, 1, "", errors, warnings)

    # Some pywin32 builds return (doc, *out_values) when there are byref
    # params; others mutate the VARIANTs in place and return just the doc.
    # Normalize.
    if isinstance(result, tuple):
        doc = result[0]
    else:
        doc = result

    err_code  = getattr(errors, "value", 0) or 0
    warn_code = getattr(warnings, "value", 0) or 0
    if err_code or warn_code:
        print(f"  OpenDoc6({Path(path).name}): errors={err_code} warnings={warn_code}")

    if doc is None:
        raise RuntimeError(
            f"OpenDoc6 returned None for {path!r} "
            f"(type={doc_type}, errors={err_code}, warnings={warn_code}). "
            f"Common causes: file path inaccessible from SW process, "
            f"unresolved references, license/permission issue."
        )
    return doc


# ---------------------------------------------------------------------------
# Rename reconciliation — collapse "filename(R2) removed + filename(R3) added"
# into a single component_changed entry.
#
# Layered identity (each layer is only consulted when stronger evidence is
# absent):
#   1. Canonical custom property "Part Number" (and common spellings).
#   2. Normalized filename stem (rev suffixes stripped) gated by mass similarity.
#
# Both layers are conservative: matches must be 1:1 and (for layer 2) within
# ±50% mass to avoid wrongly merging different parts that happen to share a
# generic stem like "plate". Anything that survives both layers stays in the
# add/remove lists.
# ---------------------------------------------------------------------------

import re as _re

_REV_SUFFIX_RE = _re.compile(
    r"(\(R\d+\)|\(REV[_ ]?[A-Z0-9]+\)|_R\d+|_REV[_ ]?[A-Z0-9]+|_v\d+|-R\d+)$",
    _re.IGNORECASE,
)


def _normalize_stem(filename: str) -> str:
    """Strip trailing revision suffixes from a filename stem.

    Examples (case-insensitive):
      BRACKET(R2).SLDPRT  -> BRACKET
      BRACKET_REV_B.SLDPRT -> BRACKET
      BRACKET_v3.SLDPRT   -> BRACKET
      BRACKET.SLDPRT      -> BRACKET (no change)
    """
    stem = Path(filename).stem
    for _ in range(3):  # peel up to 3 stacked suffixes, e.g. "FOO_R2(R3)"
        new = _REV_SUFFIX_RE.sub("", stem)
        if new == stem:
            break
        stem = new
    return stem.upper()


def _canonical_id(comp_data: dict) -> str | None:
    """Return the part's authoritative ID from custom properties, or None.

    Looks across common spellings of the "Part Number" property — many CAD
    shops set this once and rev the file independently, so two files with
    the same Part Number are by convention the same part.

    Case-insensitive on both the key and the value so this works whether or
    not the caller has already lowercased the property keys (the bridge's
    _read_component_data does; hand-built dicts may not).
    """
    props = comp_data.get("properties", {}) or {}
    targets = {"part number", "partnumber", "part_number", "partno", "part no"}
    for k, v in props.items():
        if str(k).lower() in targets:
            s = str(v).strip()
            if s:
                return s.upper()
    return None


def _mass_similar(m_before, m_after, tolerance: float = 0.5) -> bool:
    """True if two masses are similar enough to plausibly be a rev of one part.

    Uses mean-relative drift (|a-b| / mean) so 1 kg vs 2 kg → ratio 0.67 and
    is rejected at the default 50% tolerance — a doubled mass is almost
    certainly a different part. None on either side → True (don't block the
    match on missing data).
    """
    if m_before is None or m_after is None:
        return True
    if m_before == 0 and m_after == 0:
        return True
    mean = (abs(m_before) + abs(m_after)) / 2
    if mean == 0:
        return True
    return abs(m_before - m_after) / mean <= tolerance


def _reconcile_renames(
    added: list[dict],
    removed: list[dict],
    before_comps: dict[str, dict],
    after_comps: dict[str, dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Pull renamed-component pairs out of added/removed and return them as
    extra `components_changed` entries.

    Returns (added_filtered, removed_filtered, extra_changed). Each rename
    pair becomes one entry in extra_changed with:
      • name           — the new filename (after rev)
      • name_before    — the old filename
      • property_changes including {"property": "filename", ...}
      • geometry_changes from the shallow mass/volume/area comparison
      • dimension_changes / face_changes empty — Phase 3 will populate
      • _rename_match — "part_number" or "filename_stem" (diagnostic)
    """
    # ── Layer 1: match by Part Number custom property ────────────────────────
    added_by_pn: dict[str, list[dict]] = {}
    removed_by_pn: dict[str, list[dict]] = {}
    added_unmatched: list[dict] = []
    removed_unmatched: list[dict] = []

    for a in added:
        pn = _canonical_id(after_comps.get(a["name"], {}))
        if pn:
            added_by_pn.setdefault(pn, []).append(a)
        else:
            added_unmatched.append(a)
    for r in removed:
        pn = _canonical_id(before_comps.get(r["name"], {}))
        if pn:
            removed_by_pn.setdefault(pn, []).append(r)
        else:
            removed_unmatched.append(r)

    rename_pairs: list[tuple[dict, dict, str]] = []  # (removed, added, reason)
    for pn, a_list in list(added_by_pn.items()):
        r_list = removed_by_pn.get(pn, [])
        n_pair = min(len(a_list), len(r_list))
        if len(a_list) > 1 or len(r_list) > 1:
            print(f"  rename reconcile: Part Number {pn!r} matched "
                  f"{len(r_list)} removed × {len(a_list)} added — pairing first {n_pair}")
        for a, r in zip(a_list[:n_pair], r_list[:n_pair]):
            rename_pairs.append((r, a, "part_number"))
        added_unmatched.extend(a_list[n_pair:])
        removed_by_pn[pn] = r_list[n_pair:]
    for pn, r_list in removed_by_pn.items():
        removed_unmatched.extend(r_list)

    # ── Layer 2: normalized filename stem + mass sanity gate ─────────────────
    stem_added: dict[str, list[dict]] = {}
    stem_removed: dict[str, list[dict]] = {}
    for a in added_unmatched:
        stem_added.setdefault(_normalize_stem(a["name"]), []).append(a)
    for r in removed_unmatched:
        stem_removed.setdefault(_normalize_stem(r["name"]), []).append(r)

    added_final: list[dict] = []
    removed_final: list[dict] = []
    consumed_added: set[int] = set()
    consumed_removed: set[int] = set()

    for stem, a_list in stem_added.items():
        r_list = stem_removed.get(stem, [])
        if not r_list:
            continue
        if stem == "":
            continue  # empty stem after stripping is too weak a signal
        for a in a_list:
            for r in r_list:
                if id(r) in consumed_removed:
                    continue
                m_after = after_comps.get(a["name"], {}).get("mass_kg")
                m_before = before_comps.get(r["name"], {}).get("mass_kg")
                if not _mass_similar(m_before, m_after):
                    continue
                rename_pairs.append((r, a, "filename_stem"))
                consumed_added.add(id(a))
                consumed_removed.add(id(r))
                break

    for a in added_unmatched:
        if id(a) not in consumed_added:
            added_final.append(a)
    for r in removed_unmatched:
        if id(r) not in consumed_removed:
            removed_final.append(r)

    # ── Build the `components_changed` entries for each pair ─────────────────
    extra_changed: list[dict] = []
    for r, a, reason in rename_pairs:
        before_data = before_comps.get(r["name"], {})
        after_data  = after_comps.get(a["name"], {})

        prop_changes: list[dict] = [{
            "property": "filename",
            "before":   r["name"],
            "after":    a["name"],
        }]
        if r.get("quantity", 1) != a.get("quantity", 1):
            prop_changes.append({
                "property": "quantity",
                "before":   str(r.get("quantity", 1)),
                "after":    str(a.get("quantity", 1)),
            })
        for prop in sorted(
            set(before_data.get("properties", {})) | set(after_data.get("properties", {}))
        ):
            bv = str(before_data.get("properties", {}).get(prop, ""))
            av = str(after_data.get("properties", {}).get(prop, ""))
            if bv != av:
                prop_changes.append({"property": prop, "before": bv, "after": av})

        geo_changes: list[dict] = []
        for k, thresh in [("mass_kg", 1e-4), ("volume_m3", 1e-8), ("surface_area_m2", 1e-6)]:
            bv_f = before_data.get(k)
            av_f = after_data.get(k)
            if bv_f is not None and av_f is not None and abs(bv_f - av_f) > thresh:
                geo_changes.append({"property": k, "before": str(bv_f), "after": str(av_f)})

        extra_changed.append({
            "name":              a["name"],
            "name_before":       r["name"],
            "quantity_before":   r.get("quantity", 1),
            "quantity_after":    a.get("quantity", 1),
            "property_changes":  prop_changes,
            "geometry_changes":  geo_changes,
            "dimension_changes": [],  # populated by Phase 3
            "face_changes":      [],
            "_rename_match":     reason,
        })
        print(f"  rename reconciled ({reason}): {r['name']} -> {a['name']}")

    return added_final, removed_final, extra_changed


def _com_assembly_diff(before_path: str, after_path: str) -> dict:
    """
    Full assembly diff via SolidWorks COM (SldWorks.Application).

    Three-phase approach:
      Phase 1 — Open both assemblies, resolve lightweights, collect component
                 tree (all levels), mass/volume, custom properties, mates.
                 Close the assembly docs.
      Phase 2 — Shallow diff: added/removed parts, quantity/property/mass
                 changes. Identifies which shared parts need deep analysis.
      Phase 3 — Deep diff on flagged parts only: open each individual part
                 file, walk its feature tree for display dimensions, walk its
                 solid bodies for face/surface topology (cylinder radii, etc.).
                 Typically 3–8 files, ~5–15 s extra.

    Response shape:
      {
        "_source": "sw_com_api",
        "before_path": str, "after_path": str,
        "components_added":   [{"name", "quantity", "mass_kg", "properties"}],
        "components_removed": [{"name", "quantity"}],
        "components_changed": [{
            "name", "quantity_before", "quantity_after",
            "name_before",                      # only present on rename pairs
            "_rename_match",                    # "part_number" | "filename_stem"
            "property_changes":  [{"property", "before", "after"}],   # includes filename row on renames
            "geometry_changes":  [{"property", "before", "after"}],
            "dimension_changes": [{"dimension", "before", "after", "unit"}],
            "face_changes":      [{"surface_type", "change", ...}],
        }],
        "mates_added":   [{"name", "type"}],
        "mates_removed": [{"name", "type"}],
      }
    """
    import win32com.client  # type: ignore
    import pythoncom         # type: ignore

    # FastAPI dispatches requests on a worker thread pool; COM apartment
    # state is per-thread, so the startup-time CoInitialize on the main
    # thread doesn't carry over. Init this thread's apartment before any
    # COM call. Safe to call repeatedly on the same thread.
    pythoncom.CoInitialize()

    # Keep one SldWorks session alive across all three phases so part opens
    # in Phase 3 reuse the already-loaded part files from Phase 1.
    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = False

    # ── Phase 1: collect assembly data ───────────────────────────────────────
    # swDocASSEMBLY = 2, swOpenDocOptions_Silent = 1
    before_doc = _open_doc6(sw, before_path, 2)
    after_doc  = _open_doc6(sw, after_path,  2)

    try:
        # Resolve lightweight components so GetMassProperties works on all parts.
        for doc in (before_doc, after_doc):
            try:
                doc.ResolveAllLightweightComponents(False)
            except Exception:
                pass

        before_comps = _get_assembly_components(before_doc)
        after_comps  = _get_assembly_components(after_doc)
        before_mates = _get_assembly_mates(before_doc)
        after_mates  = _get_assembly_mates(after_doc)

        print(f"  SW diff phase 1: {len(before_comps)} parts (before), "
              f"{len(after_comps)} parts (after), "
              f"{len(before_mates)} mates (before), "
              f"{len(after_mates)} mates (after)")
    finally:
        for doc in (before_doc, after_doc):
            try:
                sw.CloseDoc(_sw_get_path_name(doc))
            except Exception:
                pass

    before_names = set(before_comps)
    after_names  = set(after_comps)

    # ── Phase 2: shallow diff ────────────────────────────────────────────────

    # Added components
    added = [
        {
            "name":       n,
            "quantity":   after_comps[n]["quantity"],
            "mass_kg":    after_comps[n]["mass_kg"],
            "properties": after_comps[n]["properties"],
        }
        for n in sorted(after_names - before_names)
    ]

    # Removed components
    removed = [
        {"name": n, "quantity": before_comps[n]["quantity"]}
        for n in sorted(before_names - after_names)
    ]

    # Changed components — shallow signals only; deep analysis added in Phase 3
    changed: list[dict] = []
    for name in sorted(before_names & after_names):
        bc = before_comps[name]
        ac = after_comps[name]
        prop_changes: list[dict] = []
        geo_changes:  list[dict] = []

        # Quantity
        if bc["quantity"] != ac["quantity"]:
            prop_changes.append({
                "property": "quantity",
                "before": str(bc["quantity"]),
                "after":  str(ac["quantity"]),
            })

        # Suppression / configuration state
        if bc["suppressed"] != ac["suppressed"]:
            prop_changes.append({
                "property": "suppressed",
                "before": str(bc["suppressed"]),
                "after":  str(ac["suppressed"]),
            })
        if bc["configuration"] != ac["configuration"] and ac["configuration"]:
            prop_changes.append({
                "property": "configuration",
                "before": bc["configuration"],
                "after":  ac["configuration"],
            })

        # Custom properties
        all_props = set(bc["properties"]) | set(ac["properties"])
        for prop in sorted(all_props):
            bv = bc["properties"].get(prop, "")
            av = ac["properties"].get(prop, "")
            if bv != av:
                prop_changes.append({"property": prop, "before": bv, "after": av})

        # Geometry — mass, volume, surface area
        # Threshold: 0.1 g for mass, 1e-8 m³ (~0.01 cm³) for volume
        for geo_key, threshold in [
            ("mass_kg", 1e-4),
            ("volume_m3", 1e-8),
            ("surface_area_m2", 1e-6),
        ]:
            bv_f = bc.get(geo_key)
            av_f = ac.get(geo_key)
            if bv_f is not None and av_f is not None:
                if abs(bv_f - av_f) > threshold:
                    geo_changes.append({
                        "property": geo_key,
                        "before": str(bv_f),
                        "after":  str(av_f),
                    })

        if prop_changes or geo_changes:
            changed.append({
                "name":             name,
                "quantity_before":  bc["quantity"],
                "quantity_after":   ac["quantity"],
                "property_changes": prop_changes,
                "geometry_changes": geo_changes,
                # Phase 3 will populate these:
                "dimension_changes": [],
                "face_changes":      [],
            })

    # Mate diffs
    before_mate_keys = {(m["name"], m["type"]) for m in before_mates}
    after_mate_keys  = {(m["name"], m["type"]) for m in after_mates}
    mates_added   = [{"name": n, "type": t}
                     for n, t in sorted(after_mate_keys  - before_mate_keys)]
    mates_removed = [{"name": n, "type": t}
                     for n, t in sorted(before_mate_keys - after_mate_keys)]

    # ── Phase 2.5: reconcile renames (filename(R2) → filename(R3)) ───────────
    # Pull rename pairs out of added/removed and re-classify them as changed
    # so downstream consumers see a single "Part modified" ECO instead of
    # bogus add+remove pairs that are actually the same physical part.
    added, removed, renamed_changed = _reconcile_renames(
        added, removed, before_comps, after_comps,
    )
    if renamed_changed:
        print(f"  SW diff phase 2.5: {len(renamed_changed)} rename(s) reconciled")
    changed.extend(renamed_changed)

    # ── Phase 3: deep geometric diff on flagged parts only ───────────────────
    #
    # A part is flagged for deep analysis if:
    #   (a) mass/volume changed  → geometry definitely changed, find specifics
    #   (b) mass data was None on both sides → lightweight couldn't be resolved,
    #       can't confirm unchanged — analyse to be sure
    #
    # Parts that are purely added or removed don't need deep analysis.

    def _before_key(entry: dict) -> str:
        # Renamed entries store the old filename under "name_before"; everyone
        # else uses "name" on both sides.
        return entry.get("name_before") or entry["name"]

    deep_candidates = [
        entry for entry in changed
        if entry["geometry_changes"]                                          # (a)
        or entry.get("_rename_match")                                         # always deep-diff renames
        or (before_comps.get(_before_key(entry), {}).get("mass_kg") is None   # (b)
            and after_comps.get(entry["name"], {}).get("mass_kg") is None)
    ]

    print(f"  SW diff phase 2: {len(added)} added, {len(removed)} removed, "
          f"{len(changed)} changed, {len(deep_candidates)} flagged for deep diff")

    for entry in deep_candidates:
        before_name = _before_key(entry)
        after_name  = entry["name"]
        b_path  = before_comps.get(before_name, {}).get("actual_path", "")
        a_path  = after_comps.get(after_name,  {}).get("actual_path", "")

        if not b_path or not a_path:
            continue

        label = (f"{before_name} → {after_name}"
                 if entry.get("_rename_match") else after_name)
        print(f"  SW diff phase 3: deep diff → {label}")
        deep = _deep_part_diff(b_path, a_path, sw)
        entry["dimension_changes"] = deep["dimension_changes"]
        entry["face_changes"]      = deep["face_changes"]

    print(f"  SW diff phase 3: complete")

    return {
        "_source":            "sw_com_api",
        "before_path":        before_path,
        "after_path":         after_path,
        "components_added":   added,
        "components_removed": removed,
        "components_changed": changed,
        "mates_added":        mates_added,
        "mates_removed":      mates_removed,
    }


def _folder_assembly_diff(before_path: str, after_path: str) -> dict:
    """
    Approximate diff without SolidWorks — compare *.SLDPRT / *.SLDASM files
    present in each assembly's directory.  Less accurate than COM (can't see
    inside nested assemblies), but works on any OS for quick testing.
    """
    exts = {".SLDPRT", ".SLDASM"}

    def _names(p: str) -> dict[str, int]:
        d = Path(p).parent
        return {f.name.upper(): 1 for f in d.iterdir() if f.suffix.upper() in exts}

    before_comps = _names(before_path)
    after_comps  = _names(after_path)
    before_names = set(before_comps)
    after_names  = set(after_comps)

    return {
        "before_path": before_path,
        "after_path":  after_path,
        "components_added":   [{"name": n, "quantity": 1} for n in sorted(after_names - before_names)],
        "components_removed": [{"name": n, "quantity": 1} for n in sorted(before_names - after_names)],
        "components_changed": [],
        "_source": "folder_scan",  # flag so callers know this is approximate
    }


class SldDrwToPdfRequest(BaseModel):
    drawing_path: str
    out_pdf_path: str


@app.post("/slddrw_to_pdf")
def slddrw_to_pdf(req: SldDrwToPdfRequest):
    """
    Open a SolidWorks drawing (.SLDDRW) and SaveAs PDF.

    Used by the ECO pipeline (Scenario A) to normalize drawing inputs so the
    PDF-vs-PDF diff path can consume them without requiring the user to
    pre-convert. Reuses the same SldWorks COM connection as /diff.

    Response: {"pdf_path": str}
    """
    if not _use_sw_for_diff():
        raise HTTPException(503, "SolidWorks COM unavailable — /slddrw_to_pdf requires SldWorks")
    try:
        return {"pdf_path": _com_slddrw_to_pdf(req.drawing_path, req.out_pdf_path)}
    except Exception as e:
        raise HTTPException(500, f"{type(e).__name__}: {e}")


def _com_slddrw_to_pdf(drawing_path: str, out_pdf_path: str) -> str:
    import pythoncom  # type: ignore
    pythoncom.CoInitialize()

    sw = _sw_app
    if sw is None:
        raise RuntimeError("SldWorks COM application not initialized")

    if not Path(drawing_path).exists():
        raise FileNotFoundError(f"Drawing not found: {drawing_path}")
    if Path(drawing_path).suffix.lower() != ".slddrw":
        raise ValueError(f"Expected .SLDDRW, got {Path(drawing_path).suffix}")

    Path(out_pdf_path).parent.mkdir(parents=True, exist_ok=True)

    # swDocDRAWING = 3
    doc = _open_doc6(sw, drawing_path, 3)
    try:
        # SaveAs returns True on success in most pywin32 builds, though some
        # late-bound variants return a tuple. Coerce to bool.
        ok = doc.SaveAs(out_pdf_path)
        ok = bool(ok[0]) if isinstance(ok, tuple) else bool(ok)
        if not ok or not Path(out_pdf_path).exists():
            raise RuntimeError(f"SaveAs PDF failed for {drawing_path}")
        return out_pdf_path
    finally:
        try:
            sw.CloseDoc(Path(drawing_path).name)
        except Exception:
            pass


class AuthorViewRequest(BaseModel):
    view_id: str
    azimuth: float
    elevation: float


@app.post("/author_view")
def author_view(req: AuthorViewRequest):
    if not _use_com():
        raise HTTPException(
            501,
            "author_view requires COM mode. Set BRIDGE_MODE=com and ensure Composer is open."
        )
    try:
        return _com_author_view(req.view_id, req.azimuth, req.elevation)
    except Exception as e:
        raise HTTPException(500, str(e))
