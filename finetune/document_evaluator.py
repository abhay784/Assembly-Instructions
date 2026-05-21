"""
Document Evaluator — Phase 2.5

Evaluates the entire assembly instruction document for consistency, sequence errors,
physics violations, and prerequisite issues. This catches cross-step logic errors that
reveal physics misunderstandings and teaches the fine-tuned model assembly constraints.

Output: document_feedback.jsonl with document-level audit results + per-step guidance.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from llm.client import LLMClient
from schemas.instruction import Step
from schemas.pipeline_state import EvaluatedStep


_DOCUMENT_AUDITOR_SYSTEM = """You are an assembly instruction auditor. Your job is to read a complete assembly instruction document and identify:

1. **Consistency issues** — Steps contradict each other or use inconsistent terminology
2. **Sequence errors** — Steps are in wrong order, prerequisites violated
3. **Impossible operations** — Physical impossibilities (e.g., install part before it's accessible)
4. **Clarity problems** — Ambiguous wording that could confuse assembly
5. **Prerequisite violations** — A step requires an earlier step that hasn't been done yet
6. **Terminology inconsistency** — Same part called different names, inconsistent unit/style

**Respond with JSON:**
{
  "overall_quality_score": <0-100>,
  "total_issues": <count>,
  "issues": [
    {
      "type": "inconsistency" | "clarity" | "sequence" | "missing_prerequisite" | "impossible_operation" | "terminology",
      "severity": "critical" | "warning" | "info",
      "steps_involved": [<step_ids>],
      "description": "<human-readable explanation>",
      "suggestion": "<how to fix it>"
    }
  ],
  "cross_step_patterns": [
    {
      "pattern_type": "terminology_inconsistency" | "formatting_inconsistency" | "spec_inconsistency",
      "description": "<what pattern was found>",
      "affected_steps": [<step_ids>],
      "examples": ["<example from step X>", "<example from step Y>"],
      "suggestion": "<standardize to...>"
    }
  ],
  "physics_concerns": [
    {
      "concern": "sequence_violation" | "impossible_mate" | "torque_issue" | "tool_issue" | "accessibility",
      "severity": "critical" | "warning",
      "step": <step_id>,
      "description": "<what could go wrong>",
      "recommendation": "<what to fix>"
    }
  ],
  "approval_readiness": {
    "ready_to_publish": <boolean>,
    "blocking_issues": [<issue descriptions if not ready>],
    "confidence": <0-1, how confident is this audit>
  }
}

Output ONLY valid JSON. Be thorough but pragmatic — minor formatting issues are 'info', not 'warning'."""


def evaluate_document(
    evaluated_steps: list[EvaluatedStep],
    original_steps: list[Step],
    llm: LLMClient,
) -> dict:
    """
    Evaluate entire assembly document for consistency, sequence, and physics issues.

    Args:
        evaluated_steps: List of EvaluatedStep from eval_gate (with revisions)
        original_steps: List of original Step (for context)
        llm: LLM client

    Returns:
        Audit result dictionary with issues, patterns, physics concerns
    """
    # Build document context: step_id → (original_text, revised_text)
    doc_context = []
    for ev_step in evaluated_steps:
        revised = ev_step.revised_step
        original = next((s for s in original_steps if s.step_id == revised.step_id), None)

        doc_context.append({
            "step_id": revised.step_id,
            "original_text": original.body_text if original else "",
            "revised_text": revised.revised_body_text,
            "is_new_step": revised.is_new_step,
            "confidence": revised.confidence,
            "eval_flags": [f.flag_type for f in ev_step.eval_flags],
        })

    # Send full document to LLM for holistic evaluation
    user_message = {
        "role": "user",
        "content": json.dumps({
            "document": doc_context,
            "instruction": "Audit this entire assembly instruction document for consistency, sequence, physics, and clarity issues.",
        }, indent=2),
    }

    response = llm.complete(
        messages=[user_message],
        system=_DOCUMENT_AUDITOR_SYSTEM,
    )

    result = _parse_audit_response(response.content)
    if result:
        return result
    else:
        return {
            "overall_quality_score": 0,
            "total_issues": 0,
            "issues": [],
            "cross_step_patterns": [],
            "physics_concerns": [],
            "approval_readiness": {
                "ready_to_publish": False,
                "blocking_issues": ["Document audit failed to complete"],
                "confidence": 0.0,
            },
            "_error": "audit_failed",
        }


def collect_document_feedback(
    evaluated_steps: list[EvaluatedStep],
    original_steps: list[Step],
    feedback_jsonl: Path,
    run_id: str,
    llm: LLMClient,
) -> int:
    """
    Collect document-level feedback and append to JSONL.

    Args:
        evaluated_steps: List of EvaluatedStep from eval_gate
        original_steps: List of original Step
        feedback_jsonl: Path to output JSONL file (append mode)
        run_id: Pipeline run ID
        llm: LLM client

    Returns:
        Count of new feedback records added (should be 1 per run)
    """
    # Check if already audited this run
    existing = _load_existing_audits(feedback_jsonl)
    if run_id in existing:
        return 0  # Already audited this run

    # Run document audit
    audit = evaluate_document(evaluated_steps, original_steps, llm)

    # Append to JSONL
    record = {
        "run_id": run_id,
        "timestamp": _get_timestamp(),
        "audit": audit,
        "_meta": {"id": run_id},
    }

    feedback_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with open(feedback_jsonl, "a") as f:
        f.write(json.dumps(record) + "\n")

    return 1


def print_document_report(feedback_jsonl: Path) -> None:
    """
    Print a summary report of document audits: trends, issue breakdown, physics concerns.
    """
    if not feedback_jsonl.exists():
        print("No document feedback found.")
        return

    audits = _load_feedback_jsonl(feedback_jsonl)
    if not audits:
        print("No document feedback found.")
        return

    print("\n" + "=" * 80)
    print("DOCUMENT QUALITY REPORT")
    print("=" * 80)

    # Overall trend (last few runs)
    if len(audits) > 0:
        recent = audits[-min(5, len(audits)):]
        scores = [a.get("audit", {}).get("overall_quality_score", 0) for a in recent]
        avg_recent = sum(scores) / len(scores) if scores else 0
        print(f"\nRecent Audits (last {len(recent)} runs):")
        print(f"  Avg Quality Score: {avg_recent:.1f}/100")

        if len(audits) > 1:
            first_score = audits[0].get("audit", {}).get("overall_quality_score", 0)
            trend = avg_recent - first_score
            direction = "↑" if trend > 0 else "↓" if trend < 0 else "→"
            print(f"  Trend vs. first run: {direction} {abs(trend):.1f} points")

    # Issue type breakdown (all audits)
    issue_types = {}
    for audit_record in audits:
        for issue in audit_record.get("audit", {}).get("issues", []):
            itype = issue.get("type", "unknown")
            issue_types[itype] = issue_types.get(itype, 0) + 1

    if issue_types:
        print(f"\nIssue Types (across all runs):")
        for itype, count in sorted(issue_types.items(), key=lambda x: -x[1]):
            avg_per_run = count / len(audits)
            print(f"  {itype}: {count} total ({avg_per_run:.1f} per run)")

    # Physics concerns
    physics_issues = {}
    for audit_record in audits:
        for concern in audit_record.get("audit", {}).get("physics_concerns", []):
            ctype = concern.get("concern", "unknown")
            physics_issues[ctype] = physics_issues.get(ctype, 0) + 1

    if physics_issues:
        print(f"\nPhysics Concerns:")
        for ctype, count in sorted(physics_issues.items(), key=lambda x: -x[1]):
            print(f"  {ctype}: {count} concerns")

    # Per-run detail
    print(f"\nBy Run:")
    for audit_record in audits[-5:]:  # Show last 5 runs
        run_id = audit_record.get("run_id")
        audit = audit_record.get("audit", {})
        score = audit.get("overall_quality_score", 0)
        n_issues = audit.get("total_issues", 0)
        ready = audit.get("approval_readiness", {}).get("ready_to_publish", False)
        status = "✓ Ready" if ready else "⚠ Review"
        print(f"  {run_id}: {score}/100, {n_issues} issues [{status}]")

    print("=" * 80 + "\n")


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _parse_audit_response(content: str) -> dict | None:
    """Parse JSON response from document auditor LLM."""
    content = content.strip()
    content = re.sub(r"^```[a-z]*\n?", "", content)
    content = re.sub(r"\n?```$", "", content)
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


def _load_existing_audits(feedback_jsonl: Path) -> set[str]:
    """Load set of already-audited run_ids."""
    if not feedback_jsonl.exists():
        return set()
    seen = set()
    for line in feedback_jsonl.read_text().splitlines():
        if line.strip():
            try:
                record = json.loads(line)
                run_id = record.get("run_id")
                if run_id:
                    seen.add(run_id)
            except json.JSONDecodeError:
                pass
    return seen


def _load_feedback_jsonl(feedback_jsonl: Path) -> list[dict]:
    """Load all audit records from JSONL file."""
    records = []
    if feedback_jsonl.exists():
        for line in feedback_jsonl.read_text().splitlines():
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _get_timestamp() -> str:
    """Get ISO 8601 timestamp."""
    return datetime.now(timezone.utc).isoformat()
