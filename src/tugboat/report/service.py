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
    eval_payload = _validated_eval_payload(eval_report_path)
    optimization_summary_path = run_dir / "optimization-summary.json"
    review_summary = _review_readiness_lines(
        repo,
        run_dir,
        candidate=candidate,
        decision=decision,
        optimization_summary_path=optimization_summary_path,
    )
    optimization_summary = _optimization_summary_lines(repo, optimization_summary_path)
    impact_summary = _highest_impact_summary_lines(eval_payload, optimization_summary_path)
    text = "\n".join(
        [
            "# Tugboat Report",
            "",
            "- schema_version: 1",
            f"- candidate: {candidate.base_file}",
            f"- risk_class: {candidate.risk_class}",
            f"- policy_allowed: {str(decision.allowed).lower()}",
            f"- policy_reasons: {','.join(decision.reasons)}",
            *review_summary,
            *evidence_chain,
            *eval_summary,
            *impact_summary,
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
        ("candidate_ranking", run_dir / "candidate-ranking.json"),
        ("candidate_diff", run_dir / "candidate.diff"),
        ("policy_gate", run_dir / "policy-gate.json"),
        ("eval_report", eval_report_path),
        ("acceptance_summary", run_dir / "acceptance-summary.raw.json"),
        ("decision_artifact", run_dir / "decision.json"),
        ("provenance_bundle", run_dir / "provenance-bundle.json"),
    )
    return [
        f"- {field}: {path.relative_to(repo)}"
        for field, path in artifact_fields
        if field == "eval_report" or path.exists()
    ]


def _review_readiness_lines(
    repo: Path,
    run_dir: Path,
    *,
    candidate: CandidatePatch,
    decision: PolicyDecision,
    optimization_summary_path: Path,
) -> list[str]:
    return [
        f"- risk_explanation: {risk_explanation_summary(candidate.risk_class, decision.allowed, decision.reasons)}",
        f"- rollback_readiness: {_rollback_readiness_summary(repo, run_dir, optimization_summary_path=optimization_summary_path, applied_commit='')}",
    ]


def risk_explanation_summary(
    risk_class: object,
    policy_allowed: bool | object,
    policy_reasons: tuple[str, ...] | list[object],
) -> str:
    reasons = [str(reason) for reason in policy_reasons if str(reason)]
    return (
        f"class={_report_scalar(risk_class)} "
        f"policy_allowed={_report_scalar(bool(policy_allowed))} "
        f"policy_reasons={','.join(reasons) if reasons else 'none'} "
        f"review_required={_review_required_reason(risk_class)}"
    )


def rollback_readiness_summary(
    repo: Path,
    run_dir: Path,
    *,
    applied_commit: str = "",
) -> str:
    return _rollback_readiness_summary(
        repo,
        run_dir,
        optimization_summary_path=run_dir / "optimization-summary.json",
        applied_commit=applied_commit,
    )


def _rollback_readiness_summary(
    repo: Path,
    run_dir: Path,
    *,
    optimization_summary_path: Path,
    applied_commit: str,
) -> str:
    rollback_plan = run_dir / "rollback-plan.json"
    apply_plan = run_dir / "apply-plan.json"
    if applied_commit and rollback_plan.exists():
        return _rollback_readiness_fields(
            state="applied_ready",
            command="none",
            artifact=_relative_ref(repo, rollback_plan),
            applied_commit="present",
        )
    if apply_plan.exists():
        command = _rollback_command_from_artifact(apply_plan, "apply-plan.json")
        return _rollback_readiness_fields(
            state="apply_ready",
            command=command,
            artifact=_relative_ref(repo, apply_plan),
            applied_commit="missing",
        )
    if optimization_summary_path.exists():
        command = _rollback_command_from_artifact(
            optimization_summary_path,
            "optimization-summary.json",
        )
        if command != "none":
            return _rollback_readiness_fields(
                state="planned",
                command=command,
                artifact=_relative_ref(repo, optimization_summary_path),
                applied_commit="missing",
            )
    return _rollback_readiness_fields(
        state="missing",
        command="none",
        artifact="none",
        applied_commit="present" if applied_commit else "missing",
    )


def _rollback_readiness_fields(
    *,
    state: str,
    command: str,
    artifact: str,
    applied_commit: str,
) -> str:
    return (
        f"state={state} "
        f"command={command} "
        f"artifact={artifact} "
        f"applied_commit={applied_commit}"
    )


def _review_required_reason(risk_class: object) -> str:
    key = str(risk_class).strip().lower()
    if key in {"b", "class_b"}:
        return "class_b_review_required"
    if key in {"c", "class_c"} or "restricted" in key or "policy" in key:
        return "restricted_review_required"
    return "none"


def _eval_summary_lines(eval_report_path: Path) -> list[str]:
    payload = _validated_eval_payload(eval_report_path)
    if not payload:
        return []
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


def _validated_eval_payload(eval_report_path: Path) -> dict[str, Any]:
    if not eval_report_path.exists():
        return {}
    payload: Any = json.loads(eval_report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("eval report must be a JSON object")
    validate_json_artifact("eval-report.json", payload)
    return payload


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
    payload = _validated_optimization_summary_payload(optimization_summary_path)
    fields = (
        ("decision", "optimization_decision"),
        ("suite_id", "optimization_suite_id"),
        ("trigger_score", "optimization_trigger_score"),
        ("held_out_score", "optimization_held_out_score"),
        ("governance_passed", "optimization_governance_passed"),
        ("recommendation", "optimization_recommendation"),
    )
    acceptance_reasons = payload.get("acceptance_reasons", [])
    reviewer_checklist = payload.get("reviewer_checklist", [])
    rollback_command = payload.get("rollback_command", [])
    return [
        f"- optimization_summary: {optimization_summary_path.relative_to(repo)}",
        *[
            f"- {label}: {_report_scalar(payload[field])}"
            for field, label in fields
            if field in payload
        ],
        *(
            [f"- acceptance_reason: {'; '.join(str(reason) for reason in acceptance_reasons)}"]
            if isinstance(acceptance_reasons, list) and acceptance_reasons
            else []
        ),
        *(
            [f"- reviewer_checklist: {'; '.join(str(item) for item in reviewer_checklist)}"]
            if isinstance(reviewer_checklist, list) and reviewer_checklist
            else []
        ),
        *(
            [f"- rollback_command: {_format_rollback_command(rollback_command)}"]
            if rollback_command
            else []
        ),
    ]


def _highest_impact_summary_lines(
    eval_payload: dict[str, Any],
    optimization_summary_path: Path,
) -> list[str]:
    if not eval_payload or not optimization_summary_path.exists():
        return []
    optimization_payload = _validated_optimization_summary_payload(optimization_summary_path)
    fields = highest_impact_summary_fields(eval_payload, optimization_payload)
    if fields is None:
        return []
    return [
        "- highest_impact_summary: "
        f"{fields['target']} {fields['operator']} "
        f"changed_lines={fields['changed_lines']} "
        f"held_out_delta={fields['held_out_delta']} "
        f"instruction_token_delta={fields['instruction_token_delta']} "
        f"governance_passed={fields['governance_passed']}"
    ]


def highest_impact_summary_fields(
    eval_payload: dict[str, Any],
    optimization_payload: dict[str, Any],
) -> dict[str, str] | None:
    edit = _highest_impact_edit(optimization_payload)
    if edit is None:
        return None
    metrics = eval_payload.get("metrics", {})
    token_delta = (
        metrics.get("instruction_token_delta")
        if isinstance(metrics, dict)
        else None
    )
    return {
        "target": _impact_target(edit),
        "operator": _report_scalar(edit.get("operator", "unknown")),
        "changed_lines": str(_int_or_zero(edit.get("changed_lines"))),
        "normative_changes": str(_int_or_zero(edit.get("normative_changes"))),
        "held_out_delta": _score_delta(
            eval_payload.get("held_out_score"),
            eval_payload.get("trigger_score"),
        ),
        "instruction_token_delta": _report_scalar(
            token_delta if token_delta is not None else "unknown"
        ),
        "governance_passed": _report_scalar(eval_payload.get("governance_passed", "unknown")),
    }


def _validated_optimization_summary_payload(optimization_summary_path: Path) -> dict[str, Any]:
    payload: Any = json.loads(optimization_summary_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("optimization summary must be a JSON object")
    validate_json_artifact("optimization-summary.json", payload)
    return payload


def _highest_impact_edit(payload: dict[str, Any]) -> dict[str, object] | None:
    edits = payload.get("accepted_bounded_edit_metadata", [])
    if not isinstance(edits, list):
        return None
    edit_objects = [edit for edit in edits if isinstance(edit, dict)]
    if not edit_objects:
        return None
    return max(
        edit_objects,
        key=lambda edit: (
            _int_or_zero(edit.get("normative_changes")),
            _int_or_zero(edit.get("changed_lines")),
            _report_scalar(edit.get("file", "")),
            _report_scalar(edit.get("section", "")),
        ),
    )


def _impact_target(edit: dict[str, object]) -> str:
    file_name = _report_scalar(edit.get("file", "unknown"))
    section = _report_scalar(edit.get("section", ""))
    return f"{file_name}#{section}" if section else file_name


def _score_delta(held_out_score: object, trigger_score: object) -> str:
    try:
        delta = float(held_out_score) - float(trigger_score)
    except (TypeError, ValueError):
        return "unknown"
    return f"{delta:.2f}"


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _report_scalar(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _format_rollback_command(value: object) -> str:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return " ".join(value)
    if isinstance(value, list) and all(isinstance(item, list) for item in value):
        return "; ".join(" ".join(str(part) for part in item) for item in value)
    return str(value)


def _rollback_command_from_artifact(path: Path, artifact_name: str) -> str:
    payload = _load_optional_json_object(path, artifact_name)
    if payload is None:
        return "none"
    if artifact_name in {"apply-plan.json", "optimization-summary.json"}:
        try:
            validate_json_artifact(artifact_name, payload)
        except ValueError:
            return "none"
    return _format_rollback_command(payload.get("rollback_command", []))


def _load_optional_json_object(path: Path, artifact_name: str) -> dict[str, Any] | None:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        raise ValueError(f"{artifact_name} must be a JSON object")
    return payload


def _relative_ref(repo: Path, path: Path) -> str:
    try:
        return path.relative_to(repo).as_posix()
    except ValueError:
        return path.as_posix()


def _repo_local_run_dir(repo: Path, run_id: str) -> Path:
    run_dir = runs_dir(repo) / run_id
    if not run_dir.resolve().is_relative_to(repo.resolve()):
        raise ValueError("run_id must resolve inside repo")
    return run_dir
