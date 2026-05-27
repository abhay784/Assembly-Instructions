"""
Stage 6 — Eval Gate

Fully annotating — nothing blocks. All failures add EvalFlag entries and
flow through to the PDF with visible warning banners.

Three checks:
1. Spec allowlist (deterministic): every numeric value + part number in
   revised text must appear in the ECO changes or original step text.
   Violations → flag: spec_unverified

2. Assembly logic judge (LLM): does the revised/new step make mechanical
   sense given ECO context and surrounding steps?
   Failures → flag: assembly_logic_uncertain

3. Image check (vision LLM): for re-rendered images, do expected parts
   appear clearly visible?
   Failures → flag: image_quality
"""

import base64
import json
import os
import re

from llm.client import LLMClient
from schemas.eco import ECO
from schemas.instruction import Step
from schemas.pipeline_state import (
    EvalFlag,
    EvaluatedStep,
    RenderedImage,
    RevisedStep,
)

_LOGIC_JUDGE_SYSTEM = """You are a mechanical assembly instruction reviewer. Evaluate whether a revised assembly step is mechanically correct and makes sense given the engineering change order (ECO).

Check for:
1. Does the step clearly explain how the changed part connects to its mates?
2. Are the assembly sequence and prerequisite steps implied correctly?
3. Are there any physically impossible operations described?
4. For new steps: is the installation procedure plausible given the part type?

Respond with JSON:
{
  "passes": true | false,
  "issues": ["<issue description>", ...]
}

Output ONLY valid JSON."""

_IMAGE_JUDGE_SYSTEM = """You are a technical illustration reviewer for assembly instructions. Evaluate whether the provided CAD render clearly shows the parts that should be visible for this assembly step.

Check:
1. Is the primary part being installed clearly visible and identifiable?
2. Are mating parts visible for context?
3. Is the camera angle clear and useful for assembly guidance?

Respond with JSON:
{
  "passes": true | false,
  "issues": ["<issue description>", ...]
}

Output ONLY valid JSON."""


def run(
    revised_steps: list[RevisedStep],
    rendered_images: list[RenderedImage],
    ecos: list[ECO],
    original_steps: list[Step],
    llm: LLMClient,
) -> list[EvaluatedStep]:
    eco_map = {eco.eco_id: eco for eco in ecos}
    original_map = {s.step_id: s for s in original_steps}
    image_map = {img.step_id: img for img in rendered_images}

    results: list[EvaluatedStep] = []
    for revised in revised_steps:
        eco = eco_map.get(revised.revision_source)
        original = original_map.get(revised.step_id)
        image = image_map.get(revised.step_id)

        flags: list[EvalFlag] = []

        # Check 1 — spec allowlist (deterministic)
        flags += _check_spec_allowlist(revised, eco, original)

        # Check 2 — assembly logic (LLM judge)
        if eco and (revised.is_new_step or revised.confidence in ("low", "medium")):
            flags += _check_assembly_logic(revised, eco, llm)

        # Check 3 — image quality (vision LLM)
        if image and not image.skipped and image.new_path:
            flags += _check_image_quality(revised, image, llm)

        results.append(EvaluatedStep(
            revised_step=revised,
            rendered_image=image,
            eval_flags=flags,
        ))

    return results


def _check_spec_allowlist(
    revised: RevisedStep,
    eco: ECO | None,
    original: Step | None,
) -> list[EvalFlag]:
    allowed_values: set[str] = set()

    if eco:
        for change in eco.changes:
            allowed_values.add(change.old.strip().lower())
            allowed_values.add(change.new.strip().lower())
        allowed_values.add(eco.part_number.strip().lower())

    if original:
        allowed_values.add(original.body_text.lower())
        for spec in original.specs:
            allowed_values.add(spec.value.strip().lower())
        for part in original.parts_referenced:
            allowed_values.add(part.part_id.strip().lower())

    # Extract numeric values and part-number-like tokens from revised text
    revised_text = revised.revised_body_text
    suspicious: list[str] = []

    numeric_pattern = re.compile(r"\b\d+(?:\.\d+)?(?:\s*(?:mm|nm|kg|in|ft|lb|°|V|A|rpm))?\b")
    for match in numeric_pattern.finditer(revised_text):
        val = match.group().strip().lower()
        if not _value_in_allowed(val, allowed_values):
            suspicious.append(val)

    flags: list[EvalFlag] = []
    if suspicious:
        flags.append(EvalFlag(
            flag_type="spec_unverified",
            detail=f"Values not found in ECO or original step: {', '.join(set(suspicious))}",
            severity="warning",
        ))
    return flags


def _value_in_allowed(val: str, allowed: set[str]) -> bool:
    """Check if a value appears anywhere in the allowed set."""
    for allowed_val in allowed:
        if val in allowed_val:
            return True
    return False


def _check_assembly_logic(
    revised: RevisedStep,
    eco: ECO,
    llm: LLMClient,
) -> list[EvalFlag]:
    user_content = json.dumps({
        "eco_summary": eco.summary,
        "eco_changes": [c.model_dump() for c in eco.changes],
        "revised_step_text": revised.revised_body_text,
        "is_new_step": revised.is_new_step,
        "agent_flags": revised.flags,
    })

    # Judge output is small (passes + 1-3 issue strings). 4K is generous;
    # bumped from the 8K default only to leave room for verbose reasoning.
    response = llm.complete(
        messages=[{"role": "user", "content": user_content}],
        system=_LOGIC_JUDGE_SYSTEM,
        max_tokens=4096,
    )

    if response.truncated:
        # Eval gate annotates rather than blocks — surface the incomplete
        # judgment as a flag instead of crashing the run.
        return [EvalFlag(
            flag_type="assembly_logic_uncertain",
            detail=f"Logic judge response truncated at {len(response.content)} chars — review manually",
            severity="warning",
        )]

    result = _parse_judge_response(response.content)
    if result and not result.get("passes", True):
        issues = result.get("issues", [])
        return [EvalFlag(
            flag_type="assembly_logic_uncertain",
            detail="; ".join(issues) if issues else "Assembly logic review failed",
            severity="warning",
        )]
    return []


def _check_image_quality(
    revised: RevisedStep,
    image: RenderedImage,
    llm: LLMClient,
) -> list[EvalFlag]:
    if not os.path.exists(image.new_path):
        return [EvalFlag(
            flag_type="image_quality",
            detail="Rendered image file not found on disk",
            severity="warning",
        )]

    try:
        with open(image.new_path, "rb") as f:
            image_b64 = base64.standard_b64encode(f.read()).decode()
    except OSError:
        return [EvalFlag(
            flag_type="image_quality",
            detail="Could not read rendered image file",
            severity="info",
        )]

    user_message = {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": f"Step: {revised.revised_body_text[:300]}\n\nEvaluate this assembly step image:",
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
            },
        ],
    }

    response = llm.complete(
        messages=[user_message],
        system=_IMAGE_JUDGE_SYSTEM,
        max_tokens=4096,
    )

    if response.truncated:
        return [EvalFlag(
            flag_type="image_quality",
            detail=f"Image judge response truncated at {len(response.content)} chars — review manually",
            severity="info",
        )]

    result = _parse_judge_response(response.content)
    if result and not result.get("passes", True):
        issues = result.get("issues", [])
        return [EvalFlag(
            flag_type="image_quality",
            detail="; ".join(issues) if issues else "Image quality review failed",
            severity="info",
        )]
    return []


def _parse_judge_response(content: str) -> dict | None:
    content = content.strip()
    content = re.sub(r"^```[a-z]*\n?", "", content)
    content = re.sub(r"\n?```$", "", content)
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
