"""Wrap the composer /diff endpoint for the ECO pipeline.

Returns the raw diff blob (same shape as `eco/diff_mapper.sw_diff_to_ecos`
already consumes) plus a thin error path so the CLI can surface a clear
failure if the bridge is down.
"""

from __future__ import annotations

from pathlib import Path

from composer.client import ComposerClient


def run(before_model: str | Path, after_model: str | Path) -> dict:
    with ComposerClient() as client:
        if not client.health():
            raise RuntimeError(
                "Composer bridge is not running — start it with: "
                "python -m uvicorn composer.server:app --host 127.0.0.1 --port 8000"
            )
        return client.diff(str(before_model), str(after_model))
