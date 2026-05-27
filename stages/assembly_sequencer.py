"""
Stage G1 — Assembly Sequencer (Phase 4)

Deterministic, no LLM. Turns a BoM + mate graph into an ordered list of
components: what to install in what order.

Algorithm:
  1. Build an undirected mate graph (nodes=components, edges=mates).
  2. Pick a root: heaviest component, tie-break by highest mate degree.
  3. BFS from the root. Within each layer, sort by (role_priority, -mass)
     so fasteners come last and heavier structural parts come first.
  4. Components disconnected from the root tail the end of the order, in
     the same sort, with parent_components empty.

We pick BFS over a real topological sort because real mate graphs aren't
DAGs: a bracket mated to a frame and a plate is one node touching two
already-built neighbors, not a cycle. BFS lets every non-root node attach
to "whichever earlier node it touches" without needing to break cycles.

Sequence sanity is audited downstream by eval_gate + document_evaluator
(an LLM judge). Generation here stays mechanical — hallucinated order
("attach the wheel before the axle") would be catastrophic.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque

from schemas.cad import BoM, MateEdge
from schemas.generation import AssemblyGraph, AssemblyNode, Role


_FASTENER_KEYWORDS = ("bolt", "screw", "washer", "nut", "pin", "rivet", "stud")
_STRUCTURAL_KEYWORDS = ("frame", "chassis", "bracket", "plate", "housing", "base", "rail")
_SUBASSEMBLY_KEYWORDS = ("assy", "subasm", "module", "subassembly")


_ROLE_PRIORITY: dict[Role, int] = {
    "structural": 0,
    "subassembly": 1,
    "dynamic": 2,
    "fastener": 3,
}


def run(bom: BoM) -> AssemblyGraph:
    if not bom.components:
        raise ValueError("Sequencer received an empty BoM")

    # Build adjacency. Mates with fewer than two parts named (bridge couldn't
    # resolve their MateEntity refs) become metadata-only — skipped here.
    adj: dict[str, set[tuple[str, str]]] = defaultdict(set)  # part -> {(neighbor, mate_name)}
    component_names = {c.name for c in bom.components}
    for mate in bom.mates:
        if len(mate.parts) != 2:
            continue
        a, b = mate.parts
        if a not in component_names or b not in component_names or a == b:
            continue
        adj[a].add((b, mate.name))
        adj[b].add((a, mate.name))

    mass_lookup = {c.name: (c.mass_kg if c.mass_kg is not None else 0.0) for c in bom.components}
    role_lookup = {c.name: _classify_role(c.name) for c in bom.components}

    root = _pick_root(bom, adj, mass_lookup)

    visited: set[str] = set()
    order: list[AssemblyNode] = []

    # BFS — track depth (layer) and group each layer to sort within it.
    queue: deque[tuple[str, int, list[str], list[str]]] = deque()
    queue.append((root, 0, [], []))   # (component, layer, mates_used, parents)
    visited.add(root)
    pending_by_layer: dict[int, list[tuple[str, list[str], list[str]]]] = defaultdict(list)
    pending_by_layer[0].append((root, [], []))

    next_layer_buf: list[tuple[str, list[str], list[str]]] = []
    current_layer = 0

    build_index = 0
    while pending_by_layer.get(current_layer):
        layer_items = pending_by_layer.pop(current_layer)
        layer_items.sort(key=lambda it: (_ROLE_PRIORITY[role_lookup[it[0]]], -mass_lookup[it[0]], it[0]))

        for name, mates_used, parents in layer_items:
            order.append(AssemblyNode(
                component_name=name,
                build_index=build_index,
                layer=current_layer,
                role=role_lookup[name],
                mates_used=mates_used,
                parent_components=parents,
            ))
            build_index += 1
            # Schedule children
            child_mates: dict[str, list[str]] = defaultdict(list)
            for neighbor, mate_name in adj.get(name, ()):
                if neighbor in visited:
                    continue
                child_mates[neighbor].append(mate_name)
            for child, m_names in child_mates.items():
                visited.add(child)
                pending_by_layer[current_layer + 1].append((child, m_names, [name]))

        current_layer += 1

    # Disconnected components — append at the end in role/mass order
    leftover = [c.name for c in bom.components if c.name not in visited]
    leftover.sort(key=lambda n: (_ROLE_PRIORITY[role_lookup[n]], -mass_lookup[n], n))
    for name in leftover:
        order.append(AssemblyNode(
            component_name=name,
            build_index=build_index,
            layer=-1,                          # -1 marks "disconnected from root"
            role=role_lookup[name],
            mates_used=[],
            parent_components=[],
        ))
        build_index += 1

    return AssemblyGraph(root=root, nodes=order)


def _classify_role(name: str) -> Role:
    n = name.lower()
    if any(k in n for k in _FASTENER_KEYWORDS):
        return "fastener"
    if any(k in n for k in _SUBASSEMBLY_KEYWORDS) or n.endswith(".sldasm"):
        return "subassembly"
    if any(k in n for k in _STRUCTURAL_KEYWORDS):
        return "structural"
    return "dynamic"


def _pick_root(
    bom: BoM,
    adj: dict[str, set[tuple[str, str]]],
    mass_lookup: dict[str, float],
) -> str:
    """Heaviest part wins. Tie-break: highest mate degree, then name asc.

    Mass is the right signal in practice — frames and chassis dominate
    every assembly we'll see. Degree is the fallback for assemblies of
    uniformly small parts (e.g. an electronics enclosure).
    """
    def score(name: str) -> tuple[float, int, str]:
        return (mass_lookup.get(name, 0.0), len(adj.get(name, ())), name)

    return max((c.name for c in bom.components), key=lambda n: score(n))
