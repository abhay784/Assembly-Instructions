"""Render an ECOReport to HTML and PDF.

Uses Jinja2 (already pinned) + Playwright (already used by stages/pdf_generator.py
for the instruction pipeline). No new dependencies — and no rendered CAD
images, just the text diff list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from schemas.eco import ECOReport


_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def render_html(report: ECOReport) -> str:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "htm"]),
    )
    template = env.get_template("eco.html.j2")
    return template.render(
        report=report,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )


def render_pdf(report: ECOReport, out_path: str | Path) -> Path:
    """Render to PDF via Playwright. Returns the PDF path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    html = render_html(report)

    # Local import — Playwright is heavyweight and the tests stub render_pdf.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html)
        page.pdf(path=str(out_path), print_background=True, format="Letter")
        browser.close()

    return out_path
