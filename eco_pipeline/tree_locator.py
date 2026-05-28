"""Build per-component location strings for the CAD-diff ECO path.

For each changed/added/removed component, produces:
  • tree_path     — top-down readable path, e.g.
                    "TopAsm.SLDASM > Gearbox.SLDASM > Bracket.SLDPRT"
                    Today this is the assembly file name + component file
                    name; full nested-path reconstruction requires
                    component.Parent walks the current /extract_assembly
                    endpoint does not yet expose, so we approximate with
                    the assembly file and the component name. Better than
                    no location at all and still unambiguous against the
                    BOM.
  • mate_neighbors — sorted unique list of components that share a mate
                    with this part. Pulled from /extract_assembly's
                    mates[].parts.

The locator is best-effort: any failure produces an empty ECOLocation
rather than aborting the ECO render. Falls back to assembly path + part
name when the bridge can't be reached.
"""

from __future__ import annotations

from pathlib import Path

from composer.client import ComposerClient

from schemas.eco import ECOLocation


def _extract(assembly_path: str) -> dict | None:
    try:
        with ComposerClient() as client:
            if not client.health():
                return None
            return client.extract_assembly(assembly_path)
    except Exception as exc:
        print(f"  tree_locator: extract_assembly failed ({exc}) — using fallback paths")
        return None


def build_locations(
    before_model: str,
    after_model: str,
    component_names: list[str],
) -> dict[str, ECOLocation]:
    """Return {component_name_uppercase: ECOLocation} for each requested name.

    Looks up each component in the *after* assembly first; falls back to
    *before* for removed parts. Names are matched case-insensitively on
    the file basename (e.g. ``BRACKET.SLDPRT``).
    """
    after_data = _extract(after_model) or {}
    before_data = _extract(before_model) or {}

    after_top = Path(after_model).name
    before_top = Path(before_model).name

    def _neighbors(data: dict, name_upper: str) -> list[str]:
        out: set[str] = set()
        for mate in data.get("mates", []):
            parts = [p.upper() for p in mate.get("parts", [])]
            if name_upper in parts:
                for p in parts:
                    if p and p != name_upper:
                        out.add(p)
        return sorted(out)

    def _has_component(data: dict, name_upper: str) -> bool:
        for c in data.get("components", []):
            if str(c.get("name", "")).upper() == name_upper:
                return True
        return False

    result: dict[str, ECOLocation] = {}
    for raw in component_names:
        name_upper = raw.upper()
        if _has_component(after_data, name_upper):
            tree_path = f"{after_top} > {raw}"
            neighbors = _neighbors(after_data, name_upper)
        elif _has_component(before_data, name_upper):
            tree_path = f"{before_top} > {raw}"
            neighbors = _neighbors(before_data, name_upper)
        else:
            tree_path = f"{after_top} > {raw}"
            neighbors = []
        result[name_upper] = ECOLocation(
            tree_path=tree_path,
            mate_neighbors=neighbors,
        )

    return result
