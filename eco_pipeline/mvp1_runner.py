"""Run MVP1Compare's CLI as a subprocess (no imports, no edits to that repo).

Returns the path to MVP1's changeset.json on success, or None on failure
(in which case the caller downgrades to Scenario B/C).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_MVP1_CLI = _REPO_ROOT / "MVP1Compare" / "cli.py"


def run(
    before_pdf: str | Path,
    after_pdf: str | Path,
    out_dir: str | Path,
    job_id: str,
    document_id: str | None = None,
) -> dict | None:
    """Shell out to MVP1Compare/cli.py compare and return paths to its outputs.

    Returns:
      {
        "changeset_json": Path,
        "report_pdf": Path,
      }
      or None if MVP1 isn't checked out, the CLI fails, or the expected
      outputs are missing.
    """
    if not _MVP1_CLI.exists():
        print(f"  mvp1_runner: MVP1Compare/cli.py not found at {_MVP1_CLI}")
        return None

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(_MVP1_CLI),
        "compare",
        str(Path(before_pdf).resolve()),
        str(Path(after_pdf).resolve()),
        "--out-dir", str(out_dir),
        "--job-id", job_id,
    ]
    if document_id:
        cmd += ["--part-number", document_id]

    print(f"  mvp1_runner: invoking {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            cwd=str(_MVP1_CLI.parent),
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        print("  mvp1_runner: MVP1Compare exceeded 15 min timeout")
        return None
    except FileNotFoundError as exc:
        print(f"  mvp1_runner: cannot invoke python ({exc})")
        return None

    if result.returncode != 0:
        print(f"  mvp1_runner: exited {result.returncode}")
        if result.stderr:
            print(f"  mvp1_runner stderr:\n{result.stderr[:2000]}")
        return None

    job_dir = out_dir / job_id
    changeset_json = job_dir / "changeset.json"
    report_pdf = job_dir / "report.pdf"

    # Some MVP1 runs write changeset.json under metadata/<job_id>/ — handle
    # both layouts.
    if not changeset_json.exists():
        alt = job_dir / "metadata" / job_id / "changeset.json"
        if alt.exists():
            changeset_json = alt

    if not changeset_json.exists():
        print(f"  mvp1_runner: MVP1 ran but no changeset.json at {changeset_json}")
        return None

    if not report_pdf.exists():
        print(f"  mvp1_runner: MVP1 ran but no report.pdf at {report_pdf}")
        # Still return — caller can use changeset and synthesize its own PDF.
        report_pdf = None  # type: ignore[assignment]

    return {"changeset_json": changeset_json, "report_pdf": report_pdf}


def copy_report(report_pdf: Path, dest: Path) -> Path | None:
    if not report_pdf or not Path(report_pdf).exists():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(report_pdf, dest)
    return dest
