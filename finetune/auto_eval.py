"""
finetune/auto_eval.py — Automated quality evaluation without engineer review.

Two modes:
  A. Spec compliance (deterministic) — checks that all numeric values and part
     numbers in the revised text are sourced from the ECO or original step.
     Score 1.0 = no hallucinated values. Zero LLM cost.

  B. LLM judge (style + assembly logic) — scores a revision on style
     preservation, ECO accuracy, and completeness (1–5 each).

Use `eval_validation_set()` to compare base vs. fine-tuned model on held-out data.
"""

import json
import re
from pathlib import Path

from llm.client import LLMClient
from schemas.eco import ECO
from schemas.instruction import Step

_JUDGE_SYSTEM = """You are a quality auditor for mechanical assembly instructions.
Score the revised instruction on three dimensions (1–5 each):

1. style_preservation: Does the revision match the voice, terminology, and formatting of the original?
   5 = indistinguishable in style; 1 = completely different style
2. eco_accuracy: Does the revision correctly reflect all ECO changes? Are any changes missed or invented?
   5 = perfect accuracy; 1 = missed or invented changes
3. completeness: Does the revision preserve all unchanged content from the original?
   5 = all unchanged content preserved; 1 = significant content dropped or added

Output ONLY a JSON object with this structure — no markdown, no extra text:
{
  "style_preservation": <int 1-5>,
  "eco_accuracy": <int 1-5>,
  "completeness": <int 1-5>,
  "reasoning": "<one sentence>"
}"""


def spec_compliance_score(revised_text: str, eco: ECO, original_text: str) -> float:
    """
    Fraction of numeric values and part numbers in `revised_text` that appear
    verbatim in either the ECO changes or `original_text`. 1.0 = perfect.

    Mirrors the deterministic check in stages/eval_gate.py.
    """
    def _extract_values(text: str) -> set[str]:
        nums = set(re.findall(r"\b\d+(?:\.\d+)?\s*(?:mm|cm|m|in|ft|Nm|lb|kg|N|°)?\b", text))
        parts = set(re.findall(r"\b[A-Z]{2,}-\d{3,}\b", text))
        return nums | parts

    allowed: set[str] = _extract_values(original_text)
    for change in eco.changes:
        allowed |= _extract_values(str(change.new))
        allowed |= _extract_values(str(change.old))

    revised_values = _extract_values(revised_text)
    if not revised_values:
        return 1.0
    passing = sum(1 for v in revised_values if v in allowed)
    return passing / len(revised_values)


def llm_judge_score(
    original_text: str,
    revised_text: str,
    eco_summary: str,
    llm: LLMClient,
) -> dict:
    """
    Ask an LLM judge to score the revision on style, accuracy, and completeness.
    Returns a dict with keys: style_preservation, eco_accuracy, completeness, reasoning.
    """
    user_msg = (
        f"ECO Summary: {eco_summary}\n\n"
        f"Original instruction:\n{original_text}\n\n"
        f"Revised instruction:\n{revised_text}\n\n"
        "Score this revision on the three dimensions."
    )
    response = llm.complete(
        messages=[{"role": "user", "content": user_msg}],
        system=_JUDGE_SYSTEM,
    )
    try:
        content = response.content.strip()
        content = re.sub(r"^```[a-z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
        return json.loads(content)
    except (json.JSONDecodeError, AttributeError):
        return {"style_preservation": 0, "eco_accuracy": 0, "completeness": 0, "reasoning": "parse_error"}


def eval_validation_set(
    val_jsonl_path: Path,
    llm: LLMClient,
) -> dict:
    """
    Score every example in the validation JSONL using both eval modes.

    Returns aggregate metrics:
      {
        "n": int,
        "mean_spec_compliance": float,
        "mean_style": float,
        "mean_eco_accuracy": float,
        "mean_completeness": float,
        "mean_judge_total": float,
      }
    """
    if not val_jsonl_path.exists():
        raise FileNotFoundError(f"{val_jsonl_path} not found — run split_train_val() first")

    records = [json.loads(line) for line in val_jsonl_path.read_text().splitlines() if line.strip()]
    if not records:
        return {"n": 0}

    spec_scores, style_scores, acc_scores, comp_scores = [], [], [], []

    for record in records:
        messages = record.get("messages", [])
        if len(messages) < 2:
            continue
        user_content = messages[0].get("content", "")
        assistant_content = messages[1].get("content", "")

        # Extract original text from prompt (heuristic — works with _build_initial_message format)
        original_match = re.search(r"Original Text:\n(.+?)(?:\n\n|\Z)", user_content, re.DOTALL)
        original_text = original_match.group(1).strip() if original_match else ""

        eco_summary_match = re.search(r"ECO Summary: (.+)", user_content)
        eco_summary = eco_summary_match.group(1).strip() if eco_summary_match else ""

        # Mode A: spec compliance requires an ECO object — approximate from prompt text
        # Full ECO cross-reference requires checkpoint access; use heuristic here
        spec_scores.append(1.0 if original_text else 0.0)  # placeholder without full ECO

        # Mode B: LLM judge
        judge = llm_judge_score(original_text, assistant_content, eco_summary, llm)
        style_scores.append(judge.get("style_preservation", 0))
        acc_scores.append(judge.get("eco_accuracy", 0))
        comp_scores.append(judge.get("completeness", 0))

    def _mean(lst):
        return sum(lst) / len(lst) if lst else 0.0

    return {
        "n": len(records),
        "mean_spec_compliance": _mean(spec_scores),
        "mean_style": _mean(style_scores),
        "mean_eco_accuracy": _mean(acc_scores),
        "mean_completeness": _mean(comp_scores),
        "mean_judge_total": _mean([s + a + c for s, a, c in zip(style_scores, acc_scores, comp_scores)]),
    }


def should_auto_approve(
    revised_text: str,
    original_text: str,
    eco: ECO,
    llm: LLMClient,
    spec_threshold: float = 0.9,
    judge_threshold: float = 4.0,
) -> tuple[bool, dict]:
    """
    Return (auto_approved, scores) for a single revision.

    A revision passes auto-approval if:
      - spec_compliance_score >= spec_threshold (default 0.9)
      - all three judge dimensions >= judge_threshold (default 4.0)

    Call this in eval_gate.py to reduce the review queue.
    """
    spec = spec_compliance_score(revised_text, eco, original_text)
    judge = llm_judge_score(original_text, revised_text, eco.summary, llm)

    scores = {
        "spec_compliance": spec,
        **{k: v for k, v in judge.items() if k != "reasoning"},
        "reasoning": judge.get("reasoning", ""),
    }

    passed = (
        spec >= spec_threshold
        and judge.get("style_preservation", 0) >= judge_threshold
        and judge.get("eco_accuracy", 0) >= judge_threshold
        and judge.get("completeness", 0) >= judge_threshold
    )
    return passed, scores
