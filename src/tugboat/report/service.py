from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.artifacts import validate_json_artifact, validate_report_markdown
from tugboat.paths import ensure_private_dir, mark_private_file, runs_dir
from tugboat.policy.gate import CandidatePatch, PolicyDecision
from tugboat.security.secrets import SecretScanError, scan_text


def write_report(
    repo: Path,
    run_id: str,
    *,
    candidate: CandidatePatch,
    decision: PolicyDecision,
    eval_report_path: Path,
) -> Path:
    run_dir = _repo_local_run_dir(repo, run_id)
    ensure_private_dir(runs_dir(repo))
    ensure_private_dir(run_dir)
    report_path = run_dir / "report.md"
    evidence_chain = _evidence_chain_lines(repo, run_dir, eval_report_path)
    eval_summary = _eval_summary_lines(eval_report_path)
    optimization_summary = _optimization_summary_lines(repo, run_dir / "optimization-summary.json")
    text = "\n".join(
        [
            "# Tugboat Report",
            "",
            "- schema_version: 1",
            f"- candidate: {candidate.base_file}",
            f"- risk_class: {candidate.risk_class}",
            f"- policy_allowed: {str(decision.allowed).lower()}",
            f"- policy_reasons: {','.join(decision.reasons)}",
            *evidence_chain,
            *eval_summary,
            *optimization_summary,
            "",
            "## Rationale",
            "",
            candidate.rationale,
            "",
        ]
    )
    validate_report_markdown(text)
    findings = scan_text(report_path.as_posix(), text)
    if findings:
        raise SecretScanError(findings)
    report_path.write_text(text, encoding="utf-8")
    mark_private_file(report_path)
    return report_path


def _evidence_chain_lines(repo: Path, run_dir: Path, eval_report_path: Path) -> list[str]:
    artifact_fields = (
        ("trace_input", run_dir / "trace-input.jsonl"),
        ("instruction_snapshot", run_dir / "instruction-snapshot"),
        ("instruction_graph", run_dir / "instruction-graph.json"),
        ("audit_report", run_dir / "audit.json"),
        ("candidate_metadata", run_dir / "candidate.json"),
        ("candidate_diff", run_dir / "candidate.diff"),
        ("policy_gate", run_dir / "policy-gate.json"),
        ("eval_report", eval_report_path),
        ("decision_artifact", run_dir / "decision.json"),
        ("provenance_bundle", run_dir / "provenance-bundle.json"),
    )
    return [
        f"- {field}: {path.relative_to(repo)}"
        for field, path in artifact_fields
        if field == "eval_report" or path.exists()
    ]


def _eval_summary_lines(eval_report_path: Path) -> list[str]:
    if not eval_report_path.exists():
        return []
    payload: Any = json.loads(eval_report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("eval report must be a JSON object")
    validate_json_artifact("eval-report.json", payload)
    fields = (
        "trigger_score",
        "held_out_score",
        "governance_passed",
        "recommendation",
        "live_provider_required",
    )
    return [
        f"- {field}: {_report_scalar(payload[field])}" for field in fields if field in payload
    ] + _longitudinal_summary_lines(payload)


def _longitudinal_summary_lines(payload: dict[str, Any]) -> list[str]:
    metrics = payload.get("longitudinal_metrics")
    if not isinstance(metrics, dict):
        return []
    fields = (
        "acceptance_rate",
        "rejection_rate",
        "rollback_rate",
        "recurring_incident_rate",
        "mean_changed_lines",
        "corpus_growth",
        "duplicate_rule_count",
        "governance_regression_count",
        "user_correction_recurrence",
    )
    return [
        f"- longitudinal_{field}: {_report_scalar(metrics[field])}"
        for field in fields
        if field in metrics
    ]


def _optimization_summary_lines(repo: Path, optimization_summary_path: Path) -> list[str]:
    if not optimization_summary_path.exists():
        return []
    payload: Any = json.loads(optimization_summary_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("optimization summary must be a JSON object")
    validate_json_artifact("optimization-summary.json", payload)
    fields = (
        ("decision", "optimization_decision"),
        ("suite_id", "optimization_suite_id"),
        ("trigger_score", "optimization_trigger_score"),
        ("held_out_score", "optimization_held_out_score"),
        ("governance_passed", "optimization_governance_passed"),
        ("recommendation", "optimization_recommendation"),
    )
    return [
        f"- optimization_summary: {optimization_summary_path.relative_to(repo)}",
        *[
            f"- {label}: {_report_scalar(payload[field])}"
            for field, label in fields
            if field in payload
        ],
    ]


def _report_scalar(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _repo_local_run_dir(repo: Path, run_id: str) -> Path:
    run_dir = runs_dir(repo) / run_id
    if not run_dir.resolve().is_relative_to(repo.resolve()):
        raise ValueError("run_id must resolve inside repo")
    return run_dir
