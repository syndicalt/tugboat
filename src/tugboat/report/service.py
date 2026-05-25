from __future__ import annotations

from pathlib import Path

from tugboat.artifacts import validate_report_markdown
from tugboat.paths import runs_dir
from tugboat.policy.gate import CandidatePatch, PolicyDecision


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
    text = "\n".join(
        [
            "# Tugboat Report",
            "",
            f"- candidate: {candidate.base_file}",
            f"- risk_class: {candidate.risk_class}",
            f"- policy_allowed: {str(decision.allowed).lower()}",
            f"- policy_reasons: {','.join(decision.reasons)}",
            f"- eval_report: {eval_report_path.relative_to(repo)}",
            "",
            "## Rationale",
            "",
            candidate.rationale,
            "",
        ]
    )
    validate_report_markdown(text)
    report_path.write_text(text, encoding="utf-8")
    return report_path


def _repo_local_run_dir(repo: Path, run_id: str) -> Path:
    run_dir = runs_dir(repo) / run_id
    if not run_dir.resolve().is_relative_to(repo.resolve()):
        raise ValueError("run_id must resolve inside repo")
    return run_dir
