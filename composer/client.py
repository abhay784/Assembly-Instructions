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
        resp.raise_for_status()
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
