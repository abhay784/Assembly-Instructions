"""Wrap the bridge's /slddrw_to_pdf endpoint with graceful failure.

Returns the PDF path on success, None on failure. The router treats a
None return as "drawing unavailable" and downgrades the scenario.
"""

from __future__ import annotations

from pathlib import Path

from composer.client import ComposerClient


def convert(drawing_path: str | Path, out_dir: str | Path) -> Path | None:
    drawing_path = Path(drawing_path)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_pdf = out_dir / (drawing_path.stem + ".pdf")

    try:
        with ComposerClient() as client:
            if not client.health():
                print(f"  SLDDRW→PDF: bridge unreachable, skipping {drawing_path.name}")
                return None
            client.slddrw_to_pdf(str(drawing_path), str(out_pdf))
    except Exception as exc:
        print(f"  SLDDRW→PDF: failed for {drawing_path.name} ({exc})")
        return None

    if not out_pdf.exists():
        print(f"  SLDDRW→PDF: bridge reported success but {out_pdf} is missing")
        return None
    return out_pdf
