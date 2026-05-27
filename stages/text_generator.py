"""
Stage G3 — Text Generator (Phase 4)

Per-step LLM call with a tool-calling loop, modeled on text_revision.
Output is `schemas.instruction.Step` — the same type the parser produces
in the revision pipeline — so doc stitcher and PDF gen work unchanged.

Tools:
  - query_mate_constraints(part_number)        existing
  - lookup_part_properties(part_name)          new (Phase 4)
  - lookup_prior_step(step_id)                 new (Phase 4)

Per-step intra-stage checkpointing matches Stage 4 — one file per plan,
loaded on resume.

Truncation policy: a `response.truncated` step gets a stub Step with
body_text noting truncation and a flag `generation_truncated`. The pipeline
continues. Engineers re-run with the same run_id to regenerate just that
plan (all others load from disk).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from llm.client import LLMClient
from schemas.cad import BoM
from schemas.generation import AssemblyGraph, StepPlan
from schemas.instruction import (
    Callout,
    PartRef,
    Spec,
    Step,
    StepImage,
)
from tools import mate_constraints as mate_tool
from tools import part_properties as props_tool
from tools import prior_step as prior_tool


_MAX_LOOP_ITERATIONS = 10
_PER_STEP_MAX_TOKENS = 4000


_SYSTEM_PROMPT = """You are a mechanical assembly instruction writer. You will write ONE step of an assembly manual from scratch, based on a structured plan.

You will receive:
- PLAN: which components to install, what they mate to (parent components), mate types in use.
- BOM_EXCERPT: properties (mass, dimensions, material) for the components in this step.
- MANIFEST: step_ids you have already generated in this run, with their headings and sections — use these for cross-references.

When you need more data, call:
- lookup_part_properties(part_name) for BoM details of a part not in the excerpt.
- query_mate_constraints(part_number) for mate details on a specific part.
- lookup_prior_step(step_id) before emitting a [[see: <step_id>]] reference, to confirm the target exists.

CRITICAL RULES (violations make the output invalid):
1. Use ONLY part names and numeric values present in BOM_EXCERPT, PLAN, or values returned by your tools. Never invent torques, lengths, counts, or part numbers.
2. If a numeric value is needed but not provided (e.g. you have a bolt with no torque), emit a Callout {"type": "warning", "text": "Torque spec to be confirmed"} AND include the string "spec_missing" in flags. Do NOT make up a value.
3. Reference prior steps with `[[see: <step_id>]]`, using only step_ids that appear in MANIFEST or that lookup_prior_step has confirmed.
4. parts_referenced must list every component in PLAN.components (source="explicit") plus any parent_component the prose references (source="inferred" if not in PLAN.components).
5. Images: emit exactly one StepImage with image_id = "<step_id>_img_1", kind = "renderable_cad", visible_parts = PLAN.components + PLAN.parent_components.

When you have enough information, output a JSON object with this exact shape:
{
  "heading": "<short imperative heading>",
  "body_text": "<full instruction prose for this step>",
  "parts_referenced": [
    {"part_id": "<name>", "qty": <int or null>, "role": "<role or null>", "source": "explicit" | "inferred"}
  ],
  "specs": [
    {"key": "<spec key>", "value": "<verbatim value>", "unit": "<unit or null>", "source_span": "<verbatim excerpt>"}
  ],
  "callouts": [{"type": "attention"|"warning"|"note", "text": "<text>"}],
  "tools_operations": ["<tool name>"],
  "flags": ["<flag string>", ...],
  "confidence": "high" | "medium" | "low"
}

Output ONLY the JSON object when done — no markdown, no extra text."""


_TOOLS = [mate_tool.schema(), props_tool.schema(), prior_tool.schema()]

_TOOL_EXECUTORS = {
    "query_mate_constraints": lambda args: mate_tool.execute(**args),
    "lookup_part_properties": lambda args: props_tool.execute(**args),
    "lookup_prior_step":      lambda args: prior_tool.execute(**args),
}


def run(
    step_plans: list[StepPlan],
    bom: BoM,
    graph: AssemblyGraph,
    llm: LLMClient,
    checkpoint_dir: str | Path | None = None,
) -> list[Step]:
    ckpt = Path(checkpoint_dir) if checkpoint_dir else None
    if ckpt is not None:
        ckpt.mkdir(parents=True, exist_ok=True)

    # Wire BoM + manifest into the tools. The manifest is mutated in-place
    # as we generate each step, so later iterations can look up earlier ones.
    props_tool.bind_bom(bom)
    manifest: dict[str, dict] = {}
    prior_tool.bind_manifest(manifest)

    bom_by_name = {c.name: c for c in bom.components}

    generated: list[Step] = []
    for i, plan in enumerate(step_plans):
        step = _load_or_generate(i, plan, bom_by_name, manifest, llm, ckpt)
        generated.append(step)
        manifest[step.step_id] = {
            "step_id": step.step_id,
            "section": step.section,
            "step_number": step.step_number,
            "heading": step.heading,
            "parts": [p.part_id for p in step.parts_referenced],
        }

    return generated


def _plan_slug(plan: StepPlan) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", plan.step_id.strip().lower()).strip("_") or "step"


def _load_or_generate(
    plan_idx: int,
    plan: StepPlan,
    bom_by_name: dict,
    manifest: dict,
    llm: LLMClient,
    ckpt: Path | None,
) -> Step:
    if ckpt is not None:
        path = ckpt / f"plan_{plan_idx:04d}_{_plan_slug(plan)}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cached = Step.model_validate(data)
                print(f"  Stage G3: plan {plan_idx + 1} ({plan.step_id}) loaded from checkpoint")
                return cached
            except (json.JSONDecodeError, ValidationError) as e:
                print(f"  Stage G3: plan {plan_idx + 1} cache rejected ({type(e).__name__}), re-running")
                path.unlink(missing_ok=True)

    step = _run_generation_loop(plan, bom_by_name, manifest, llm)

    if ckpt is not None:
        path = ckpt / f"plan_{plan_idx:04d}_{_plan_slug(plan)}.json"
        path.write_text(json.dumps(step.model_dump(), ensure_ascii=False), encoding="utf-8")
        print(f"  Stage G3: plan {plan_idx + 1} ({plan.step_id}) generated + checkpointed")
    return step


def _run_generation_loop(
    plan: StepPlan,
    bom_by_name: dict,
    manifest: dict,
    llm: LLMClient,
) -> Step:
    user_content = _build_initial_message(plan, bom_by_name, manifest)
    messages = [{"role": "user", "content": user_content}]

    for _ in range(_MAX_LOOP_ITERATIONS):
        response = llm.complete(
            messages=messages,
            system=_SYSTEM_PROMPT,
            tools=_TOOLS,
            max_tokens=_PER_STEP_MAX_TOKENS,
        )

        if response.has_tool_calls:
            messages.append({
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": response.tool_calls,
            })
            for tc in response.tool_calls:
                try:
                    tool_args = json.loads(tc["arguments"])
                except (json.JSONDecodeError, TypeError):
                    tool_args = {}
                executor = _TOOL_EXECUTORS.get(tc["name"])
                tool_result = executor(tool_args) if executor else {"error": f"unknown tool {tc['name']!r}"}
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_result),
                })
            continue

        if response.truncated:
            # See module docstring — truncation becomes a stub step with a
            # flag, so the engineer can re-run just this plan.
            return _truncated_stub(plan)

        parsed = _parse_generation_output(response.content)
        if parsed:
            try:
                return _assemble_step(plan, parsed)
            except ValidationError as e:
                print(f"  Step {plan.step_id}: generation rejected by schema ({e.error_count()} error(s)), re-prompting")
                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": "Your JSON failed schema validation. Re-emit a corrected JSON object only.",
                })
                continue

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "Please output the final JSON object now."})

    # Max iterations exceeded — emit a low-confidence stub so the pipeline
    # continues. eval_gate will surface it for engineer review.
    return _max_iterations_stub(plan)


def _build_initial_message(plan: StepPlan, bom_by_name: dict, manifest: dict) -> str:
    bom_excerpt = []
    relevant = set(plan.components) | set(plan.parent_components)
    for name in relevant:
        comp = bom_by_name.get(name)
        if comp is None:
            continue
        bom_excerpt.append({
            "name": comp.name,
            "quantity": comp.quantity,
            "mass_kg": comp.mass_kg,
            "properties": comp.properties,
            "dimensions_mm": comp.dimensions,
        })

    manifest_excerpt = [
        {"step_id": v["step_id"], "section": v["section"], "heading": v["heading"]}
        for v in manifest.values()
    ]

    payload = {
        "PLAN": {
            "step_id": plan.step_id,
            "section": plan.section,
            "step_number": plan.step_number,
            "components": plan.components,
            "parent_components": plan.parent_components,
            "mates_used": plan.mates_used,
            "role_hint": plan.role_hint,
        },
        "BOM_EXCERPT": bom_excerpt,
        "MANIFEST": manifest_excerpt,
    }
    return json.dumps(payload, indent=2)


_JSON_FENCE = re.compile(r"^```[a-z]*\n?|\n?```$", re.MULTILINE)


def _parse_generation_output(content: str) -> dict | None:
    raw = content.strip()
    if raw.startswith("```"):
        raw = _JSON_FENCE.sub("", raw).strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(result, dict) or not result.get("body_text"):
        return None
    return result


def _assemble_step(plan: StepPlan, parsed: dict) -> Step:
    parts = [PartRef.model_validate(p) for p in parsed.get("parts_referenced", [])]
    specs = [Spec.model_validate(s) for s in parsed.get("specs", [])]
    callouts = [Callout.model_validate(c) for c in parsed.get("callouts", [])]
    image = StepImage(
        image_id=f"{plan.step_id}_img_1",
        kind="renderable_cad",
        caption=None,
        camera_hint=None,
        visible_parts=list(dict.fromkeys(plan.components + plan.parent_components)),
    )
    return Step(
        step_id=plan.step_id,
        section=plan.section,
        step_number=plan.step_number,
        heading=parsed.get("heading", "").strip() or _default_heading(plan),
        body_text=parsed["body_text"].strip(),
        parts_referenced=parts,
        specs=specs,
        callouts=callouts,
        images=[image],
        tools_operations=list(parsed.get("tools_operations", [])),
    )


def _default_heading(plan: StepPlan) -> str:
    return f"Step {plan.step_number}: Install {plan.components[0]}"


def _truncated_stub(plan: StepPlan) -> Step:
    return Step(
        step_id=plan.step_id,
        section=plan.section,
        step_number=plan.step_number,
        heading=_default_heading(plan),
        body_text="[generation truncated — re-run this step to regenerate]",
        parts_referenced=[
            PartRef(part_id=c, qty=None, role=None, source="explicit") for c in plan.components
        ],
        specs=[],
        callouts=[Callout(type="warning", text="Auto-generation truncated; engineer must review.")],
        images=[StepImage(
            image_id=f"{plan.step_id}_img_1",
            kind="renderable_cad",
            visible_parts=list(dict.fromkeys(plan.components + plan.parent_components)),
        )],
        tools_operations=[],
    )


def _max_iterations_stub(plan: StepPlan) -> Step:
    return Step(
        step_id=plan.step_id,
        section=plan.section,
        step_number=plan.step_number,
        heading=_default_heading(plan),
        body_text=f"[generation hit max iterations — install {', '.join(plan.components)}]",
        parts_referenced=[
            PartRef(part_id=c, qty=None, role=None, source="explicit") for c in plan.components
        ],
        specs=[],
        callouts=[Callout(type="warning", text="Auto-generation incomplete; engineer must review.")],
        images=[StepImage(
            image_id=f"{plan.step_id}_img_1",
            kind="renderable_cad",
            visible_parts=list(dict.fromkeys(plan.components + plan.parent_components)),
        )],
        tools_operations=[],
    )
