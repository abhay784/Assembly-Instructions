"""
SolidWorks COM Renderer

Connects to a running SolidWorks instance (or launches one), opens the
assembly file, and renders a PNG for each step at the calculated camera angle.

No SolidWorks Composer or .smg file needed — works directly from the .sldasm
file the pipeline already receives via --after-model.

The angle for each step is provided by finetune/angle_optimizer.py which uses
heuristics based on part types (fasteners → isometric, gears → face-on, etc.).

Prerequisites:
    pip install pywin32
    SolidWorks must be installed and licensed on this machine.
"""

import time
from pathlib import Path


# swDocumentTypes_e
_SW_DOC_ASSEMBLY = 2

# swOpenDocOptions_e
_SW_OPEN_SILENT = 1

# swStandardViews_e  (name, numeric ID for ShowNamedView2)
_SW_NAMED_VIEWS = {
    "front":     ("*Front",     1),
    "back":      ("*Back",      2),
    "left":      ("*Left",      3),
    "right":     ("*Right",     4),
    "top":       ("*Top",       5),
    "bottom":    ("*Bottom",    6),
    "isometric": ("*Isometric", 7),
    "dimetric":  ("*Dimetric",  8),
    "trimetric": ("*Trimetric", 9),
}

# swSaveAsOptions_e
_SW_SAVE_SILENT = 1


def angle_to_named_view(azimuth: float, elevation: float) -> tuple[str, int]:
    """
    Map azimuth/elevation degrees to the closest SolidWorks standard view.

    azimuth  : 0 = front, 90 = right, 180 = back, 270 = left
    elevation: 0 = horizontal, 90 = looking straight down
    """
    if elevation >= 65:
        return _SW_NAMED_VIEWS["top"]
    if elevation <= -65:
        return _SW_NAMED_VIEWS["bottom"]

    if elevation >= 25:           # angled views
        az = azimuth % 360
        if az <= 90 or az >= 315:
            return _SW_NAMED_VIEWS["isometric"]   # front-right-top (default)
        elif az <= 180:
            return _SW_NAMED_VIEWS["dimetric"]    # right-back-top
        else:
            return _SW_NAMED_VIEWS["trimetric"]   # back-left-top

    # Near-horizontal — map to nearest cardinal face
    az = azimuth % 360
    if az <= 45 or az >= 315:
        return _SW_NAMED_VIEWS["front"]
    elif az <= 135:
        return _SW_NAMED_VIEWS["right"]
    elif az <= 225:
        return _SW_NAMED_VIEWS["back"]
    else:
        return _SW_NAMED_VIEWS["left"]


class SolidWorksRenderer:
    """
    Automates SolidWorks via COM to render assembly step images as PNG.

    Usage:
        renderer = SolidWorksRenderer("C:/path/assembly.sldasm")
        if renderer.connect() and renderer.open_assembly():
            png = renderer.render_step(
                step_id="step_1",
                azimuth=45.0, elevation=35.0,
                output_path="assets/renders/step_1.png",
            )
        renderer.close()
    """

    def __init__(self, assembly_path: str):
        self.assembly_path = str(Path(assembly_path).resolve())
        self._sw = None
        self._doc = None
        self._win32 = None
        self._pcom = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Attach to a running SolidWorks instance, or launch a new one."""
        try:
            import win32com.client
            import pythoncom
            pythoncom.CoInitialize()
            self._win32 = win32com.client
            self._pcom = pythoncom

            # Prefer attaching (no startup cost)
            try:
                self._sw = win32com.client.GetActiveObject("SldWorks.Application")
                print("  SW Renderer: attached to running SolidWorks")
            except Exception:
                self._sw = win32com.client.Dispatch("SldWorks.Application")
                self._sw.Visible = True
                print("  SW Renderer: launched SolidWorks — waiting for init...")
                time.sleep(8)

            return True

        except ImportError:
            print("  SW Renderer: pywin32 not installed — run:  pip install pywin32")
            return False
        except Exception as e:
            print(f"  SW Renderer: connection failed — {e}")
            return False

    # ------------------------------------------------------------------
    # Document management
    # ------------------------------------------------------------------

    def open_assembly(self) -> bool:
        """Open the assembly; reuse it if already open in this SolidWorks session."""
        if self._sw is None:
            return False
        try:
            path_lower = self.assembly_path.lower()

            # Reuse already-open document
            docs = self._sw.GetDocuments()
            if docs:
                for doc in docs:
                    try:
                        if doc.GetPathName().lower() == path_lower:
                            self._doc = doc
                            self._sw.ActivateDoc3(self.assembly_path, True, 0, 0)
                            print("  SW Renderer: activated open assembly")
                            return True
                    except Exception:
                        continue

            # Open fresh
            errors = self._win32.VARIANT(
                self._pcom.VT_BYREF | self._pcom.VT_I4, 0
            )
            warnings = self._win32.VARIANT(
                self._pcom.VT_BYREF | self._pcom.VT_I4, 0
            )
            self._doc = self._sw.OpenDoc6(
                self.assembly_path,
                _SW_DOC_ASSEMBLY,
                _SW_OPEN_SILENT,
                "",       # configuration (blank = use default)
                errors,
                warnings,
            )
            if self._doc is None:
                print(
                    "  SW Renderer: OpenDoc6 returned None — "
                    "verify the path exists and SolidWorks is licensed"
                )
                return False

            print(f"  SW Renderer: opened {Path(self.assembly_path).name}")
            return True

        except Exception as e:
            print(f"  SW Renderer: open_assembly failed — {e}")
            return False

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render_step(
        self,
        step_id: str,
        azimuth: float,
        elevation: float,
        output_path: str,
    ) -> str:
        """
        Apply the named view closest to azimuth/elevation, fit to window,
        and export a PNG to output_path.  Returns the path on success.
        """
        if self._doc is None:
            raise RuntimeError("No assembly open — call open_assembly() first")

        view_name, view_id = angle_to_named_view(azimuth, elevation)
        print(f"    step {step_id}: view={view_name} ({azimuth}°/{elevation}°)")

        # Set camera and fit
        self._doc.ShowNamedView2(view_name, view_id)
        self._doc.ViewZoomtofit2()
        time.sleep(0.4)   # allow GL redraw

        # Export
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        errors = self._win32.VARIANT(
            self._pcom.VT_BYREF | self._pcom.VT_I4, 0
        )
        warnings = self._win32.VARIANT(
            self._pcom.VT_BYREF | self._pcom.VT_I4, 0
        )

        ok = self._doc.Extension.SaveAs3(
            output_path,      # .png extension → image export
            0,                # swSaveAsCurrentVersion
            _SW_SAVE_SILENT,
            None,             # IExportPDFData (unused for images)
            None,
            errors,
            warnings,
        )

        if not ok:
            err_val = getattr(errors, "value", errors)
            raise RuntimeError(
                f"SaveAs3 failed for '{step_id}' (SW error code {err_val}). "
                "Ensure the output directory is writable."
            )

        return output_path

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self):
        self._doc = None
        self._sw = None
        try:
            if self._pcom:
                self._pcom.CoUninitialize()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
