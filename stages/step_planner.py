"""
Stage G2 — Step Planner (Phase 4)

Maps an ordered AssemblyGraph to a list of StepPlans (~ steps in the final
manual). Most of the work is deterministic grouping; section-break naming
is the one LLM call.

Groupings:
  1. Consecutive fasteners with the same parent_components collapse into
     one step ("Install four M6 bolts to mount the bracket").
  2. Everything else gets its own step.

Section names: one LLM call asks the model to suggest top-level section
breaks given the ordered build list. Failure falls back to a single
section named after the assembly root.
"""

from __future__ import annotations

import json
import re

from llm.client import LLMClient
from schemas.generation import AssemblyGraph, AssemblyNode, StepPlan


_SECTIONING_SYSTEM = """You are sectioning an assembly build order into top-level manual sections.

You will receive an ordered JSON list of components with their roles. Output a JSON array of section boundaries:
[
  {"section": "<Section Name>", "start_build_index": <integer>}
]

Rules:
1. Sections must be in build order and cover the entire range starting from build_index 0.
2. start_build_index of the first section MUST be 0.
3. Use 1-5 sections for small assemblies (<20 components), 3-10 for larger.
4. Section names should be concise mechanical labels ("Frame", "Drive Train", "Electronics", "Final Assembly"). Avoid generic names like "Section 1".
5. Group related components — a bracket and its bolts belong together; switch sections at natural subassembly boundaries.
6. Output ONLY the JSON array — no markdown, no commentary."""


def run(graph: AssemblyGraph, llm: LLMClient) -> list[StepPlan]:
    sections = _build_sections(graph, llm)
    section_for_index = _index_to_section_map(sections, len(graph.nodes))

    # Group nodes into raw step bundles (fastener collapsing).
    bundles: list[list[AssemblyNode]] = []
    for node in graph.nodes:
        if (
            bundles
            and node.role == "fastener"
            and bundles[-1][-1].role == "fastener"
            and bundles[-1][-1].parent_components == node.parent_components
            and section_for_index[node.build_index] == section_for_index[bundles[-1][-1].build_index]
        ):
            bundles[-1].append(node)
        else:
            bundles.append([node])

    plans: list[StepPlan] = []
    section_counters: dict[str, int] = {}
    for bundle in bundles:
        section = section_for_index[bundle[0].build_index]
        slug = _slug(section)
        section_counters[slug] = section_counters.get(slug, 0) + 1
        step_number = section_counters[slug]
        components = [n.component_name for n in bundle]
        parent_components: list[str] = []
        mates_used: list[str] = []
        for n in bundle:
            for p in n.parent_components:
                if p not in parent_components:
                    parent_components.append(p)
            for m in n.mates_used:
                if m not in mates_used:
                    mates_used.append(m)

        plans.append(StepPlan(
            step_id=f"{slug}_step_{step_number:02d}",
            section=section,
            step_number=step_number,
            components=components,
            parent_components=parent_components,
            mates_used=mates_used,
            role_hint=bundle[0].role,
        ))

    return plans


def _slug(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip().lower())
    return safe.strip("_") or "section"


def _build_sections(graph: AssemblyGraph, llm: LLMClient) -> list[tuple[str, int]]:
    """Returns [(section_name, start_build_index), ...] sorted by index, starting at 0.

    Falls back to a single section named after the root on any LLM failure.
    """
    fallback = [(_section_name_from_root(graph.root), 0)]

    if len(graph.nodes) <= 4:
        return fallback

    payload = [
        {"build_index": n.build_index, "name": n.component_name, "role": n.role, "layer": n.layer}
        for n in graph.nodes
    ]
    try:
        response = llm.complete(
            messages=[{"role": "user", "content": json.dumps(payload)}],
            system=_SECTIONING_SYSTEM,
            max_tokens=4000,
        )
        if response.truncated:
            print("  Stage G2: sectioning LLM truncated, using fallback (single section)")
            return fallback
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw)
        markers = json.loads(raw)
    except Exception as e:
        print(f"  Stage G2: sectioning LLM failed ({type(e).__name__}: {e}), using fallback")
        return fallback

    parsed: list[tuple[str, int]] = []
    for m in markers:
        name = (m.get("section") or "").strip()
        try:
            idx = int(m.get("start_build_index"))
        except (TypeError, ValueError):
            continue
        if not name or idx < 0 or idx >= len(graph.nodes):
            continue
        parsed.append((name, idx))

    if not parsed:
        return fallback

    parsed.sort(key=lambda p: p[1])
    if parsed[0][1] != 0:
        # Force coverage of the first nodes
        parsed[0] = (parsed[0][0], 0)
    return parsed


def _section_name_from_root(root: str) -> str:
    base = re.sub(r"\.(SLDASM|SLDPRT)$", "", root, flags=re.IGNORECASE)
    base = base.replace("_", " ").strip()
    return base.title() or "Main Assembly"


def _index_to_section_map(sections: list[tuple[str, int]], total: int) -> list[str]:
    """Expand section boundaries into a flat list: section name per build_index."""
    result = [""] * total
    for i, (name, start) in enumerate(sections):
        end = sections[i + 1][1] if i + 1 < len(sections) else total
        for j in range(start, min(end, total)):
            result[j] = name
    # Defensive fill — should never trigger
    for j in range(total):
        if not result[j]:
            result[j] = sections[0][0]
    return result
