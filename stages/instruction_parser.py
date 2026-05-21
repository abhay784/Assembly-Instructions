"""
Stage 1 — Instruction Parser

Single LLM call that converts raw instruction document text into structured
List[Step] JSON. The prompt is format-agnostic — handles both prose-style
documents (hobbyist, OpenLeg-style) and structured/tabular documents
(aerospace MIL-spec, ISO work instructions).

Key constraints enforced in the system prompt:
  - Specs extracted verbatim from source text (no paraphrasing values)
  - Part references tagged as explicit (named in text) vs inferred (implied)
  - Images tagged as renderable_cad vs reference_photo
  - step_id derived from section slug + step number (stable across runs)
"""

import json
import re

from llm.client import LLMClient
from schemas.instruction import Step

_SYSTEM_PROMPT = """You are a mechanical assembly instruction parser. Convert the provided raw assembly instruction text into structured JSON.

Output a JSON array of step objects. Each step must follow this exact schema:

{
  "step_id": "<section_slug>_step_<NN>",        // e.g. "upper_leg_step_01"
  "section": "<Section Name>",                   // e.g. "Upper Leg Assembly"
  "step_number": <integer>,
  "heading": "<step heading text>",
  "body_text": "<full instruction prose for this step>",
  "parts_referenced": [
    {
      "part_id": "<part name or number>",
      "qty": <integer or null>,
      "role": "<what the part does in this step, or null>",
      "source": "explicit"                        // "explicit" if named, "inferred" if implied
    }
  ],
  "specs": [
    {
      "key": "<spec name>",                       // e.g. "gap_mm", "torque_nm", "wire_length_mm"
      "value": "<exact value as written>",        // copy verbatim — do NOT paraphrase numbers
      "unit": "<unit string or null>",
      "source_span": "<verbatim excerpt from body_text containing this spec>"
    }
  ],
  "callouts": [
    {
      "type": "attention" | "warning" | "note",
      "text": "<callout text>"
    }
  ],
  "images": [
    {
      "image_id": "<section_slug>_step_<NN>_img_<M>",
      "kind": "renderable_cad" | "reference_photo",  // CAD render = renderable_cad; real photo = reference_photo
      "caption": "<caption text or null>",
      "camera_hint": "<angle description if discernible, e.g. 'front isometric', or null>",
      "visible_parts": ["<part names visible in this image>"]
    }
  ],
  "tools_operations": ["<tool or operation name>"]  // e.g. "solder", "M3 hex key", "super glue"
}

Rules:
1. NEVER invent, round, or paraphrase numeric values. Copy specs exactly as written.
2. A part_reference is "explicit" if the step text names it directly. "inferred" if it's clearly required but not named.
3. An image is "renderable_cad" if it appears to be a 3D CAD render or technical illustration. "reference_photo" if it's a real photograph.
4. section_slug = section name lowercased with spaces replaced by underscores, e.g. "upper_leg_assembly"
5. step_id must be stable: same section + step number always produces the same step_id.
6. If a step has no specs, parts, callouts, images, or tools — use empty arrays, not null.
7. Output ONLY valid JSON. No markdown fences, no explanation text."""


def run(raw_text: str, llm: LLMClient) -> list[Step]:
    response = llm.complete(
        messages=[{"role": "user", "content": raw_text}],
        system=_SYSTEM_PROMPT,
        max_tokens=32000,
    )

    parsed = _parse_json_response(response.content)
    steps = [Step.model_validate(item) for item in parsed]
    return steps


def _parse_json_response(content: str) -> list[dict]:
    content = content.strip()

    # Strip markdown fences if the model added them despite instructions
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)

    try:
        result = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Instruction parser returned invalid JSON: {e}\n\nRaw content:\n{content[:500]}")

    if not isinstance(result, list):
        raise ValueError(f"Expected JSON array, got {type(result).__name__}")

    return result
