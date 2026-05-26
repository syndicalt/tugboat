from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.artifacts import validate_json_artifact, validate_report_markdown
from tugboat.paths import runs_dir
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
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "report.md"
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
            f"- eval_report: {eval_report_path.relative_to(repo)}",
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
    return report_path


def _eval_summary_lines(eval_report_path: Path) -> list[str]:
    if not eval_report_path.exists():
        return []
    payload: Any = json.loads(eval_report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("eval report must be a JSON object")
    fields = (
        "trigger_score",
        "held_out_score",
        "governance_passed",
        "recommendation",
        "live_provider_required",
    )
    return [f"- {field}: {_report_scalar(payload[field])}" for field in fields if field in payload]


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
