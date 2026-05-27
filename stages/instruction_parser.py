"""
Stage 1 — Instruction Parser (two-pass, chunked)

Long assembly documents (100+ pages) overflow a single LLM call's output
token budget and arrive truncated. This stage splits the work in two:

  Pass 1 — Section index. One LLM call returns top-level section headers
           plus a verbatim anchor string for each. We locate each anchor
           in the raw text via substring search and deterministically
           slice the document into per-section chunks.

  Pass 2 — Per-section parse. For each chunk we call the LLM with the
           section's raw text plus a running manifest carrying forward
           context from prior sections (step IDs already assigned, parts
           already introduced, tools in use). After each call we append
           the new section's contributions to the manifest before the
           next call runs.

Cross-step references in body_text are emitted as inline pointers of the
form `[[see: <step_id>]]`. Downstream stages already build
`step_map = {s.step_id: s for s in steps}` and can resolve pointers via
that lookup without inlining the referenced step's text.
"""

import json
import re
from pathlib import Path

from llm.client import LLMClient
from schemas.instruction import Step


_SECTIONING_PROMPT = """You are sectioning an assembly instruction document into its major top-level sections.

Output a JSON array of section markers, one per major section:
[
  {
    "section": "<Section Name>",
    "anchor": "<verbatim first ~80 characters of the section, copied exactly from the document>"
  }
]

Rules:
1. "anchor" MUST be a character-for-character substring of the source — including capitalization, punctuation, and whitespace. It is used as a locator via exact substring search.
2. Identify only top-level sections (e.g. "Upper Leg Assembly", "Knee Joint", "Final Assembly"). Do NOT list sub-sections or individual steps.
3. The anchor should start at the very beginning of the section (typically the section heading plus a few following words to make it unique within the document).
4. If the document has no clear section structure, return a single entry covering the whole document with anchor = first ~80 chars of the document.
5. List sections in document order.
6. Output ONLY the JSON array — no markdown fences, no commentary."""


_PARSE_SECTION_PROMPT = """You are a mechanical assembly instruction parser. Convert the provided section's raw text into structured JSON steps.

You will receive:
- SECTION: the section header for the chunk you are parsing.
- MANIFEST: JSON context from prior sections — step IDs already assigned, parts already introduced, tools in use, sections completed.
- SECTION RAW TEXT: the prose for THIS section only.

Output a JSON array of step objects with this exact schema:
{
  "step_id": "<section_slug>_step_<NN>",
  "section": "<Section Name>",
  "step_number": <integer>,
  "heading": "<step heading text>",
  "body_text": "<full instruction prose for this step>",
  "parts_referenced": [
    {"part_id": "<part name>", "qty": <integer or null>, "role": "<role or null>", "source": "explicit" | "inferred"}
  ],
  "specs": [
    {"key": "<spec key>", "value": "<verbatim value>", "unit": "<unit or null>", "source_span": "<verbatim excerpt>"}
  ],
  "callouts": [
    {"type": "attention" | "warning" | "note", "text": "<text>"}
  ],
  "images": [
    {"image_id": "<section_slug>_step_<NN>_img_<M>", "kind": "renderable_cad" | "reference_photo", "caption": "<or null>", "camera_hint": "<or null>", "visible_parts": ["<part>"]}
  ],
  "tools_operations": ["<tool>"]
}

Rules:
1. NEVER invent, round, or paraphrase numeric values. Copy specs exactly as written.
2. A part_reference is "explicit" if named directly in the text; "inferred" if clearly required but not named.
3. An image is "renderable_cad" if it is a 3D CAD render or technical illustration; "reference_photo" if it is a real photograph.
4. section_slug = section name lowercased with spaces replaced by underscores (e.g. "upper_leg_assembly").
5. step_id is stable: same section + step number always produces the same step_id.
6. Use empty arrays (not null) when a step has no specs / parts / callouts / images / tools.

Cross-step references (IMPORTANT):
7. When body_text refers to a step from a PRIOR section (e.g. "as in step 3", "the bracket installed earlier", "repeat the procedure from the knee assembly"), emit an inline pointer `[[see: <step_id>]]` immediately after the reference. Look up the correct step_id in MANIFEST.step_id_index. If you cannot confidently identify the referenced step, leave the original text unchanged — do NOT guess a step_id.
8. Within the SAME section, you may reference earlier steps the same way using the step_id you will assign them.
9. When a part appears in MANIFEST.parts_introduced, reuse the existing part_id verbatim to keep references consistent across sections.

10. Output ONLY a JSON array — no markdown fences, no commentary.

WORKED EXAMPLE — what a correctly-parsed step looks like:

Input fragment:
  "Step 3 — Install Y-axis motor

  Attention: align the motor shaft with the pulley before tightening.

  Take the Y-motor (1x) and fasten it to the frame using two M3x10 screws.
  Torque: 0.4 Nm. Make sure the cable points to the rear.
  See step 1 [[see: 2a._y-axis_assembly_step_01]] for the cable routing.
  [photo: motor_mounting.jpg]"

Correct output object:
{
  "step_id": "2a._y-axis_assembly_step_03",
  "section": "2A. Y-axis Assembly",
  "step_number": 3,
  "heading": "Install Y-axis motor",
  "body_text": "Attention: align the motor shaft with the pulley before tightening. Take the Y-motor (1x) and fasten it to the frame using two M3x10 screws. Torque: 0.4 Nm. Make sure the cable points to the rear. See step 1 [[see: 2a._y-axis_assembly_step_01]] for the cable routing.",
  "parts_referenced": [
    {"part_id": "Y-motor", "qty": 1, "role": "drive motor for Y-axis", "source": "explicit"},
    {"part_id": "M3x10 screw", "qty": 2, "role": "fastens motor to frame", "source": "explicit"}
  ],
  "specs": [
    {"key": "torque_nm", "value": "0.4", "unit": "Nm", "source_span": "Torque: 0.4 Nm"}
  ],
  "callouts": [
    {"type": "attention", "text": "align the motor shaft with the pulley before tightening"}
  ],
  "images": [
    {"image_id": "2a._y-axis_assembly_step_03_img_1", "kind": "reference_photo", "caption": "motor_mounting.jpg", "camera_hint": null, "visible_parts": ["Y-motor", "frame"]}
  ],
  "tools_operations": ["torque wrench"]
}

COMMON MISTAKES — avoid these specifically:

A. **Paraphrasing numeric values.** If the source says "0.4 Nm", output "0.4" in `value`, not "0.4 Nm" or "less than 0.5 Nm" or "approximately 0.4". The `unit` field is separate.

B. **Missing source_span.** Every spec MUST include `source_span` containing the verbatim excerpt the value came from. Without it, downstream validators can't audit the extraction. If you can't find a source_span, the spec is likely hallucinated — drop it.

C. **Guessing step_ids for cross-references.** If you see "see step 3" but MANIFEST.step_id_index doesn't contain a matching entry from the current or prior sections, leave the text alone. A wrong pointer is worse than no pointer.

D. **Using null where empty arrays are required.** Rule 6: `parts_referenced: []`, NOT `parts_referenced: null`. Same for specs, callouts, images, tools_operations.

E. **Confusing renderable_cad with reference_photo.** A 3D CAD render or technical line drawing → `renderable_cad`. A real-world photograph (visible focus blur, real lighting, real screwdriver in someone's hand) → `reference_photo`. When in doubt, prefer `reference_photo`.

F. **Inventing parts.** If the prose talks about "the bracket" without ever naming it, look at MANIFEST.parts_introduced for a recent bracket. If none exists, mark `source: "inferred"` and use a descriptive label like "Upper Bracket" — do NOT invent a SKU/part number that wasn't in the source.

G. **HTML tags inside body_text.** `body_text` is plain prose. NEVER emit `<br>`, `<br/>`, `<p>`, `<strong>`, `<em>`, or any other HTML tag. For paragraph breaks inside a step, use a JSON-escaped newline (`\\n`) — the downstream renderer converts those to `<br>` on its own. Emitting literal `<br>` produces broken output in the final PDF and the review queue."""


def run(
    raw_text: str,
    llm: LLMClient,
    checkpoint_dir: str | Path | None = None,
) -> list[Step]:
    """Parse a raw instruction document into a list of Steps.

    When ``checkpoint_dir`` is provided, intermediate per-section results
    are written to disk so a network failure mid-stage doesn't throw away
    work done for prior sections. On resume, cached sections are loaded
    and only missing ones are sent to the LLM. The cache for one section
    looks like ``<checkpoint_dir>/section_<NN>_<slug>.json``; the section
    index from Pass 1 is cached at ``<checkpoint_dir>/_sections_index.json``.
    """
    ckpt = Path(checkpoint_dir) if checkpoint_dir else None
    if ckpt is not None:
        ckpt.mkdir(parents=True, exist_ok=True)

    sections = _load_or_build_section_index(raw_text, llm, ckpt)

    manifest: dict = {
        "sections_completed": [],
        "step_id_index": [],
        "parts_introduced": [],
        "tools_in_use": [],
    }

    all_steps: list[Step] = []
    for i, (section_name, section_text) in enumerate(sections):
        section_steps = _load_or_parse_section(
            section_index=i,
            section_name=section_name,
            section_text=section_text,
            manifest=manifest,
            llm=llm,
            ckpt=ckpt,
        )
        all_steps.extend(section_steps)
        _update_manifest(manifest, section_name, section_steps)

    return all_steps


def _section_slug(name: str) -> str:
    """Filesystem-safe slug for a section name (used in checkpoint filenames)."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip().lower())
    return safe.strip("_") or "section"


def _load_or_build_section_index(
    raw_text: str,
    llm: LLMClient,
    ckpt: Path | None,
) -> list[tuple[str, str]]:
    """Use cached Pass 1 result if present, otherwise compute and cache it."""
    if ckpt is not None:
        idx_path = ckpt / "_sections_index.json"
        if idx_path.exists():
            data = json.loads(idx_path.read_text(encoding="utf-8"))
            print(f"  Stage 1: loaded section index from checkpoint ({len(data)} sections)")
            return [(item["section"], item["text"]) for item in data]

    sections = _build_section_index(raw_text, llm)

    if ckpt is not None:
        idx_path = ckpt / "_sections_index.json"
        idx_path.write_text(
            json.dumps(
                [{"section": name, "text": text} for name, text in sections],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    return sections


def _load_or_parse_section(
    section_index: int,
    section_name: str,
    section_text: str,
    manifest: dict,
    llm: LLMClient,
    ckpt: Path | None,
) -> list[Step]:
    """Return cached steps for this section if checkpointed, else parse + cache."""
    if ckpt is not None:
        slug = _section_slug(section_name)
        section_path = ckpt / f"section_{section_index:03d}_{slug}.json"
        if section_path.exists():
            data = json.loads(section_path.read_text(encoding="utf-8"))
            for item in data:
                _scrub_html_breaks(item)
            steps = [Step.model_validate(item) for item in data]
            print(f"  Stage 1: section {section_index + 1} ({section_name!r}) "
                  f"loaded from checkpoint ({len(steps)} steps)")
            return steps

    steps = _parse_section(section_name, section_text, manifest, llm)

    if ckpt is not None:
        slug = _section_slug(section_name)
        section_path = ckpt / f"section_{section_index:03d}_{slug}.json"
        section_path.write_text(
            json.dumps([s.model_dump() for s in steps], ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  Stage 1: section {section_index + 1} ({section_name!r}) "
              f"parsed + checkpointed ({len(steps)} steps)")
    return steps


def _build_section_index(raw_text: str, llm: LLMClient) -> list[tuple[str, str]]:
    """Pass 1 — locate section anchors and slice raw_text deterministically.

    Returns [(section_name, section_text), ...] in document order. Any preamble
    before the first anchor is prepended to the first section's chunk.
    """
    response = llm.complete(
        messages=[{"role": "user", "content": raw_text}],
        system=_SECTIONING_PROMPT,
        max_tokens=4000,
    )
    markers = _parse_json_response(response.content)

    if not markers:
        raise ValueError("Sectioning pass returned an empty list — cannot chunk document")

    positions: list[tuple[str, int]] = []
    for marker in markers:
        section_name = (marker.get("section") or "").strip()
        anchor = marker.get("anchor") or ""
        if not section_name or not anchor:
            continue
        idx = raw_text.find(anchor)
        if idx == -1 and len(anchor) > 40:
            # Tail of the anchor may have been paraphrased — retry with the head
            idx = raw_text.find(anchor[:40])
        if idx == -1:
            raise ValueError(
                f"Section anchor not found in raw text for section {section_name!r}. "
                f"Anchor: {anchor[:120]!r}"
            )
        positions.append((section_name, idx))

    if not positions:
        raise ValueError("Sectioning pass returned markers but none had usable name + anchor")

    # Defensive sort — model may emit out of order
    positions.sort(key=lambda p: p[1])

    sections: list[tuple[str, str]] = []
    for i, (name, start) in enumerate(positions):
        end = positions[i + 1][1] if i + 1 < len(positions) else len(raw_text)
        # Pull preamble into the first section so nothing is dropped
        chunk_start = 0 if i == 0 else start
        sections.append((name, raw_text[chunk_start:end]))
    return sections


_PER_SECTION_MAX_TOKENS = 32000


def _parse_section(
    section_name: str,
    section_text: str,
    manifest: dict,
    llm: LLMClient,
) -> list[Step]:
    """Pass 2 — parse one section's steps with the running manifest as context."""
    user_message = (
        f"SECTION: {section_name}\n\n"
        f"MANIFEST (context from prior sections):\n{json.dumps(manifest, indent=2)}\n\n"
        f"SECTION RAW TEXT:\n{section_text}"
    )
    response = llm.complete(
        messages=[{"role": "user", "content": user_message}],
        system=_PARSE_SECTION_PROMPT,
        max_tokens=_PER_SECTION_MAX_TOKENS,
        cache_system=True,  # Pass 2 calls share the same system prompt across all sections
    )
    if response.truncated:
        # Distinguish "model ran out of output budget" from "model emitted bad JSON" —
        # the fix for truncation is to chunk this section further or raise the cap,
        # not to debug the prompt.
        raise RuntimeError(
            f"Section {section_name!r} hit the output token limit "
            f"({_PER_SECTION_MAX_TOKENS}). The section is too large to parse in one "
            f"call. Raise _PER_SECTION_MAX_TOKENS or add intra-section chunking by "
            f"step boundaries."
        )
    parsed = _parse_json_response(response.content)
    for item in parsed:
        _scrub_html_breaks(item)
    return [Step.model_validate(item) for item in parsed]


# Strips HTML line-break tags the LLM occasionally emits inside body_text
# despite the prompt's prohibition. Catches `<br>`, `<br/>`, `<br />`
# (case-insensitive, any whitespace). Converts each to a single `\n` so the
# template's `replace('\n', '<br>')` filter renders the breaks correctly
# instead of showing literal tag text in the final PDF and review queue.
_BR_TAG_RE = re.compile(r"<\s*br\s*/?\s*>", re.IGNORECASE)


def _scrub_html_breaks(item: dict) -> None:
    body = item.get("body_text")
    if isinstance(body, str) and "<" in body:
        item["body_text"] = _BR_TAG_RE.sub("\n", body)


def _update_manifest(manifest: dict, section_name: str, steps: list[Step]) -> None:
    """Fold a parsed section's contributions into the carry-forward manifest."""
    manifest["sections_completed"].append(section_name)

    seen_parts = {p["part_id"] for p in manifest["parts_introduced"]}
    seen_tools = set(manifest["tools_in_use"])

    for step in steps:
        manifest["step_id_index"].append({
            "step_id": step.step_id,
            "section": step.section,
            "step_number": step.step_number,
            "heading": step.heading,
        })
        for part in step.parts_referenced:
            if part.part_id not in seen_parts:
                seen_parts.add(part.part_id)
                manifest["parts_introduced"].append({
                    "part_id": part.part_id,
                    "first_seen_step": step.step_id,
                })
        for tool in step.tools_operations:
            if tool not in seen_tools:
                seen_tools.add(tool)
                manifest["tools_in_use"].append(tool)


def _parse_json_response(content: str) -> list[dict]:
    content = content.strip()

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
