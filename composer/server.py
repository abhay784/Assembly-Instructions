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

        for prog_id in prog_ids:
            try:
                _com_app = win32com.client.Dispatch(prog_id)
                _active_progid = prog_id
                print(f"  Composer COM: connected via {prog_id}")
                if SMG_PATH and Path(SMG_PATH).exists():
                    # NOTE: method name may differ — check your Composer version
                    _com_app.OpenFile(SMG_PATH)
                return True
            except Exception:
                continue

        print("  Composer COM: no ProgID matched. Run: python -m composer.discover_com")
    except ImportError:
        print("  Composer COM: pywin32 not installed — pip install pywin32")
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
    if BRIDGE_MODE == "folder":
        return False
    if BRIDGE_MODE == "com":
        return _com_app is not None
    return _com_app is not None  # auto: prefer COM when available


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup():
    RENDER_DIR.mkdir(parents=True, exist_ok=True)
    if BRIDGE_MODE != "folder":
        _try_init_com()
    mode = "COM" if _use_com() else f"folder ({RENDER_DIR}/)"
    print(f"  Composer Bridge ready — mode: {mode}")
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
        if _use_com():
            return _com_assembly_diff(req.before_path, req.after_path)
        return _folder_assembly_diff(req.before_path, req.after_path)
    except Exception as e:
        raise HTTPException(500, str(e))


def _get_assembly_components(doc) -> dict[str, dict]:
    """
    Walk the assembly component tree and return a dict keyed by component
    filename (upper-cased stem, e.g. 'BEARING_6MM.SLDPRT').
    Each value: {"quantity": int, "properties": dict[str, str]}.
    """
    components: dict[str, dict] = {}
    try:
        comp_array = doc.GetComponents(False)  # False = top-level only
        if not comp_array:
            return components
        for comp in comp_array:
            try:
                path = comp.GetPathName()
                if not path:
                    continue
                name = Path(path).name.upper()
                if name not in components:
                    # Pull custom properties
                    props: dict[str, str] = {}
                    try:
                        mgr = comp.GetModelDoc2().Extension.CustomPropertyManager("")
                        names = mgr.GetNames()
                        if names:
                            for pn in names:
                                _, _, val, _ = mgr.Get4(pn, False)
                                props[pn.lower()] = str(val)
                    except Exception:
                        pass
                    components[name] = {"quantity": 0, "properties": props}
                components[name]["quantity"] += 1
            except Exception:
                continue
    except Exception:
        pass
    return components


def _com_assembly_diff(before_path: str, after_path: str) -> dict:
    """Full assembly diff via SolidWorks COM — Windows only."""
    import win32com.client  # type: ignore
    sw = win32com.client.Dispatch("SldWorks.Application")
    sw.Visible = False

    before_doc = sw.OpenDoc6(before_path, 2, 1, "", 0, 0)  # 2 = swDocASSEMBLY
    after_doc  = sw.OpenDoc6(after_path,  2, 1, "", 0, 0)

    try:
        before_comps = _get_assembly_components(before_doc)
        after_comps  = _get_assembly_components(after_doc)
    finally:
        try:
            sw.CloseDoc(before_doc.GetPathName())
            sw.CloseDoc(after_doc.GetPathName())
        except Exception:
            pass

    before_names = set(before_comps)
    after_names  = set(after_comps)

    added = [
        {"name": n, "quantity": after_comps[n]["quantity"]}
        for n in sorted(after_names - before_names)
    ]
    removed = [
        {"name": n, "quantity": before_comps[n]["quantity"]}
        for n in sorted(before_names - after_names)
    ]
    changed = []
    for name in sorted(before_names & after_names):
        bc = before_comps[name]
        ac = after_comps[name]
        prop_changes = []
        if bc["quantity"] != ac["quantity"]:
            prop_changes.append({
                "property": "quantity",
                "before": str(bc["quantity"]),
                "after": str(ac["quantity"]),
            })
        all_props = set(bc["properties"]) | set(ac["properties"])
        for prop in sorted(all_props):
            bv = bc["properties"].get(prop, "")
            av = ac["properties"].get(prop, "")
            if bv != av:
                prop_changes.append({"property": prop, "before": bv, "after": av})
        if prop_changes:
            changed.append({
                "name": name,
                "quantity_before": bc["quantity"],
                "quantity_after":  ac["quantity"],
                "property_changes": prop_changes,
            })

    return {
        "before_path": before_path,
        "after_path":  after_path,
        "components_added":   added,
        "components_removed": removed,
        "components_changed": changed,
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
