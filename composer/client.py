"""
Composer FastAPI bridge client.

Calls the local SolidWorks Composer service at SOLIDWORKS_API (default
http://localhost:8000). The service runs on Windows with the Composer COM
API and is never exposed beyond loopback.

Endpoints:
  POST /sync                    — sync .smg with updated CAD file
  GET  /views                   — list all views with their step_id mappings
  POST /render/{view_id}        — render a view to PNG, returns path
  GET  /mates/{part_number}     — return mate constraints for a part
  POST /author_view             — v1 stub, always returns 501
"""

import os
from pathlib import Path

import httpx


class ComposerClient:
    def __init__(self):
        base = os.environ.get("SOLIDWORKS_API", "http://localhost:8000")
        self._base = base.rstrip("/")
        self._client = httpx.Client(base_url=self._base, timeout=60.0)

    def sync(self, smg_path: str, new_cad_path: str) -> dict:
        resp = self._client.post(
            "/sync",
            json={"smg_path": smg_path, "new_cad_path": new_cad_path},
        )
        resp.raise_for_status()
        return resp.json()

    def list_views(self) -> list[dict]:
        resp = self._client.get("/views")
        if resp.status_code >= 400:
            # Surface the bridge's HTTPException detail so callers see the
            # actual COM/SW error (e.g. "no document open", "Views property
            # missing on viewer-only ActiveX") instead of a bare 500.
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Bridge /views failed (HTTP {resp.status_code}): {detail}")
        return resp.json()

    def render(self, view_id: str) -> str:
        """Render a view and return the PNG path on the Windows filesystem."""
        resp = self._client.post(f"/render/{view_id}")
        resp.raise_for_status()
        return resp.json()["png_path"]

    def get_mates(self, part_number: str) -> dict:
        resp = self._client.get(f"/mates/{part_number}")
        resp.raise_for_status()
        return resp.json()

    def diff(self, before_path: str, after_path: str) -> dict:
        """
        Compare two SolidWorks assemblies and return full component-level
        and geometric differences.

        Returns a dict with keys:
          _source, before_path, after_path,
          components_added, components_removed, components_changed,
          mates_added, mates_removed
        """
        # SolidWorks COM runs in its own process with its own CWD and cannot
        # resolve paths relative to the bridge's working directory. Always
        # send absolute, OS-native paths.
        before_abs = str(Path(before_path).resolve())
        after_abs  = str(Path(after_path).resolve())

        resp = self._client.post(
            "/diff",
            json={"before_path": before_abs, "after_path": after_abs},
            # 300 s: opening two assemblies + resolving all lightweight
            # components + reading mass properties across ~60 parts each
            timeout=300.0,
        )
        if resp.status_code >= 400:
            # Surface the bridge's HTTPException detail so callers see the
            # actual COM/SW error instead of a bare "500 Internal Server Error".
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Bridge /diff failed (HTTP {resp.status_code}): {detail}")
        return resp.json()

    def extract_assembly(self, assembly_path: str) -> dict:
        """
        Walk one SolidWorks assembly and return its full BoM + mate graph.
        Used by the Phase 4 generation pipeline.
        """
        abs_path = str(Path(assembly_path).resolve())
        resp = self._client.post(
            "/extract_assembly",
            json={"assembly_path": abs_path},
            timeout=300.0,
        )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text
            raise RuntimeError(f"Bridge /extract_assembly failed (HTTP {resp.status_code}): {detail}")
        return resp.json()

    def author_view(self, view_id: str, azimuth: float, elevation: float) -> dict:
        """Set camera angle for a view (azimuth, elevation in degrees)."""
        resp = self._client.post(
            "/author_view",
            json={"view_id": view_id, "azimuth": azimuth, "elevation": elevation},
        )
        resp.raise_for_status()
        return resp.json()

    def health(self) -> bool:
        try:
            resp = self._client.get("/health", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
