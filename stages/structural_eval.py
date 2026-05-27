"""
Stage 9 — Structural Evaluation

Deterministic post-pipeline check: did the revisions actually do what the
ECOs required? Runs without any ground-truth manual — every expectation
is derived from the ECO + original step + action plan, all of which the
pipeline already has in memory.

Six checks per (step, ECO) pair:
  A. new_part_mentioned     — when ECO adds a part, revised text names it
  B. old_part_removed       — when ECO removes/replaces a part, the old name no longer appears
  C. new_specs_present      — every "new" numeric/string value from the ECO appears in revised text
  D. old_specs_absent       — every replaced "old" value no longer appears in revised text
  E. summary_keywords_covered — at least one meaningful keyword from eco.summary survives
  F. confidence_acceptable  — agent flagged the step with high/medium confidence (info, not pass/fail)

Plus one document-level check:
  G. add_step_count_matches — number of is_new_step=True revisions equals
     the count of add_step_flagged plans

Output: dict with per-check and per-step results plus aggregate counts.
Writes alongside the PDF as eval_report.json.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from schemas.eco import ECO
from schemas.instruction import Step
from schemas.pipeline_state import ActionPlan, EvaluatedStep


# Words that show up in nearly every ECO summary and carry no signal on
# their own — exclude them from the keyword-coverage check.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "by", "is", "are", "was", "were", "be", "been", "this", "that", "it",
    "its", "as", "at", "from", "but", "not", "no", "yes",
    "part", "parts", "added", "removed", "changed", "modified", "updated",
    "sw_com_api", "folder_scan", "synth", "diff",
}

_NUMERIC = re.compile(r"\b\d+(?:\.\d+)?\b")


def run(
    original_steps: list[Step],
    evaluated_steps: list[EvaluatedStep],
    action_plans: list[ActionPlan],
    ecos: list[ECO],
) -> dict[str, Any]:
    eco_map = {eco.eco_id: eco for eco in ecos}
    original_map = {s.step_id: s for s in original_steps}
    plan_map = {(p.step_id, p.eco_id): p for p in action_plans}

    per_check_counts: dict[str, Counter] = {
        "new_part_mentioned":      Counter(),
        "old_part_removed":        Counter(),
        "new_specs_present":       Counter(),
        "old_specs_absent":        Counter(),
        "summary_keywords_covered": Counter(),
    }
    per_step_records: list[dict] = []

    for ev in evaluated_steps:
        rs = ev.revised_step
        eco = eco_map.get(rs.revision_source)
        if eco is None:
            continue
        plan = plan_map.get((rs.step_id, rs.revision_source))
        if plan is None or plan.action == "no_change":
            # no_change revisions are pass-throughs of the original text; not
            # interesting for structural eval.
            continue
        original = original_map.get(rs.step_id)

        revised_text = rs.revised_body_text or ""
        passed: list[str] = []
        failed: list[dict[str, str]] = []

        for check_name, ok, detail in _run_step_checks(revised_text, eco, original):
            per_check_counts[check_name][("pass" if ok else "fail")] += 1
            if ok:
                passed.append(check_name)
            else:
                failed.append({"check": check_name, "detail": detail})

        per_step_records.append({
            "step_id": rs.step_id,
            "eco_id":  rs.revision_source,
            "action":  plan.action,
            "confidence": rs.confidence,
            "is_new_step": rs.is_new_step,
            "passed": passed,
            "failed": failed,
        })

    doc_checks = _document_level_checks(action_plans, evaluated_steps)

    total_pass = sum(c["pass"] for c in per_check_counts.values()) + sum(1 for d in doc_checks if d["passed"])
    total_fail = sum(c["fail"] for c in per_check_counts.values()) + sum(1 for d in doc_checks if not d["passed"])
    total = total_pass + total_fail

    return {
        "summary": {
            "total_evaluations": total,
            "checks_passed":     total_pass,
            "checks_failed":     total_fail,
            "pass_rate":         round(total_pass / total, 3) if total else 1.0,
            "by_check": {
                name: {"pass": counts["pass"], "fail": counts["fail"]}
                for name, counts in per_check_counts.items()
            },
            "document_checks": doc_checks,
        },
        "by_step": per_step_records,
    }


def _run_step_checks(
    revised_text: str,
    eco: ECO,
    original: Step | None,
) -> list[tuple[str, bool, str]]:
    """Per-(step, eco) checks. Returns (check_name, passed, detail) tuples.

    A check is only emitted when it applies — e.g. "new_part_mentioned" is
    skipped if the ECO doesn't add a part. That keeps the failure rate from
    being diluted by N/A buckets.
    """
    results: list[tuple[str, bool, str]] = []
    text_lower = revised_text.lower()

    for change in eco.changes:
        field = (change.field or "").lower()
        old = (change.old or "").strip()
        new = (change.new or "").strip()

        is_part_field = field == "part"
        is_meaningful_text = bool(new and len(new) >= 2)
        is_part_add    = is_part_field and not old and is_meaningful_text
        is_part_remove = is_part_field and old and not new
        is_part_swap   = is_part_field and old and new and old != new

        # ── A. new_part_mentioned ─────────────────────────────────────────
        if is_part_add or is_part_swap:
            name = _strip_extension(new).lower()
            ok = bool(name) and name in text_lower
            if name:  # only emit if there's something concrete to look for
                results.append((
                    "new_part_mentioned",
                    ok,
                    "" if ok else f"new part {new!r} not found in revised text",
                ))

        # ── B. old_part_removed ───────────────────────────────────────────
        if is_part_remove or is_part_swap:
            old_name = _strip_extension(old).lower()
            ok = bool(old_name) and old_name not in text_lower
            if old_name:
                results.append((
                    "old_part_removed",
                    ok,
                    "" if ok else f"old part {old!r} still appears in revised text",
                ))

        # ── C/D. spec changes (dimensions, materials, properties, etc.) ───
        # Only run these on non-part fields. Numeric values get extra weight
        # because they're the spec-correctness signal that matters most.
        if not is_part_field:
            if new and not is_blank_or_placeholder(new):
                new_lower = new.lower()
                ok = new_lower in text_lower or _numeric_match(new, revised_text)
                results.append((
                    "new_specs_present",
                    ok,
                    "" if ok else f"new value {new!r} for {field!r} not found in revised text",
                ))
            if old and not is_blank_or_placeholder(old) and old != new:
                old_lower = old.lower()
                # Only flag literal substring presence; numeric values often
                # coincide across unrelated specs so we don't strict-match them
                # for the absence check.
                ok = old_lower not in text_lower
                results.append((
                    "old_specs_absent",
                    ok,
                    "" if ok else f"old value {old!r} for {field!r} still appears in revised text",
                ))

    # ── E. summary keyword coverage ────────────────────────────────────────
    keywords = _extract_keywords(eco.summary)
    if keywords:
        hit = any(kw in text_lower for kw in keywords)
        results.append((
            "summary_keywords_covered",
            hit,
            "" if hit else f"none of the summary keywords {sorted(keywords)[:5]!r} appear in revised text",
        ))

    return results


def _document_level_checks(
    action_plans: list[ActionPlan],
    evaluated_steps: list[EvaluatedStep],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    # G. add_step_count_matches
    planned_new = sum(1 for p in action_plans if p.action == "add_step_flagged")
    actual_new  = sum(1 for ev in evaluated_steps if ev.revised_step.is_new_step)
    checks.append({
        "name":   "add_step_count_matches",
        "passed": planned_new == actual_new,
        "detail": f"plans called for {planned_new} new step(s), pipeline produced {actual_new}",
    })

    # Confidence distribution — info only, not pass/fail
    conf_counter = Counter(ev.revised_step.confidence for ev in evaluated_steps)
    checks.append({
        "name":   "confidence_distribution",
        "passed": True,
        "detail": f"high={conf_counter['high']}, medium={conf_counter['medium']}, low={conf_counter['low']}",
    })

    return checks


def _strip_extension(name: str) -> str:
    """Strip .SLDPRT / .SLDASM so 'bracket.SLDPRT' matches plain 'bracket' in prose."""
    lower = name.lower()
    for ext in (".sldprt", ".sldasm", ".slddrw"):
        if lower.endswith(ext):
            return name[: -len(ext)]
    return name


def is_blank_or_placeholder(value: str) -> bool:
    v = value.strip().lower()
    return v in {"", "none", "null", "n/a", "-", "—"}


def _numeric_match(needle: str, haystack: str) -> bool:
    """Detect a numeric value from `needle` showing up in `haystack`.

    ECO values often arrive as ``"6.0"`` while prose says ``"6 mm"`` — direct
    substring search misses these. Pull the numeric tokens from each and
    check overlap on rounded-to-3-decimal floats.
    """
    needle_nums = _NUMERIC.findall(needle)
    if not needle_nums:
        return False
    haystack_nums = set(_NUMERIC.findall(haystack))
    for n in needle_nums:
        try:
            target = round(float(n), 3)
        except ValueError:
            continue
        for h in haystack_nums:
            try:
                if abs(round(float(h), 3) - target) < 1e-6:
                    return True
            except ValueError:
                continue
    return False


def _extract_keywords(summary: str) -> set[str]:
    """Pull content-bearing tokens from an ECO summary for coverage check."""
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", summary.lower())
    return {t for t in tokens if t not in _STOPWORDS}


def render_report(report: dict[str, Any]) -> str:
    """One-screen human summary for stdout."""
    s = report["summary"]
    lines = [
        f"Structural eval — pass rate {s['pass_rate']*100:.1f}% "
        f"({s['checks_passed']}/{s['total_evaluations']})",
    ]
    for name, counts in s["by_check"].items():
        total = counts["pass"] + counts["fail"]
        if total == 0:
            continue
        rate = counts["pass"] / total
        lines.append(f"  {name:<28} {counts['pass']:>4} / {total:<4}  ({rate*100:5.1f}%)")
    for doc in s["document_checks"]:
        ok = "PASS" if doc["passed"] else "FAIL"
        lines.append(f"  {doc['name']:<28} {ok}  ({doc['detail']})")
    return "\n".join(lines)
