"""
Stage 4 — Text Revision Agent

The only true agentic loop in the pipeline. For each affected step, runs a
tool-calling loop that rewrites the step prose using only values sourced from
the ECO or the original step. Never invents specs.

Loop exit condition: model returns a response with no tool calls and valid
JSON in the content field (the revised step object).

Tools available:
  - lookup_part_history(part_number)
  - query_mate_constraints(part_number)
"""

import json
import re

from llm.client import LLMClient
from schemas.eco import ECO
from schemas.instruction import Step
from schemas.pipeline_state import ActionPlan, RevisedStep
from tools import mate_constraints as mate_tool
from tools import part_history as history_tool

_MAX_LOOP_ITERATIONS = 10

_SYSTEM_PROMPT = """You are a mechanical assembly instruction writer. Your job is to revise an assembly step based on an engineering change order (ECO).

CRITICAL RULES — violations make the output invalid:
1. Every numeric value (dimensions, torques, counts, distances) in your revised text MUST appear verbatim in either the ECO changes or the original step text. Never invent or estimate values.
2. Every part number in your revised text MUST appear verbatim in either the ECO or the original step. Never invent part numbers.
3. If you are uncertain about how a new part connects to the assembly, use the query_mate_constraints tool before writing.
4. If you need the history of a part to understand the change context, use lookup_part_history.

STYLE RULES — apply these when revising an existing step:
5. Match the exact terminology of the original (e.g. if the original says "fastener", never substitute "screw" or "bolt").
6. Match the sentence structure and voice of the original (imperative vs. passive, level of detail per action).
7. Match all formatting conventions exactly: warning labels (e.g. "ATTENTION:", "CAUTION:"), unit notation (e.g. "8 Nm" not "8Nm"), and list punctuation.
8. Do not add, remove, or reorder prose elements that the ECO does not explicitly change.

When you have enough information, output a JSON object with this exact structure:
{
  "revised_body_text": "<the complete revised instruction text>",
  "confidence": "high" | "medium" | "low",
  "flags": ["<flag string>", ...],
  "reasoning": "<one sentence explaining your confidence level>"
}

Confidence levels:
- "high": all values sourced directly from ECO, assembly logic clear
- "medium": most values sourced from ECO; minor inferences needed for prose flow
- "low": new step with uncertain assembly sequence, or mate logic unclear

Common flags: "assembly_logic_uncertain", "torque_spec_unverified", "part_number_inferred", "sequence_position_uncertain"

Output ONLY the JSON object when done — no markdown, no extra text."""


_TOOLS = [history_tool.schema(), mate_tool.schema()]

_TOOL_EXECUTORS = {
    "lookup_part_history": lambda args: history_tool.execute(**args),
    "query_mate_constraints": lambda args: mate_tool.execute(**args),
}


def run(
    action_plans: list[ActionPlan],
    steps: list[Step],
    ecos: list[ECO],
    llm: LLMClient,
) -> list[RevisedStep]:
    eco_map = {eco.eco_id: eco for eco in ecos}
    step_map = {s.step_id: s for s in steps}

    revised: list[RevisedStep] = []
    for plan in action_plans:
        if plan.action == "no_change":
            original = step_map[plan.step_id]
            revised.append(
                RevisedStep(
                    step_id=plan.step_id,
                    original_step=original,
                    revised_body_text=original.body_text,
                    confidence="high",
                    revision_source=plan.eco_id,
                )
            )
            continue

        eco = eco_map[plan.eco_id]
        original_step = step_map.get(plan.step_id)
        result = _run_revision_loop(plan, eco, original_step, llm)
        revised.append(result)

    return revised


def _run_revision_loop(
    plan: ActionPlan,
    eco: ECO,
    original_step: Step | None,
    llm: LLMClient,
) -> RevisedStep:
    is_new = plan.action == "add_step_flagged"

    user_content = _build_initial_message(plan, eco, original_step, is_new)
    messages = [{"role": "user", "content": user_content}]

    for _ in range(_MAX_LOOP_ITERATIONS):
        response = llm.complete(messages=messages, system=_SYSTEM_PROMPT, tools=_TOOLS)

        if response.has_tool_calls:
            messages.append({"role": "assistant", "content": response.content or "", "tool_calls": response.tool_calls})
            for tc in response.tool_calls:
                tool_args = json.loads(tc["arguments"])
                tool_result = _TOOL_EXECUTORS[tc["name"]](tool_args)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_result),
                })
            continue

        # No tool calls — expect final JSON output
        parsed = _parse_revision_output(response.content)
        if parsed:
            placeholder_step = original_step or _make_placeholder_step(plan.step_id)
            return RevisedStep(
                step_id=plan.step_id,
                original_step=placeholder_step,
                revised_body_text=parsed["revised_body_text"],
                confidence=parsed.get("confidence", "low"),
                flags=parsed.get("flags", []),
                revision_source=eco.eco_id,
                needs_manual_view=plan.needs_manual_view,
                is_new_step=is_new,
            )

        # Model returned non-JSON — prompt it to produce the output format
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": "Please output your answer as the JSON object now."})

    # Fallback: return original text with low confidence after max iterations
    placeholder_step = original_step or _make_placeholder_step(plan.step_id)
    return RevisedStep(
        step_id=plan.step_id,
        original_step=placeholder_step,
        revised_body_text=original_step.body_text if original_step else "",
        confidence="low",
        flags=["max_iterations_exceeded"],
        revision_source=eco.eco_id,
        needs_manual_view=plan.needs_manual_view,
        is_new_step=is_new,
    )


def _build_initial_message(
    plan: ActionPlan,
    eco: ECO,
    original_step: Step | None,
    is_new: bool,
) -> str:
    parts = [
        f"ECO ID: {eco.eco_id}",
        f"Part Number: {eco.part_number}",
        f"ECO Summary: {eco.summary}",
        "Changes:",
    ]
    for change in eco.changes:
        parts.append(f"  - {change.field}: {change.old!r} → {change.new!r}")

    if is_new:
        parts += [
            "",
            "Task: This is a NEW part being added to the assembly.",
            "Write a new assembly step explaining how to install this part.",
            "Use query_mate_constraints to understand how it connects before writing.",
            "If the assembly sequence is unclear, set confidence to 'low' and flag 'assembly_logic_uncertain'.",
        ]
    elif original_step:
        parts += [
            "",
            f"Original Step: {original_step.heading}",
            f"Original Text:\n{original_step.body_text}",
            "",
            "Style analysis — before writing, note from the original text above:",
            "  - Terminology: exact nouns used for parts and actions (do not substitute synonyms)",
            "  - Voice: imperative vs. passive; level of detail per action",
            "  - Formatting: warning label prefixes, unit notation style, list punctuation",
            "",
            "Task: Revise the step text to reflect the ECO changes above.",
            "Keep all unchanged information intact. Only update what the ECO modifies.",
            "Your revised text must be indistinguishable in style from the original.",
        ]

    return "\n".join(parts)


def _parse_revision_output(content: str) -> dict | None:
    content = content.strip()
    content = re.sub(r"^```[a-z]*\n?", "", content)
    content = re.sub(r"\n?```$", "", content)
    try:
        result = json.loads(content)
        if "revised_body_text" in result:
            return result
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def _make_placeholder_step(step_id: str) -> Step:
    from schemas.instruction import Step as S
    return S(
        step_id=step_id,
        section="",
        step_number=0,
        heading=step_id,
        body_text="",
    )
