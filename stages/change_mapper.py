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
    raw_index = _build_raw_index(steps)
    affected: list[AffectedStep] = []
    seen: set[tuple[str, str]] = set()
    dropped_near_matches: list[tuple[str, str, str]] = []  # (eco_part, step_part, reason)

    for eco in ecos:
        # Strategy 1: exact normalized match
        exact_hits = set(part_to_steps.get(_normalize(eco.part_number), []))

        # Strategy 2: token-overlap fallback for names that differ by prefix/suffix
        # e.g. 'OpenLeg_Upper_Bracket-1' matches 'Upper Bracket' in steps
        fuzzy_hits: set[str] = set()
        for step_id, raw_parts in raw_index.items():
            if step_id not in exact_hits:
                for rp in raw_parts:
                    matched, reject_reason = _parts_match(eco.part_number, rp)
                    if matched:
                        fuzzy_hits.add(step_id)
                        break
                    if reject_reason is not None:
                        dropped_near_matches.append((eco.part_number, rp, reject_reason))

        for step_id in exact_hits | fuzzy_hits:
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

    # Diagnostic: summarize fuzzy-match candidates that were rejected by the
    # strict rule. If real matches are being dropped, this output helps the
    # user adjust _DISTINCTIVE_STOPWORDS or the overlap threshold below.
    if dropped_near_matches:
        from collections import Counter
        reason_counts = Counter(reason for _, _, reason in dropped_near_matches)
        print(
            f"  Change mapper: dropped {len(dropped_near_matches)} near-matches "
            f"under strict rule "
            f"({', '.join(f'{r}={n}' for r, n in reason_counts.most_common())})"
        )

    return affected


def _normalize(s: str) -> str:
    """Lowercase and strip non-alphanumeric chars for exact matching."""
    import re
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _tokenize(s: str) -> set[str]:
    """
    Split a part name/number into meaningful tokens for fuzzy overlap matching.
    Splits on underscores, hyphens, spaces, and CamelCase boundaries.
    Drops tokens shorter than 3 chars (articles, version suffixes like 'v1').
    Examples:
      'OpenLeg_Upper_Bracket-1' → {'openleg', 'upper', 'bracket'}
      'M5x10 Socket Head Cap Screw' → {'m5x10', 'socket', 'head', 'cap', 'screw'}
    """
    import re
    # Insert space before uppercase runs in CamelCase
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    # Split on non-alphanumeric
    tokens = re.split(r"[^a-zA-Z0-9]+", s.lower())
    # Drop short/numeric-only tokens (version numbers, single letters)
    return {t for t in tokens if len(t) >= 3 and not t.isdigit()}


# Tokens that are too generic to anchor a match on their own. Two-token
# overlaps consisting entirely of these words match almost any pair of part
# names (e.g. "Upper Bracket" matches "Upper Frame" via {upper}).
_DISTINCTIVE_STOPWORDS = {
    "part", "parts", "screw", "screws", "bolt", "bolts", "nut", "nuts",
    "washer", "washers", "metal", "plastic", "left", "right", "front",
    "back", "upper", "lower", "top", "bottom", "side", "inner", "outer",
    "small", "large", "long", "short", "main", "kit", "set", "assembly",
    "component", "components", "piece", "pieces",
}


def _parts_match(eco_part: str, step_part: str) -> tuple[bool, str | None]:
    """
    True if eco_part and step_part refer to the same physical part.

    Returns (matched, reject_reason). reject_reason is None when matched is
    True OR when the pair had no overlap at all (not worth logging). It is
    a short string when the pair was a near-match dropped by the strict rule
    — the caller surfaces this for diagnostics so the user can validate that
    no legitimate matches are being lost.

    Strategies:
      1. Exact normalized match — handles identical names.
      2. Token overlap with three guards (catches the 10x-amplification from
         the previous "2 shared tokens" rule):
         a) ≥2 shared tokens
         b) overlap / max(|eco|, |step|) ≥ 0.5 — measure against the LARGER
            set so that one common prefix word doesn't carry a tiny ECO name
            to a large step name.
         c) At least one shared token is distinctive (≥4 chars and not in
            _DISTINCTIVE_STOPWORDS) — prevents matches built entirely on
            generic words like "upper/lower/screw".
    """
    if _normalize(eco_part) == _normalize(step_part):
        return True, None

    eco_tokens = _tokenize(eco_part)
    step_tokens = _tokenize(step_part)
    if not eco_tokens or not step_tokens:
        return False, None

    overlap = eco_tokens & step_tokens
    if not overlap:
        return False, None

    if len(overlap) < 2:
        return False, "too_few_shared_tokens"

    coverage = len(overlap) / max(len(eco_tokens), len(step_tokens))
    if coverage < 0.5:
        return False, "low_coverage"

    if not any(len(t) >= 4 and t not in _DISTINCTIVE_STOPWORDS for t in overlap):
        return False, "only_generic_tokens"

    return True, None


def _build_index(steps: list[Step]) -> dict[str, list[str]]:
    """
    Maps normalized part_id → list of step_ids  (used for exact lookup).
    Also stores raw part_ids per step for token-overlap fallback.
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


def _build_raw_index(steps: list[Step]) -> dict[str, list[str]]:
    """Maps step_id → raw part_id strings (for token-overlap matching)."""
    result: dict[str, list[str]] = defaultdict(list)
    for step in steps:
        for part_ref in step.parts_referenced:
            result[step.step_id].append(part_ref.part_id)
    return dict(result)


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
