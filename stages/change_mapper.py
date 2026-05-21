"""
Stage 2 — Change Mapper

Pure deterministic Python — no LLM. Builds an inverted index from
part_number → [step_ids] and returns AffectedStep records for each
ECO, classified as direct or indirect impact.

Direct:  the step explicitly references or infers the changed part_number.
Indirect: the step references a part whose mate constraints involve the
          changed part. Requires the mate graph from Composer or prior
          context — falls back to "direct only" if unavailable.
"""

from collections import defaultdict

from schemas.eco import ECO
from schemas.instruction import Step
from schemas.pipeline_state import AffectedStep


def run(
    steps: list[Step],
    ecos: list[ECO],
    mate_graph: dict[str, list[str]] | None = None,
) -> list[AffectedStep]:
    """
    mate_graph: optional mapping of part_number → [mated_part_numbers]
                If provided, enables indirect impact detection.
    """
    part_to_steps = _build_index(steps)
    affected: list[AffectedStep] = []
    seen: set[tuple[str, str]] = set()

    for eco in ecos:
        # Direct impact — step mentions the changed part
        for step_id in part_to_steps.get(_normalize(eco.part_number), []):
            key = (step_id, eco.eco_id)
            if key not in seen:
                seen.add(key)
                affected.append(
                    AffectedStep(
                        step_id=step_id,
                        eco_id=eco.eco_id,
                        change_types=_classify_changes(eco),
                        impact="direct",
                    )
                )

        # Indirect impact — step mentions a part that mates with the changed part
        if mate_graph:
            mated_parts = mate_graph.get(eco.part_number, [])
            for mated_part in mated_parts:
                for step_id in part_to_steps.get(_normalize(mated_part), []):
                    key = (step_id, eco.eco_id)
                    if key not in seen:
                        seen.add(key)
                        affected.append(
                            AffectedStep(
                                step_id=step_id,
                                eco_id=eco.eco_id,
                                change_types=_classify_changes(eco),
                                impact="indirect",
                            )
                        )

    return affected


def _normalize(s: str) -> str:
    """Lowercase and strip non-alphanumeric chars for fuzzy matching."""
    import re
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _build_index(steps: list[Step]) -> dict[str, list[str]]:
    """
    Maps normalized part_id → list of step_ids.
    Normalization strips spaces/underscores/hyphens so that 'HexBearing',
    'hex bearing', and 'hex_bearing' all map to the same key.
    """
    index: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        for part_ref in step.parts_referenced:
            key = _normalize(part_ref.part_id)
            if key:
                index[key].append(step.step_id)
        for spec in step.specs:
            if _looks_like_part_number(spec.value):
                index[_normalize(spec.value)].append(step.step_id)
    return dict(index)


def _classify_changes(eco: ECO) -> list[str]:
    """Infer change type from ECO field names."""
    types: set[str] = set()
    dimension_keys = {"length", "width", "height", "diameter", "radius", "depth", "gap", "mm", "inch"}
    torque_keys = {"torque", "nm", "ft_lb", "in_lb"}
    mate_keys = {"mate", "constraint", "concentric", "coincident", "parallel", "perpendicular"}
    material_keys = {"material", "alloy", "grade", "finish", "coating"}

    for change in eco.changes:
        field_lower = change.field.lower()
        if any(k in field_lower for k in dimension_keys):
            types.add("dimension")
        elif any(k in field_lower for k in torque_keys):
            types.add("torque_spec")
        elif any(k in field_lower for k in mate_keys):
            types.add("mate_constraint")
        elif any(k in field_lower for k in material_keys):
            types.add("material")
        else:
            types.add("property")

    return sorted(types) or ["property"]


def _looks_like_part_number(value: str) -> bool:
    """Heuristic: part numbers are typically alphanumeric with dashes/underscores."""
    import re
    return bool(re.match(r"^[A-Z0-9][A-Z0-9\-_]{2,}$", value.strip().upper()))
