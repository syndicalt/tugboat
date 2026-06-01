from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from yaml import YAMLError

from tugboat.artifacts import (
    ArtifactValidationError,
    SCHEMA_VERSION,
    load_json_object_artifact,
    validate_json_artifact,
    write_json_artifact,
)
from tugboat.audit.pipeline import run_audit_pipeline
from tugboat.auto_apply import (
    AutoApplyCandidate,
    AutoApplyConfirmation,
    AutoApplyIncidentState,
    AutoApplyLanePolicy,
    AutoApplyPolicy,
    AutoApplyReadiness,
    VcsProof,
    evaluate_auto_apply,
)
from tugboat.config import DEFAULT_INSTRUCTION_FILES, load_policy
from tugboat.models import DEFAULT_FIXTURE_LLMFF_BINARY, Policy
from tugboat.corpus.indexer import InstructionIndexBudgetExceeded, index_repo
from tugboat.daemon.runner import (
    DaemonLoopConfig,
    default_trace_dirs,
    run_daemon_cycle,
    run_daemon_loop,
    write_worktree_profile,
)
from tugboat.daemon.service import (
    DaemonRunConfig,
    daemon_status,
    default_kill_switch,
    run_daemon_once,
    serve_daemon_socket,
)
from tugboat.daemon.queue import DaemonQueue, JobState, validate_local_bind_address
from tugboat.db import Store
from tugboat.eval.pipeline import run_eval_pipeline
from tugboat.harness.checks import (
    check_harness_legibility,
    generate_cleanup_candidates,
    generate_harness_report,
)
from tugboat.llmff.contracts import LlmffRunFailed
from tugboat.llmff.runner import MissingOutputError, command_prefix, inspect_manifest, run_manifest
from tugboat.manifests import (
    manifests_are_allowed_by_policy,
    materialize_manifests,
    require_manifest_contracts,
    validate_manifest_contracts,
)
from tugboat.mcp import run_stdio_server
from tugboat.ops.backup import (
    build_sidecar_backup_bundle,
    build_sidecar_restore_bundle,
    execute_sidecar_backup,
    execute_sidecar_restore,
)
from tugboat.ops.migrations import (
    assert_supported_sidecar_marker,
    dry_run_migration_plan,
    execute_migration_plan,
)
from tugboat.ops.observability import (
    observability_event_log_text,
    observability_metrics_text,
    summarize_sidecar_observability,
)
from tugboat.ops.retention import (
    RetentionScanBudgetExceeded,
    apply_retention_policy,
    export_redacted_artifacts,
)
from tugboat.optimization import (
    REJECTED_EDIT_SUPPRESSION_SIGNAL,
    EpisodeOutcome,
    build_minibatches,
    build_success_failure_minibatch,
    reflect_on_minibatch,
)
from tugboat.paths import latest_run_dir, mark_private_file, runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef, evaluate_candidate
from tugboat.report.decision_trace import write_decision_trace
from tugboat.propose.pipeline import run_propose_pipeline
from tugboat.report.service import (
    highest_impact_summary_fields,
    risk_explanation_summary,
    rollback_readiness_summary,
    write_report,
)
from tugboat.security.redaction import redact_text
from tugboat.security.secrets import SecretScanError, scan_path, scan_text
from tugboat.vcs import PullRequestResult, VcsAdapter, VcsStateError


REVIEW_REJECTION_TEMPLATES: dict[str, dict[str, str]] = {
    "redundant-rule": {
        "reason": "redundant_rule",
        "category": "proposal_quality",
        "failure_pattern": "duplicates existing guidance",
    },
    "too-broad": {
        "reason": "too_broad",
        "category": "bounded_edit_quality",
        "failure_pattern": "edit exceeds requested scope",
    },
    "weakens-safety": {
        "reason": "safety_weakening",
        "category": "policy_regression",
        "failure_pattern": "weakens safety or approval constraint",
    },
    "unsupported-evidence": {
        "reason": "unsupported_evidence",
        "category": "evidence_quality",
        "failure_pattern": "proposal is not grounded in trusted evidence",
    },
}


@dataclass(frozen=True)
class OptimizeWorkflowResult:
    exit_code: int
    run_dir: Path
    message: str


def _write_blocked_by_read_only(repo: Path, action: str) -> bool:
    if not default_kill_switch(repo).is_enabled():
        return False
    print(f"{action} blocked: read-only kill switch is enabled")
    return True


def _serialize_secret_scanned_json_artifact(
    path: Path,
    artifact_name: str,
    payload: dict[str, object],
) -> str:
    validate_json_artifact(artifact_name, payload)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    findings = scan_text(path.as_posix(), text)
    if findings:
        raise SecretScanError(findings)
    return text


def _write_secret_scanned_json_artifact(path: Path, artifact_name: str, payload: dict[str, object]) -> None:
    text = _serialize_secret_scanned_json_artifact(path, artifact_name, payload)
    path.write_text(text, encoding="utf-8")


def _initialize_repo_policy(repo: Path) -> Path:
    sidecar = sidecar_dir(repo)
    policy_path = sidecar / "policy.yaml"
    if policy_path.exists():
        raise FileExistsError(".sidecar/policy.yaml already exists")
    sidecar.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": 1,
        "mode": "proposal_only",
        "instruction_files": [
            {
                "path": item.path,
                "kind": item.kind,
                "precedence": item.precedence,
                "protected": item.protected,
            }
            for item in DEFAULT_INSTRUCTION_FILES
        ],
        "auto_apply": {
            "enabled": False,
            "max_changed_lines": Policy().auto_apply_max_changed_lines,
            "max_instruction_token_delta": Policy().auto_apply_max_instruction_token_delta,
            "minimum_burn_in_days": Policy().auto_apply_minimum_burn_in_days,
            "maximum_rejection_rate": Policy().auto_apply_maximum_rejection_rate,
            "maximum_rollback_rate": Policy().auto_apply_maximum_rollback_rate,
            "lanes": {
                lane.name: {
                    "enabled": lane.enabled,
                    "allowed_categories": list(lane.allowed_categories),
                    "allowed_risk_classes": list(lane.allowed_risk_classes),
                    "max_changed_lines": lane.max_changed_lines,
                    "max_instruction_token_delta": lane.max_instruction_token_delta,
                    "minimum_burn_in_days": lane.minimum_burn_in_days,
                    "maximum_rejection_rate": lane.maximum_rejection_rate,
                    "maximum_rollback_rate": lane.maximum_rollback_rate,
                }
                for lane in Policy().auto_apply_lanes
            },
        },
        "roadmap": {
            "drift_cluster": {
                "max_evidence_refs": Policy().roadmap_drift_cluster_max_evidence_refs,
            },
        },
        "index": {
            "max_instruction_files": Policy().index_max_instruction_files,
        },
        "trace": {
            "max_input_bytes": Policy().trace_max_input_bytes,
            "max_events": Policy().trace_max_events,
        },
        "llmff": {
            "binary": DEFAULT_FIXTURE_LLMFF_BINARY,
            "require_inspect": True,
            "allow_network": False,
        },
        "mcp": {"allowed_repositories": [str(repo)]},
    }
    text = yaml.safe_dump(payload, sort_keys=False)
    findings = scan_text(policy_path.as_posix(), text)
    if findings:
        raise SecretScanError(findings)
    policy_path.write_text(text, encoding="utf-8")
    (sidecar / ".gitignore").write_text(
        "*\n"
        "!.gitignore\n"
        "!policy.yaml\n"
        "!manifests/\n"
        "!manifests/**\n",
        encoding="utf-8",
    )
    return policy_path


def _print_doctor_report(repo: Path) -> int:
    policy_path = repo / ".sidecar" / "policy.yaml"
    policy_exists = policy_path.exists()
    policy = load_policy(repo)
    llmff_binary_status, llmff_binary_reason = _doctor_llmff_binary_status(policy)
    auto_apply_state = "enabled" if policy.auto_apply_enabled else "disabled"
    llmff_network_state = "enabled" if policy.llmff_allow_network else "disabled"
    allowed_providers = ", ".join(policy.llmff_allowed_providers) or "none"
    manifest_policy = (
        f"pinned {len(policy.allowed_manifest_hashes)}"
        if policy.allowed_manifest_hashes
        else "unrestricted"
    )

    print("tugboat: ok")
    print(f"repo: {repo}")
    print(f"policy: {'found' if policy_exists else 'missing'}")
    print(f"mode: {policy.mode}")
    print(f"auto_apply: {auto_apply_state}")
    print(f"llmff_network: {llmff_network_state}")
    print(f"llmff_binary: {llmff_binary_status}")
    print(f"allowed_providers: {allowed_providers}")
    print(f"manifest_policy: {manifest_policy}")
    recommendations = _doctor_recommendations(
        repo,
        policy,
        policy_exists,
        llmff_binary_status=llmff_binary_status,
        llmff_binary_reason=llmff_binary_reason,
    )
    if recommendations:
        for recommendation in recommendations:
            print(f"recommendation: {recommendation}")
    else:
        print("recommendation: none")
    return 0


def _doctor_llmff_binary_status(policy: Policy) -> tuple[str, str | None]:
    try:
        command = command_prefix(policy.llmff_binary)
    except ValueError as error:
        return "invalid", str(error)
    executable = command[0]
    if Path(executable).is_absolute() or "/" in executable:
        if Path(executable).exists():
            return "available", None
        return "missing", f"configured executable does not exist: {executable}"
    if shutil.which(executable) is None:
        return "missing", f"configured executable is not on PATH: {executable}"
    return "available", None


def _doctor_recommendations(
    repo: Path,
    policy: Policy,
    policy_exists: bool,
    *,
    llmff_binary_status: str,
    llmff_binary_reason: str | None,
) -> tuple[str, ...]:
    if not policy_exists:
        return (
            f"run `tugboat init --repo {repo}`",
            f"run `tugboat index --repo {repo}` after initialization",
        )

    recommendations: list[str] = []
    if llmff_binary_status in {"invalid", "missing"}:
        reason_suffix = f" ({llmff_binary_reason})" if llmff_binary_reason else ""
        recommendations.append(
            "fix llmff.binary in .sidecar/policy.yaml or rerun "
            f"`tugboat init --repo {repo}` on a fresh sidecar "
            f"[llmff_binary_{llmff_binary_status}]"
            f"{reason_suffix}"
        )
    if policy.mode != "proposal_only":
        recommendations.append("review policy mode before running apply or auto-apply")
    if policy.auto_apply_enabled:
        recommendations.append("review auto-apply lanes before running `tugboat auto-apply`")
    if policy.llmff_allow_network:
        recommendations.append("confirm provider manifests are reviewed and pinned")
    elif policy.llmff_allowed_providers:
        recommendations.append("remove allowed providers or enable llmff.allow_network intentionally")
    if not (repo / ".sidecar" / "db.sqlite").exists():
        recommendations.append(f"run `tugboat index --repo {repo}`")
    return tuple(recommendations)


def _print_apply_blocked_next_step(repo: Path, run_dir: Path, reason: str) -> None:
    repo_ref = repo.resolve()
    if "base_hash_mismatch" in reason or "base hash mismatch" in reason:
        print(
            f"next: re-run tugboat optimize --repo {repo_ref} --trace <trace> --suite all "
            "from the current base, then apply the new candidate"
        )
        return
    run_id = run_dir.name
    if (
        reason.startswith("policy gate rejected candidate")
        or reason == "stored policy gate rejected candidate"
        or reason == "eval policy gate rejected candidate"
    ):
        print(f"next: tugboat inspect-decision --repo {repo_ref} --decision {run_id}")
        print(f"next: tugboat report --repo {repo_ref} --run {run_id}")
        return
    if _apply_blocked_reason_is_eval_related(reason):
        eval_ref = _apply_blocked_artifact_ref(repo, run_dir, "eval-report.json")
        if eval_ref is not None:
            print(f"next: inspect {eval_ref}")
        print(f"next: tugboat report --repo {repo_ref} --run {run_id}")


def _print_trace_blocked_next_step(trace: Path, message: str) -> None:
    if message.startswith("audit blocked: trace file not found:"):
        print(f"next: create or export the trace file at {trace}")
        return
    if message.startswith("audit blocked: trace path is not a file:"):
        print(f"next: pass a trace file path instead of directory {trace}")
        return
    if message.startswith("audit blocked: invalid trace:"):
        if "rerun with --trace-format" in message:
            rerun_hint = message.rsplit("rerun with ", 1)[-1]
            print(f"next: rerun with {rerun_hint}")
            return
        print("next: validate the trace as JSONL or JSON and rerun with --trace-format auto")


def _policy_preflight_blocked(command: str, repo: Path) -> bool:
    try:
        load_policy(repo)
    except (OSError, ValueError, YAMLError) as error:
        print(f"{command} blocked: policy invalid: {error}")
        return True
    return _sidecar_schema_preflight_blocked(command, repo)


def _sidecar_schema_preflight_blocked(command: str, repo: Path) -> bool:
    try:
        assert_supported_sidecar_marker(repo)
    except (OSError, ValueError, YAMLError) as error:
        print(f"{command} blocked: {error}")
        return True
    return False


def _apply_blocked_reason_is_eval_related(reason: str) -> bool:
    return (
        reason.startswith("eval ")
        or reason.startswith("eval-report.json")
        or reason.startswith("held-out eval ")
        or reason.startswith("regression score ")
        or reason.startswith("validation ")
        or reason.startswith("stored validation ")
        or reason.startswith("trigger and held-out ")
        or reason == "eval report candidate_id does not match candidate"
        or reason == "eval report did not pass"
    )


def _apply_blocked_artifact_ref(repo: Path, run_dir: Path, artifact_name: str) -> str | None:
    path = run_dir / artifact_name
    if not path.exists():
        return None
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tugboat")
    subcommands = parser.add_subparsers(dest="command", required=True)
    doctor = subcommands.add_parser(
        "doctor",
        help="inspect local installation and repo policy posture",
    )
    doctor.add_argument("--repo", default=".")

    init = subcommands.add_parser(
        "init",
        help="bootstrap proposal-only sidecar policy",
    )
    init.add_argument("--repo", default=".")

    status = subcommands.add_parser(
        "status",
        help="write sidecar status report and print latest state",
    )
    status.add_argument("--repo", required=True)

    retention = subcommands.add_parser(
        "retention",
        help="inspect or apply local artifact retention policy",
    )
    retention.add_argument("--repo", required=True)
    retention.add_argument("--apply", action="store_true")
    retention.add_argument("--redact-output")

    ci = subcommands.add_parser(
        "ci",
        help="run CI readiness checks for manifests and harness health",
    )
    ci.add_argument("--repo", required=True)
    ci.add_argument("--max-instruction-lines", type=int, default=100)
    ci.add_argument("--candidate")
    ci.add_argument("--suite", default="all")

    index = subcommands.add_parser(
        "index",
        help="parse instruction files and optionally dry-run with --check",
    )
    index.add_argument("--repo", required=True)
    index.add_argument("--check", action="store_true")

    audit = subcommands.add_parser(
        "audit",
        help="ingest a trace and write audit evidence",
    )
    audit.add_argument("--repo", required=True)
    audit.add_argument("--trace", required=True)
    audit.add_argument(
        "--trace-format",
        choices=("auto", "generic-jsonl", "codex", "claude", "ci", "mcp"),
        default="auto",
    )
    audit.add_argument("--mock-llmff-inspect", action="store_true")

    propose = subcommands.add_parser(
        "propose",
        help="generate a bounded candidate from audit evidence",
    )
    propose.add_argument("--repo", required=True)
    propose.add_argument("--audit", required=True)

    evaluate = subcommands.add_parser(
        "eval",
        help="evaluate a candidate against a named suite",
    )
    evaluate.add_argument("--repo", required=True)
    evaluate.add_argument("--candidate", required=True)
    evaluate.add_argument("--suite", required=True)

    optimize = subcommands.add_parser(
        "optimize",
        help="run audit, propose, eval, and acceptance summary",
    )
    optimize.add_argument("--repo", required=True)
    optimize.add_argument("--trace", required=True)
    optimize.add_argument("--train-trace", action="append", default=[])
    optimize.add_argument("--suite", required=True)
    optimize.add_argument("--held-out-episode", action="append", default=[])
    optimize.add_argument("--unseen-suite", action="append", default=[])
    optimize.add_argument(
        "--trace-format",
        choices=("auto", "generic-jsonl", "codex", "claude", "ci", "mcp"),
        default="auto",
    )

    apply = subcommands.add_parser(
        "apply",
        description=(
            "Apply a reviewed candidate. proposal mode writes review artifacts without "
            "changing files. branch, commit, and pr modes require VCS safety checks. "
            "Write modes require policy gate, eval report, and rollback plan evidence."
        ),
        help="prepare or apply a reviewed candidate through VCS-gated modes",
    )
    apply.add_argument("--repo", required=True)
    apply.add_argument("--candidate", required=True)
    apply.add_argument("--mode", choices=("proposal", "branch", "commit", "pr"), default="proposal")
    apply.add_argument("--review-actor", default="tugboat")
    apply.add_argument("--human-review", action="store_true")
    apply.add_argument("--auto-apply", action="store_true")
    apply.add_argument("--confirm-auto-apply", action="store_true")
    apply.add_argument("--auto-apply-policy-version", type=int)

    auto_apply = subcommands.add_parser(
        "auto-apply",
        description=(
            "Evaluate narrow Class A auto-apply. It is disabled unless repo policy and "
            "CLI confirmation pass. preflight and shadow record evidence without "
            "applying patches. The read-only kill switch blocks writes."
        ),
        help="evaluate policy-gated auto-apply, preflight, or shadow evidence",
    )
    auto_apply.add_argument("--repo", required=True)
    auto_apply.add_argument("--candidate", required=True)
    auto_apply.add_argument("--confirm-auto-apply", action="store_true")
    auto_apply.add_argument("--auto-apply-policy-version", type=int)
    auto_apply.add_argument("--actor", required=True)
    auto_apply.add_argument("--preflight", action="store_true")
    auto_apply.add_argument("--shadow", action="store_true")

    review = subcommands.add_parser(
        "review",
        help="record reviewer decisions and rejected-edit memory",
    )
    review_subcommands = review.add_subparsers(dest="review_command", required=True)
    review_reject = review_subcommands.add_parser("reject")
    review_reject.add_argument("--repo", required=True)
    review_reject.add_argument("--candidate", required=True)
    review_reject.add_argument("--actor", required=True)
    review_reject.add_argument("--template")
    review_reject.add_argument("--reason")
    review_reject.add_argument("--category")
    review_reject.add_argument("--failure-pattern")

    rollback = subcommands.add_parser(
        "rollback",
        description=(
            "Prepare or execute rollback evidence. without --execute, rollback writes "
            "a reviewable plan. --execute performs the recorded VCS revert. read-only "
            "mode blocks execution."
        ),
        help="prepare or execute a recorded rollback",
    )
    rollback.add_argument("--repo", required=True)
    rollback.add_argument("--decision", required=True)
    rollback.add_argument("--execute", action="store_true")

    mcp = subcommands.add_parser(
        "mcp",
        help="serve read-first MCP tools for a bounded repo",
    )
    mcp_subcommands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_stdio = mcp_subcommands.add_parser("stdio")
    mcp_stdio.add_argument("--repo")
    mcp_stdio.add_argument("--read-only", action="store_true")

    daemon = subcommands.add_parser(
        "daemon",
        description=(
            "Run the local sidecar worker and inspect its queue. The read-only kill "
            "switch blocks write jobs."
        ),
        help="manage the local sidecar worker and read-only kill switch",
    )
    daemon_subcommands = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_status_parser = daemon_subcommands.add_parser("status")
    daemon_status_parser.add_argument("--repo", required=True)
    daemon_run_once = daemon_subcommands.add_parser("run-once")
    daemon_run_once.add_argument("--repo", required=True)
    daemon_run_once.add_argument("--worker-id", default="tugboat-daemon")
    daemon_run_once.add_argument("--lease-seconds", type=int, default=300)
    daemon_cycle = daemon_subcommands.add_parser("cycle")
    daemon_cycle.add_argument("--repo", required=True)
    daemon_cycle.add_argument("--worker-id", default="tugboat-daemon")
    daemon_cycle.add_argument("--lease-seconds", type=int, default=300)
    daemon_cycle.add_argument("--max-jobs", type=int, default=1)
    daemon_cycle.add_argument("--concurrency", type=int, default=1)
    daemon_cycle.add_argument("--rate-limit-window-seconds", type=int)
    daemon_cycle.add_argument("--rate-limit-max-jobs", type=int)
    daemon_cycle.add_argument("--trace-dir", action="append", default=[])
    daemon_cycle.add_argument("--cycles", type=int, default=1)
    daemon_cycle.add_argument("--interval-seconds", type=float, default=0.0)
    daemon_serve = daemon_subcommands.add_parser("serve")
    daemon_serve.add_argument("--repo", required=True)
    daemon_serve.add_argument("--worker-id", default="tugboat-daemon")
    daemon_serve.add_argument("--lease-seconds", type=int, default=300)
    daemon_serve.add_argument("--socket")
    daemon_serve.add_argument("--max-requests", type=int)
    daemon_read_only = daemon_subcommands.add_parser("read-only")
    daemon_read_only.add_argument("--repo", required=True)
    read_only_action = daemon_read_only.add_mutually_exclusive_group(required=True)
    read_only_action.add_argument("--enable", action="store_true")
    read_only_action.add_argument("--disable", action="store_true")
    read_only_action.add_argument("--status", action="store_true")
    daemon_profile = daemon_subcommands.add_parser("profile")
    daemon_profile.add_argument("--repo", required=True)
    daemon_profile.add_argument("--app-boot-json", required=True)
    daemon_profile.add_argument("--observability-ref", action="append", default=[])

    report = subcommands.add_parser(
        "report",
        help="write an operator report for a run",
    )
    report.add_argument("--repo", required=True)
    report.add_argument("--run", required=True)

    inspect_decision = subcommands.add_parser(
        "inspect-decision",
        help="write decision trace and print bounded review metadata",
    )
    inspect_decision.add_argument("--repo", required=True)
    inspect_decision.add_argument("--decision", required=True)
    inspect_decision.add_argument("--compare")

    harness = subcommands.add_parser(
        "harness",
        help="check instruction harness health and cleanup candidates",
    )
    harness_subcommands = harness.add_subparsers(dest="harness_command", required=True)
    harness_check = harness_subcommands.add_parser("check")
    harness_check.add_argument("--repo", required=True)
    harness_check.add_argument("--max-instruction-lines", type=int, default=100)
    harness_report = harness_subcommands.add_parser("report")
    harness_report.add_argument("--repo", required=True)
    harness_cleanup = harness_subcommands.add_parser("cleanup")
    harness_cleanup.add_argument("--repo", required=True)
    ops = subcommands.add_parser(
        "ops",
        description=(
            "Manage backup, restore, migration, observability, and release evidence. "
            "destructive operations require explicit execute or apply flags."
        ),
        help="run operational backup, migration, observability, restore, and release tasks",
    )
    ops_subcommands = ops.add_subparsers(dest="ops_command", required=True)
    ops_backup = ops_subcommands.add_parser(
        "backup",
        description="Create sidecar backup evidence; plans backup unless --execute is supplied.",
        help="plan or execute a sidecar backup",
    )
    ops_backup.add_argument("--repo", required=True)
    ops_backup.add_argument("--archive", required=True)
    ops_backup.add_argument("--execute", action="store_true")
    ops_migrate = ops_subcommands.add_parser(
        "migrate",
        description="Inspect sidecar schema migration state; dry-run migration unless --apply is supplied.",
        help="dry-run or apply sidecar schema migrations",
    )
    ops_migrate.add_argument("--repo", required=True)
    ops_migrate.add_argument("--apply", action="store_true")
    ops_observability = ops_subcommands.add_parser(
        "observability",
        description=(
            "write local operations observability artifacts. The summary JSON remains "
            "the review source of truth. --metrics-output writes Prometheus text "
            "metrics. --event-log-output writes JSONL operational events without raw "
            "trace payloads."
        ),
        help="write local operations observability artifacts",
    )
    ops_observability.add_argument("--repo", required=True)
    ops_observability.add_argument("--output")
    ops_observability.add_argument("--metrics-output")
    ops_observability.add_argument("--event-log-output")
    ops_release_manifest = ops_subcommands.add_parser("release-manifest")
    ops_release_manifest.add_argument("--repo", required=True)
    ops_release_manifest.add_argument("--wheel", required=True)
    ops_release_manifest.add_argument("--commit", required=True)
    ops_release_manifest.add_argument("--ci-url", required=True)
    ops_release_manifest.add_argument("--approver", required=True)
    ops_release_manifest.add_argument("--security-review-decision", required=True)
    ops_release_manifest.add_argument(
        "--security-review-critical-high-findings",
        required=True,
        type=int,
    )
    ops_release_manifest.add_argument("--evidence", action="append", default=[])
    ops_restore = ops_subcommands.add_parser(
        "restore",
        description="Create sidecar restore evidence; plans restore unless --execute is supplied.",
        help="plan or execute a sidecar restore",
    )
    ops_restore.add_argument("--repo", required=True)
    ops_restore.add_argument("--archive", required=True)
    ops_restore.add_argument("--staging", required=True)
    ops_restore.add_argument("--pre-restore", required=True)
    ops_restore.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        repo = Path(args.repo).resolve()
        try:
            return _print_doctor_report(repo)
        except (OSError, ValueError, YAMLError) as error:
            print(f"doctor blocked: policy invalid: {error}")
            print(f"recommendation: fix .sidecar/policy.yaml and rerun `tugboat doctor --repo {repo}`")
            return 1

    if args.command == "init":
        repo = Path(args.repo).resolve()
        if _sidecar_schema_preflight_blocked("init", repo):
            return 1
        try:
            policy_path = _initialize_repo_policy(repo)
        except FileExistsError as error:
            print(f"init blocked: {error}")
            return 1
        print(f"initialized: {policy_path.relative_to(repo).as_posix()}")
        return 0

    if args.command == "status":
        repo = Path(args.repo)
        if _policy_preflight_blocked("status", repo):
            return 1
        policy = load_policy(repo)
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            latest = store.connection.execute(
                "SELECT id, stage, status FROM runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            latest_llmff = None
            latest_failure_kind = None
            if latest is not None:
                latest_llmff = store.connection.execute(
                    """
                    SELECT id, manifest_name, status, exit_code
                    FROM llmff_jobs
                    WHERE run_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (latest[0],),
                ).fetchone()
                if latest_llmff is not None:
                    latest_failure_kind = _latest_llmff_failure_kind(store, int(latest_llmff[0]))
            pending_candidates = int(
                store.connection.execute(
                    "SELECT COUNT(*) FROM candidates WHERE state = 'needs_review'"
                ).fetchone()[0]
            )
            indexed_documents = store.count("documents")
        try:
            retention = apply_retention_policy(repo, policy, dry_run=True)
        except RetentionScanBudgetExceeded as error:
            print(f"status blocked: {error}")
            return 1
        manifest_policy = (
            f"pinned {len(policy.allowed_manifest_hashes)}"
            if policy.allowed_manifest_hashes
            else "unrestricted"
        )
        status_payload = {
            "schema_version": SCHEMA_VERSION,
            "mode": policy.mode,
            "auto_apply": "enabled" if policy.auto_apply_enabled else "disabled",
            "indexed_documents": indexed_documents,
            "latest_run": (
                {"run_id": str(latest[0]), "stage": str(latest[1]), "status": str(latest[2])}
                if latest
                else None
            ),
            "latest_llmff_job": (
                {"manifest_name": str(latest_llmff[1]), "status": str(latest_llmff[2])}
                if latest_llmff
                else None
            ),
            "latest_llmff_exit_code": (
                int(latest_llmff[3])
                if latest_llmff is not None and latest_llmff[3] is not None
                else None
            ),
            "latest_llmff_failure_kind": latest_failure_kind,
            "pending_candidates": pending_candidates,
            "retention_candidates": len(retention.candidates),
            "retention_redaction_candidates": len(retention.redaction_candidates),
            "manifest_policy": manifest_policy,
        }
        validate_json_artifact("status-report.json", status_payload)
        status_report_path = write_json_artifact(sidecar_dir(repo) / "status-report.json", status_payload)
        print(f"mode: {policy.mode}")
        print(f"auto_apply: {'enabled' if policy.auto_apply_enabled else 'disabled'}")
        print(f"indexed_documents: {indexed_documents}")
        print(f"latest_run: {latest[1]} {latest[2]}" if latest else "latest_run: none")
        if latest_llmff is None:
            print("latest_llmff_job: none")
            print("latest_llmff_exit_code: none")
        else:
            print(f"latest_llmff_job: {latest_llmff[1]} {latest_llmff[2]}")
            print(f"latest_llmff_exit_code: {latest_llmff[3] if latest_llmff[3] is not None else 'none'}")
        print(f"latest_llmff_failure_kind: {latest_failure_kind or 'none'}")
        print(f"pending_candidates: {pending_candidates}")
        print(f"retention_candidates: {len(retention.candidates)}")
        print(f"retention_redaction_candidates: {len(retention.redaction_candidates)}")
        print(f"manifest_policy: {manifest_policy}")
        print(f"status_report: {status_report_path}")
        return 0

    if args.command == "retention":
        repo = Path(args.repo)
        if _policy_preflight_blocked("retention", repo):
            return 1
        policy = load_policy(repo)
        redaction_export = None
        if args.redact_output is not None:
            if _write_blocked_by_read_only(repo, "redaction"):
                return 1
            try:
                result = apply_retention_policy(repo, policy, dry_run=True)
                redaction_export = export_redacted_artifacts(
                    repo,
                    Path(args.redact_output),
                    scan_file_budget=policy.retention_scan_file_budget,
                )
            except ValueError as error:
                print(f"redaction blocked: {error}")
                return 1
            report_path = _write_retention_report(
                repo,
                mode="dry-run",
                status="complete",
                candidates=result.candidates,
                deleted=result.deleted,
                redaction_candidates=result.redaction_candidates,
            )
        elif args.apply:
            if _write_blocked_by_read_only(repo, "retention"):
                return 1
            try:
                preflight = apply_retention_policy(repo, policy, dry_run=True)
            except RetentionScanBudgetExceeded as error:
                print(f"retention blocked: {error}")
                return 1
            report_path = _write_retention_report(
                repo,
                mode="apply",
                status="planned",
                candidates=preflight.candidates,
                deleted=(),
                redaction_candidates=preflight.redaction_candidates,
            )
            try:
                result = apply_retention_policy(repo, policy, dry_run=False)
            except RetentionScanBudgetExceeded as error:
                print(f"retention blocked: {error}")
                return 1
            report_path = _write_retention_report(
                repo,
                mode="apply",
                status="complete",
                candidates=result.candidates,
                deleted=result.deleted,
                redaction_candidates=result.redaction_candidates,
            )
        else:
            try:
                result = apply_retention_policy(repo, policy, dry_run=True)
            except RetentionScanBudgetExceeded as error:
                print(f"retention blocked: {error}")
                return 1
            report_path = _write_retention_report(
                repo,
                mode="dry-run",
                status="complete",
                candidates=result.candidates,
                deleted=result.deleted,
                redaction_candidates=result.redaction_candidates,
            )
        mode = "redact" if redaction_export is not None else "apply" if args.apply else "dry-run"
        print(f"retention_mode: {mode}")
        print(f"candidates: {len(result.candidates)}")
        print(f"deleted: {len(result.deleted)}")
        print(f"redaction_candidates: {len(result.redaction_candidates)}")
        print(f"retention_report: {report_path}")
        if redaction_export is not None:
            print(f"redacted_export: {redaction_export.output_dir}")
        for candidate in result.candidates:
            print(f"candidate: {candidate}")
        for deleted in result.deleted:
            print(f"deleted: {deleted}")
        for candidate in result.redaction_candidates:
            print(
                "redaction_candidate: "
                f"{candidate['path']}:{candidate['line_number']}:{candidate['kind']}"
            )
        return 0

    if args.command == "ci":
        repo = Path(args.repo).resolve()
        if _sidecar_schema_preflight_blocked("ci", repo):
            return 1
        try:
            report_path, payload = _write_ci_report(
                repo,
                max_instruction_lines=args.max_instruction_lines,
                candidate=args.candidate,
                suite=args.suite,
            )
        except (InstructionIndexBudgetExceeded, SecretScanError) as error:
            print(f"ci blocked: {error}")
            return 1
        harness = payload["checks"]["harness"]
        eval_check = payload["checks"].get("eval")
        if _ci_payload_passed(payload):
            print("ci: ok")
            print(f"report: {report_path}")
            return 0
        print("ci: failed")
        for finding in harness["findings"]:
            print(finding)
        manifest_contracts = payload["checks"]["manifest_contracts"]
        if not manifest_contracts["passed"]:
            print("manifest contract validation failed")
            for finding in manifest_contracts["findings"]:
                print(finding)
        semantic_lint = payload["checks"]["semantic_policy_lint"]
        if not semantic_lint["passed"]:
            print("semantic policy lint failed")
            for finding in semantic_lint["findings"]:
                print(finding)
        harness_report = payload["checks"]["harness_report"]
        if not harness_report["passed"]:
            print("harness report failed")
            for finding in harness_report["doc_gardening_tasks"]:
                print(finding)
        if eval_check is not None and not eval_check["passed"]:
            print(f"eval suite {eval_check['suite_id']} failed")
        print(f"report: {report_path}")
        return 1

    if args.command == "index":
        repo = Path(args.repo)
        if _policy_preflight_blocked("index", repo):
            return 1
        try:
            result = index_repo(repo, load_policy(repo))
        except InstructionIndexBudgetExceeded as error:
            print(f"index blocked: {error}")
            return 1
        if not args.check:
            with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
                store.index_documents(repo, result)
        print(f"indexed documents: {result.indexed_count}")
        return 0

    if args.command == "audit":
        repo = Path(args.repo)
        if _policy_preflight_blocked("audit", repo):
            return 1
        result = run_audit_pipeline(
            repo,
            Path(args.trace),
            trace_format=args.trace_format,
            mock_llmff_inspect=args.mock_llmff_inspect,
        )
        print(result.message)
        if result.exit_code != 0:
            _print_trace_blocked_next_step(Path(args.trace), result.message)
        return result.exit_code

    if args.command == "propose":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("propose", repo):
            return 1
        try:
            result = run_propose_pipeline(repo, args.audit)
        except (
            ArtifactValidationError,
            FileNotFoundError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            print(f"propose blocked: {error}")
            return 1
        print(result.message)
        return result.exit_code

    if args.command == "eval":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("eval", repo):
            return 1
        try:
            result = run_eval_pipeline(repo, args.candidate, args.suite)
        except (
            ArtifactValidationError,
            FileNotFoundError,
            json.JSONDecodeError,
            ValueError,
        ) as error:
            print(f"eval blocked: {error}")
            return 1
        print(result.message)
        try:
            return _finalize_governed_candidate_evaluation(
                repo,
                result.run_dir,
                suite_id=args.suite,
                eval_exit_code=result.exit_code,
            )
        except (ArtifactValidationError, KeyError, ValueError) as error:
            print(f"eval blocked: {error}")
            return 1

    if args.command == "optimize":
        repo = Path(args.repo)
        if _policy_preflight_blocked("optimize", repo):
            return 1
        result = run_optimize_workflow(
            repo,
            Path(args.trace),
            suite_id=args.suite,
            train_traces=tuple(Path(trace) for trace in args.train_trace),
            held_out_episodes=tuple(args.held_out_episode),
            unseen_suites=tuple(args.unseen_suite),
            trace_format=args.trace_format,
        )
        return result.exit_code

    if args.command == "apply":
        repo = Path(args.repo)
        if _write_blocked_by_read_only(repo, "apply"):
            return 1
        if _sidecar_schema_preflight_blocked("apply", repo):
            return 1
        run_dir = latest_run_dir(repo) if args.candidate == "latest" else runs_dir(repo) / args.candidate
        try:
            apply_path = _write_apply_plan(
                repo,
                run_dir,
                mode=args.mode,
                review_actor=args.review_actor,
                human_review=args.human_review,
                auto_apply=args.auto_apply,
                confirm_auto_apply=args.confirm_auto_apply,
                auto_apply_policy_version=args.auto_apply_policy_version,
            )
        except (FileNotFoundError, KeyError, VcsStateError, ValueError) as error:
            print(f"apply blocked: {error}")
            _print_apply_blocked_next_step(repo, run_dir, str(error))
            return 1
        print(f"apply plan: {apply_path}")
        return 0

    if args.command == "auto-apply":
        repo = Path(args.repo)
        if _write_blocked_by_read_only(repo, "auto-apply"):
            return 1
        if _sidecar_schema_preflight_blocked("auto-apply", repo):
            return 1
        run_dir = latest_run_dir(repo) if args.candidate == "latest" else runs_dir(repo) / args.candidate
        if args.preflight and args.shadow:
            print("auto-apply blocked: choose only one of --preflight or --shadow")
            return 1
        if args.preflight:
            try:
                preflight_path = _write_auto_apply_preflight(
                    repo,
                    run_dir,
                    review_actor=args.actor,
                    confirmed=args.confirm_auto_apply,
                    policy_version=args.auto_apply_policy_version,
                )
            except (FileNotFoundError, KeyError, VcsStateError, ValueError) as error:
                print(f"auto-apply preflight blocked: {error}")
                return 1
            print(f"auto-apply preflight: {preflight_path}")
            return 0
        if args.shadow:
            try:
                shadow_path = _write_auto_apply_shadow(
                    repo,
                    run_dir,
                    review_actor=args.actor,
                    confirmed=args.confirm_auto_apply,
                    policy_version=args.auto_apply_policy_version,
                )
            except (FileNotFoundError, KeyError, VcsStateError, ValueError) as error:
                print(f"auto-apply shadow blocked: {error}")
                return 1
            print(f"auto-apply shadow: {shadow_path}")
            return 0
        try:
            apply_path = _write_apply_plan(
                repo,
                run_dir,
                mode="commit",
                review_actor=args.actor,
                human_review=False,
                auto_apply=True,
                confirm_auto_apply=args.confirm_auto_apply,
                auto_apply_policy_version=args.auto_apply_policy_version,
            )
        except (FileNotFoundError, KeyError, VcsStateError, ValueError) as error:
            print(f"auto-apply blocked: {error}")
            return 1
        print(f"auto-apply plan: {apply_path}")
        return 0

    if args.command == "review" and args.review_command == "reject":
        repo = Path(args.repo)
        if _write_blocked_by_read_only(repo, "review rejection"):
            return 1
        if _sidecar_schema_preflight_blocked("review", repo):
            return 1
        run_dir = latest_run_dir(repo) if args.candidate == "latest" else runs_dir(repo) / args.candidate
        try:
            _write_human_review_rejection(
                repo,
                run_dir,
                actor=args.actor,
                template=args.template,
                reason=args.reason,
                category=args.category,
                failure_pattern=args.failure_pattern,
            )
        except (FileNotFoundError, KeyError, ValueError, SecretScanError) as error:
            print(f"review blocked: {error}")
            return 1
        print("review: rejected")
        return 0

    if args.command == "rollback":
        repo = Path(args.repo)
        if args.execute and _write_blocked_by_read_only(repo, "rollback"):
            return 1
        if _sidecar_schema_preflight_blocked("rollback", repo):
            return 1
        run_dir = latest_run_dir(repo) if args.decision == "latest" else runs_dir(repo) / args.decision
        try:
            rollback_path = _write_rollback_plan(repo, run_dir, execute=args.execute)
        except (FileNotFoundError, KeyError, VcsStateError, ValueError) as error:
            print(f"rollback blocked: {error}")
            return 1
        print(f"rollback plan: {rollback_path}")
        return 0

    if args.command == "mcp" and args.mcp_command == "stdio":
        repo = Path(args.repo) if args.repo else None
        if repo is not None and not args.read_only and _sidecar_schema_preflight_blocked("mcp", repo):
            return 1
        return run_stdio_server(
            sys.stdin,
            sys.stdout,
            repo=repo,
            read_only=args.read_only,
        )

    if args.command == "daemon" and args.daemon_command == "status":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("daemon status", repo):
            return 1
        status = daemon_status(repo, kill_switch=default_kill_switch(repo))
        print(f"queue_path: {status['queue_path']}")
        print(f"kill_switch_enabled: {str(status['kill_switch_enabled']).lower()}")
        for state, count in sorted(status["jobs_by_state"].items()):
            print(f"{state}: {count}")
        print(f"oldest_queued_job_id: {status['oldest_queued_job_id']}")
        print(f"leased_job_count: {status.get('leased_job_count', 0)}")
        print(f"stuck_job_count: {status.get('stuck_job_count', 0)}")
        print(f"oldest_stuck_job_id: {status.get('oldest_stuck_job_id')}")
        print(f"oldest_stuck_lease_expires_at: {status.get('oldest_stuck_lease_expires_at')}")
        if status.get("recovery_hint"):
            print(f"recovery_hint: {status['recovery_hint']}")
        return 0

    if args.command == "daemon" and args.daemon_command == "run-once":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("daemon run-once", repo):
            return 1
        result = run_daemon_once(
            repo,
            DaemonRunConfig(
                worker_id=args.worker_id,
                lease_duration=timedelta(seconds=args.lease_seconds),
                kill_switch=default_kill_switch(repo),
            ),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "daemon" and args.daemon_command == "profile":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("daemon profile", repo):
            return 1
        try:
            app_boot = _parse_profile_app_boot(args.app_boot_json)
            observability_refs = _validate_observability_refs(args.observability_ref)
            profile_path = write_worktree_profile(
                repo,
                app_boot=app_boot,
                observability_refs=observability_refs,
            )
        except ValueError as error:
            print(f"daemon profile blocked: {error}")
            return 1
        print(f"worktree_profile: {profile_path}")
        return 0

    if args.command == "daemon" and args.daemon_command == "cycle":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("daemon cycle", repo):
            return 1
        trace_dirs = (
            tuple(Path(trace_dir) for trace_dir in args.trace_dir)
            if args.trace_dir
            else tuple(default_trace_dirs(repo))
        )
        if (args.rate_limit_window_seconds is None) != (args.rate_limit_max_jobs is None):
            raise ValueError(
                "rate-limit-window-seconds and rate-limit-max-jobs must be provided together"
            )
        config = DaemonLoopConfig(
            worker_id=args.worker_id,
            max_jobs_per_cycle=args.max_jobs,
            concurrency_limit=args.concurrency,
            lease_duration=timedelta(seconds=args.lease_seconds),
            trace_dirs=trace_dirs,
            rate_limit_window=(
                timedelta(seconds=args.rate_limit_window_seconds)
                if args.rate_limit_window_seconds is not None
                else None
            ),
            max_jobs_per_rate_window=args.rate_limit_max_jobs,
            kill_switch=default_kill_switch(repo),
        )
        if args.cycles < 1:
            raise ValueError("cycles must be at least 1")
        if args.interval_seconds < 0:
            raise ValueError("interval_seconds must be non-negative")
        if args.cycles == 1:
            result = run_daemon_cycle(repo, config)
        else:
            result = run_daemon_loop(
                repo,
                config,
                cycles=args.cycles,
                interval_seconds=args.interval_seconds,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "daemon" and args.daemon_command == "serve":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("daemon serve", repo):
            return 1
        socket_path = Path(args.socket) if args.socket else sidecar_dir(repo) / "daemon.sock"
        try:
            result = serve_daemon_socket(
                repo,
                socket_path=socket_path,
                config=DaemonRunConfig(
                    worker_id=args.worker_id,
                    lease_duration=timedelta(seconds=args.lease_seconds),
                    kill_switch=default_kill_switch(repo),
                ),
                max_requests=args.max_requests,
            )
        except ValueError as error:
            print(f"daemon serve blocked: {error}")
            return 1
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "daemon" and args.daemon_command == "read-only":
        repo = Path(args.repo)
        kill_switch = default_kill_switch(repo)
        if args.disable and _sidecar_schema_preflight_blocked("daemon read-only", repo):
            return 1
        if args.enable:
            kill_switch.path.parent.mkdir(parents=True, exist_ok=True)
            kill_switch.path.write_text("enabled\n", encoding="utf-8")
        if args.disable:
            kill_switch.path.unlink(missing_ok=True)
        print(f"kill_switch_path: {kill_switch.path.relative_to(repo).as_posix()}")
        print(f"kill_switch_enabled: {str(kill_switch.is_enabled()).lower()}")
        return 0

    if args.command == "report":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("report", repo):
            return 1
        try:
            run_dir = latest_run_dir(repo) if args.run == "latest" else runs_dir(repo) / args.run
            candidate = _candidate_from_artifacts(run_dir)
        except FileNotFoundError as error:
            if error.filename:
                print(f"report blocked: missing artifact: {Path(error.filename).name}")
            else:
                print(f"report blocked: {error}")
            return 1
        except ArtifactValidationError as error:
            print(f"report blocked: {error}")
            return 1
        except KeyError as error:
            print(f"report blocked: candidate.json missing required field: {error.args[0]}")
            return 1
        except ValueError as error:
            print(f"report blocked: candidate.json invalid: {error}")
            return 1
        try:
            decision = _decision_from_artifact(run_dir)
        except FileNotFoundError as error:
            print(f"report blocked: missing artifact: {Path(error.filename).name}")
            return 1
        except ArtifactValidationError as error:
            print(f"report blocked: {error}")
            return 1
        except KeyError as error:
            print(f"report blocked: policy-gate.json missing required field: {error.args[0]}")
            return 1
        try:
            report_path = write_report(
                repo,
                run_dir.name,
                candidate=candidate,
                decision=decision,
                eval_report_path=run_dir / "eval-report.json",
            )
        except (
            ArtifactValidationError,
            FileNotFoundError,
            SecretScanError,
            ValueError,
        ) as error:
            print(f"report blocked: {error}")
            return 1
        print(f"report: {report_path}")
        return 0

    if args.command == "inspect-decision":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("inspect decision", repo):
            return 1
        try:
            trace_path = write_decision_trace(repo, args.decision)
        except (FileNotFoundError, KeyError, ValueError, SecretScanError) as error:
            print(f"inspect decision blocked: {error}")
            return 1
        print(f"decision_trace: {trace_path}")
        try:
            _print_decision_inspection_summary(trace_path)
        except (ArtifactValidationError, KeyError, ValueError) as error:
            print(f"inspect decision blocked: {error}")
            return 1
        if args.compare:
            try:
                compare_trace_path = write_decision_trace(repo, args.compare)
            except (FileNotFoundError, KeyError, ValueError, SecretScanError) as error:
                print(f"inspect decision blocked: {error}")
                return 1
            print(f"compare_decision_trace: {compare_trace_path}")
            _print_decision_comparison_summary(trace_path, compare_trace_path)
        return 0

    if args.command == "harness" and args.harness_command == "check":
        try:
            result = check_harness_legibility(Path(args.repo), args.max_instruction_lines)
        except InstructionIndexBudgetExceeded as error:
            print(f"harness blocked: {error}")
            return 1
        if result.passed:
            print("harness: ok")
            return 0
        for finding in result.findings:
            print(finding)
        return 1

    if args.command == "harness" and args.harness_command == "report":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("harness report", repo):
            return 1
        try:
            report = generate_harness_report(repo)
        except InstructionIndexBudgetExceeded as error:
            print(f"harness blocked: {error}")
            return 1
        try:
            _persist_harness_report(repo, report)
        except (ArtifactValidationError, SecretScanError) as error:
            print(f"harness report invalid: {error}")
            return 1
        print("# Tugboat Harness Report")
        print("## Knowledge Map")
        for source, targets in report.knowledge_map.items():
            for target in targets:
                print(f"{source} -> {target}")
        print("## Missing Docs")
        for item in report.missing_docs:
            print(f"- {item}")
        print("## Stale Docs")
        for item in report.stale_docs:
            print(f"- {item}")
        print("## Orphaned Runbooks")
        for item in report.orphaned_runbooks:
            print(f"- {item}")
        print("## Recurring Failures Without Docs")
        for item in report.recurring_failures_without_docs:
            print(f"- {item}")
        print("## Doc Gardening Tasks")
        for item in report.doc_gardening_tasks:
            print(f"- {item}")
        print("## Token Efficiency")
        token_metrics = report.token_metrics
        print(
            "instruction_corpus_estimated_tokens: "
            f"{token_metrics['instruction_corpus_estimated_tokens']}"
        )
        print(
            "active_context_estimated_tokens: "
            f"{token_metrics['active_context_estimated_tokens']}"
        )
        print(
            "retrieval_pack_file_count: "
            f"{token_metrics['retrieval_pack_file_count']}"
        )
        print(
            "retrieval_pack_estimated_tokens: "
            f"{token_metrics['retrieval_pack_estimated_tokens']}"
        )
        print(
            "duplicate_rule_estimated_tokens: "
            f"{token_metrics['duplicate_rule_estimated_tokens']}"
        )
        print(
            "duplicate_rule_token_budget: "
            f"{token_metrics['token_budget']['duplicate_rule_estimated_tokens']}"
        )
        for violation in token_metrics["token_budget_violations"]:
            print(f"token_budget_violation: {violation}")
        return 0

    if args.command == "harness" and args.harness_command == "cleanup":
        repo = Path(args.repo)
        if _write_blocked_by_read_only(repo, "cleanup"):
            return 1
        if _sidecar_schema_preflight_blocked("cleanup", repo):
            return 1
        try:
            report = generate_harness_report(repo)
        except InstructionIndexBudgetExceeded as error:
            print(f"harness blocked: {error}")
            return 1
        _persist_harness_report(repo, report)
        candidates = generate_cleanup_candidates(repo)
        structural_eval = run_cleanup_structural_eval(repo, candidates)
        if not structural_eval["passed"]:
            print("cleanup candidates blocked: structural eval failed")
            return 1
        path = sidecar_dir(repo) / "harness-cleanup-candidates.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "structural_eval": structural_eval,
            "candidates": [candidate.to_json_dict() for candidate in candidates],
        }
        try:
            validate_json_artifact("harness-cleanup-candidates.json", payload)
        except ArtifactValidationError as error:
            print(f"cleanup candidates invalid: {error}")
            return 1
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            _write_harness_cleanup_proposals(
                repo,
                candidates,
                structural_eval=structural_eval,
                bundle_path=path,
            )
        except ArtifactValidationError as error:
            print(f"cleanup proposals invalid: {error}")
            return 1
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            for candidate in candidates:
                store.record_harness_finding(
                    repo_path=repo,
                    finding=json.dumps(candidate.to_json_dict(), sort_keys=True),
                    severity="cleanup_candidate",
                )
        print(f"cleanup candidates: {path}")
        return 0

    if args.command == "ops" and args.ops_command == "observability":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("observability", repo):
            return 1
        output_path = (
            Path(args.output)
            if args.output
            else sidecar_dir(repo) / "ops" / "observability" / "summary.json"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "summary": summarize_sidecar_observability(repo),
        }
        validate_json_artifact("observability-summary.json", payload)
        output_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if args.metrics_output:
            metrics_path = Path(args.metrics_output)
            metrics_path.parent.mkdir(parents=True, exist_ok=True)
            metrics_path.write_text(
                observability_metrics_text(payload["summary"]),
                encoding="utf-8",
            )
            print(f"observability metrics: {metrics_path}")
        if args.event_log_output:
            event_log_path = Path(args.event_log_output)
            event_log_path.parent.mkdir(parents=True, exist_ok=True)
            event_log_path.write_text(
                observability_event_log_text(
                    payload["summary"],
                    source="ops.observability",
                    repo=str(repo.resolve()),
                ),
                encoding="utf-8",
            )
            print(f"observability events: {event_log_path}")
        print(f"observability summary: {output_path}")
        return 0

    if args.command == "ops" and args.ops_command == "release-manifest":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("release manifest", repo):
            return 1
        try:
            output_path = _write_release_artifact_manifest(
                repo=repo,
                wheel_path=Path(args.wheel),
                commit=args.commit,
                ci_url=args.ci_url,
                approver=args.approver,
                security_review_decision=args.security_review_decision,
                security_review_critical_high_findings=(
                    args.security_review_critical_high_findings
                ),
                evidence_paths=[Path(path) for path in args.evidence],
            )
        except (FileNotFoundError, ValueError, ArtifactValidationError) as error:
            print(f"release manifest blocked: {error}")
            return 1
        print(f"release manifest: {output_path}")
        return 0

    if args.command == "ops" and args.ops_command == "migrate":
        repo = Path(args.repo)
        if args.apply and _write_blocked_by_read_only(repo, "migration"):
            return 1
        try:
            plan = execute_migration_plan(repo) if args.apply else dry_run_migration_plan(repo)
        except (ValueError, json.JSONDecodeError, OSError) as error:
            print(f"migration blocked: {error}")
            return 1
        print(f"migration_mode: {'apply' if args.apply else 'dry-run'}")
        print(f"current_version: {plan.current_version}")
        print(f"target_version: {plan.target_version}")
        for step in plan.steps:
            print(f"step: {step.migration_id} {step.from_version}->{step.to_version}")
        if plan.report_path is not None:
            print(f"migration_report: {plan.report_path}")
        return 0

    if args.command == "ops" and args.ops_command == "backup":
        repo = Path(args.repo)
        if _sidecar_schema_preflight_blocked("backup plan", repo):
            return 1
        try:
            bundle = build_sidecar_backup_bundle(repo=repo, archive_path=Path(args.archive))
            archive_path = (
                execute_sidecar_backup(repo=repo, archive_path=Path(args.archive))
                if args.execute
                else None
            )
        except ValueError as error:
            print(f"backup blocked: {error}" if args.execute else f"backup plan blocked: {error}")
            return 1
        path = sidecar_dir(repo) / "ops" / "backup-plan.json"
        _write_ops_command_bundle(path, bundle.to_dict())
        if archive_path is not None:
            print(f"backup archive: {archive_path}")
        else:
            print(f"backup plan: {path}")
        return 0

    if args.command == "ops" and args.ops_command == "restore":
        repo = Path(args.repo)
        if args.execute and _write_blocked_by_read_only(repo, "restore"):
            return 1
        if _sidecar_schema_preflight_blocked("restore plan", repo):
            return 1
        try:
            bundle = build_sidecar_restore_bundle(
                repo=repo,
                archive_path=Path(args.archive),
                staging_path=Path(args.staging),
                pre_restore_path=Path(args.pre_restore),
            )
            restored_path = (
                execute_sidecar_restore(
                    repo=repo,
                    archive_path=Path(args.archive),
                    staging_path=Path(args.staging),
                    pre_restore_path=Path(args.pre_restore),
                )
                if args.execute
                else None
            )
        except ValueError as error:
            print(f"restore blocked: {error}" if args.execute else f"restore plan blocked: {error}")
            return 1
        path = sidecar_dir(repo) / "ops" / "restore-plan.json"
        _write_ops_command_bundle(path, bundle.to_dict())
        if restored_path is not None:
            print(f"restored sidecar: {restored_path}")
        else:
            print(f"restore plan: {path}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


def run_cleanup_structural_eval(repo: Path, candidates: Sequence[object]) -> dict[str, object]:
    candidate_ids: list[str] = []
    candidate_hashes: dict[str, str] = {}
    findings: list[str] = []
    for index, candidate in enumerate(candidates):
        candidate_payload = candidate.to_json_dict()
        candidate_id = str(candidate_payload.get("candidate_id", f"candidate-{index + 1}"))
        candidate_ids.append(candidate_id)
        encoded_candidate = json.dumps(candidate_payload, sort_keys=True).encode("utf-8")
        candidate_hashes[candidate_id] = hashlib.sha256(encoded_candidate).hexdigest()
        if candidate_payload.get("auto_apply") is not False:
            findings.append(f"{candidate_id}: cleanup candidates must remain review-only")
        if candidate_payload.get("risk_class") != "review_required":
            findings.append(f"{candidate_id}: cleanup candidates must be review_required")
        eval_suites = candidate_payload.get("required_eval_suites", [])
        if not isinstance(eval_suites, list) or "structural" not in eval_suites:
            findings.append(f"{candidate_id}: structural eval suite is required")
        source_findings = candidate_payload.get("source_findings", [])
        if not isinstance(source_findings, list) or not source_findings:
            findings.append(f"{candidate_id}: source findings are required")
    return {
        "suite_id": "structural",
        "runner": "harness-cleanup-structural",
        "passed": not findings,
        "candidate_count": len(candidates),
        "evaluated_candidates": candidate_ids,
        "candidate_hashes": candidate_hashes,
        "findings": findings,
    }


def _write_harness_cleanup_proposals(
    repo: Path,
    candidates: Sequence[object],
    *,
    structural_eval: dict[str, object],
    bundle_path: Path,
) -> None:
    proposal_dir = sidecar_dir(repo) / "harness-cleanup-proposals"
    proposal_dir.mkdir(parents=True, exist_ok=True)
    candidate_hashes = structural_eval.get("candidate_hashes", {})
    if not isinstance(candidate_hashes, dict):
        candidate_hashes = {}
    for index, candidate in enumerate(candidates):
        candidate_payload = candidate.to_json_dict()
        candidate_id = str(candidate_payload.get("candidate_id", f"candidate-{index + 1}"))
        proposal = {
            "schema_version": SCHEMA_VERSION,
            "kind": "cleanup_proposal",
            "candidate_id": candidate_id,
            "state": "waiting_review",
            "auto_apply": candidate_payload.get("auto_apply"),
            "risk_class": candidate_payload.get("risk_class"),
            "task": candidate_payload.get("task"),
            "source_findings": candidate_payload.get("source_findings"),
            "required_eval_suites": candidate_payload.get("required_eval_suites"),
            "structural_eval": {
                "bundle": _relative_repo_path(repo, bundle_path),
                "candidate_hash": str(candidate_hashes.get(candidate_id, "")),
                "suite_id": structural_eval.get("suite_id", "structural"),
            },
        }
        validate_json_artifact("harness-cleanup-proposal.json", proposal)
        (proposal_dir / f"{candidate_id}.json").write_text(
            json.dumps(proposal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _write_ops_command_bundle(path: Path, bundle: dict[str, object]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "bundle": bundle,
    }
    validate_json_artifact("ops-command-bundle.json", payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_release_artifact_manifest(
    *,
    repo: Path,
    wheel_path: Path,
    commit: str,
    ci_url: str,
    approver: str,
    security_review_decision: str,
    security_review_critical_high_findings: int,
    evidence_paths: Sequence[Path],
) -> Path:
    resolved_repo = repo.resolve()
    resolved_wheel = wheel_path.resolve()
    if not resolved_wheel.exists():
        raise FileNotFoundError("wheel does not exist")
    if not resolved_wheel.is_file():
        raise ValueError("wheel must be a file")
    if security_review_critical_high_findings < 0:
        raise ValueError("security review critical/high findings must be non-negative")
    if security_review_critical_high_findings > 0:
        raise ValueError("security review has open critical/high findings")
    if security_review_decision not in {"approved_proposal_only", "approved_provider_backed"}:
        raise ValueError("security review decision is not approved")
    if not evidence_paths:
        raise ValueError("retained evidence is required")
    retained_evidence = []
    for evidence_path in evidence_paths:
        resolved_evidence = evidence_path.resolve()
        if not resolved_evidence.exists():
            raise FileNotFoundError(f"evidence does not exist: {evidence_path}")
        if not resolved_evidence.is_file():
            raise ValueError(f"evidence must be a file: {evidence_path}")
        try:
            scan_path(resolved_evidence)
        except SecretScanError as error:
            findings = ", ".join(
                f"{Path(finding.path).name}:{finding.line_number}:{finding.kind}"
                for finding in error.findings
            )
            raise ValueError(f"retained evidence contains secret: {findings}") from error
        _validate_release_evidence_content(resolved_evidence, wheel_filename=resolved_wheel.name)
        retained_evidence.append(_file_manifest_entry(resolved_evidence))
    missing_release_evidence = _missing_release_evidence(retained_evidence)
    if missing_release_evidence is not None:
        raise ValueError(f"{missing_release_evidence} evidence is required")
    provider_backed_evidence: list[dict[str, object]] = []
    if security_review_decision == "approved_provider_backed":
        provider_backed_evidence = _provider_backed_release_evidence(retained_evidence)
        if not provider_backed_evidence:
            raise ValueError("provider-backed release evidence is required")
        policy = load_policy(resolved_repo)
        if not policy.llmff_allow_network:
            raise ValueError(
                "provider-backed release requires llmff.allow_network and allowed_providers"
            )
        allowed_providers = set(policy.llmff_allowed_providers)
        if not allowed_providers:
            raise ValueError(
                "provider-backed release requires llmff.allow_network and allowed_providers"
            )
        for evidence in provider_backed_evidence:
            evidence_providers = set(evidence["providers"])
            if not allowed_providers.issuperset(evidence_providers):
                provider = next(
                    provider for provider in evidence_providers if provider not in allowed_providers
                )
                raise ValueError(
                    f"provider-backed release evidence uses unallowed provider: {provider}"
                )
    preflight_findings = scan_text(
        "release-artifact-manifest.json",
        json.dumps(
            {
                "approver": approver,
                "ci_url": ci_url,
                "commit": commit,
                "security_review_decision": security_review_decision,
            },
            sort_keys=True,
        ),
    )
    if preflight_findings:
        raise SecretScanError(preflight_findings)
    current_head = _current_git_head(resolved_repo)
    if commit != current_head:
        raise ValueError(f"commit does not match current HEAD: {current_head}")

    payload = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "release_artifact_manifest",
        "package": _project_package_metadata(resolved_repo),
        "commit": commit,
        "ci_url": ci_url,
        "approver": approver,
        "security_review": {
            "decision": security_review_decision,
            "critical_high_findings": security_review_critical_high_findings,
        },
        "wheel": _file_manifest_entry(resolved_wheel),
        "smoke_commands": [
            "tugboat doctor",
            "tugboat index --repo . --check",
            "tugboat harness check --repo .",
            "tugboat ci --repo .",
            "python -m pytest --cov=src --cov-report=term-missing -q",
            "python -m build --wheel",
            "python -m twine check dist/<wheel>.whl",
            "clean venv install from built wheel",
            "installed tugboat doctor",
            "installed tugboat index --repo . --check",
            "installed tugboat harness check --repo .",
            "installed tugboat optimize --repo .sidecar/ci/proposal-smoke-repo --trace tests/fixtures/traces/codex-local-session-export.jsonl --suite all",
        ],
        "retained_evidence": retained_evidence,
    }
    if provider_backed_evidence:
        payload["provider_backed_evidence"] = provider_backed_evidence
    validate_json_artifact("release-artifact-manifest.json", payload)
    output_path = sidecar_dir(repo) / "ops" / "release-artifact-manifest.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_secret_scanned_json_artifact(output_path, "release-artifact-manifest.json", payload)
    mark_private_file(output_path)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "release.manifest_written",
            {
                "artifact": ".sidecar/ops/release-artifact-manifest.json",
                "artifact_sha256": CandidatePatch.hash_file(output_path),
                "commit": commit,
                "ci_url": ci_url,
                "approver": approver,
                "security_review": payload["security_review"],
                "wheel_sha256": payload["wheel"]["sha256"],
            },
        )
    return output_path


def _missing_release_evidence(retained_evidence: Sequence[dict[str, object]]) -> str | None:
    required = (
        ("pytest-coverage", "pytest coverage"),
        ("doctor", "doctor output"),
        ("index-check", "index check"),
        ("harness", "harness check"),
        ("ci-report", "CI"),
        ("security-review", "security review"),
        ("build-wheel", "wheel build"),
        ("twine-check", "twine check"),
        ("install-smoke", "install smoke"),
    )
    evidence_names = {Path(str(entry["path"])).name for entry in retained_evidence}
    for required_token, label in required:
        if not any(required_token in name for name in evidence_names):
            return label
    return None


def _validate_release_evidence_content(path: Path, *, wheel_filename: str) -> None:
    name = path.name
    text = path.read_text(encoding="utf-8", errors="replace")
    lowered = text.lower()
    if "pytest-coverage" in name:
        if (
            " passed" not in lowered
            or _contains_failed_release_signal(lowered)
            or _release_coverage_percent(text) < 90.0
        ):
            raise ValueError("pytest coverage evidence did not pass")
        return
    if "install-smoke" in name:
        if (
            "installed tugboat wheel" not in lowered
            or wheel_filename.lower() not in lowered
            or "installed tugboat doctor" not in lowered
            or "tugboat: ok" not in lowered
            or "mode: proposal_only" not in lowered
            or "auto_apply: disabled" not in lowered
            or "installed tugboat index --repo . --check" not in lowered
            or "index: ok" not in lowered
            or "installed tugboat harness check --repo ." not in lowered
            or "harness: ok" not in lowered
            or "installed tugboat optimize --repo .sidecar/ci/proposal-smoke-repo --trace tests/fixtures/traces/codex-local-session-export.jsonl --suite all"
            not in lowered
            or "optimization:" not in lowered
            or "audit.json" not in lowered
            or "candidate.json" not in lowered
            or "eval-report.json" not in lowered
            or "optimization-summary.json" not in lowered
            or "report.md" not in lowered
            or "auto_apply: enabled" in lowered
            or _contains_failed_release_signal(lowered)
        ):
            raise ValueError("install smoke evidence did not pass")
        return
    if "doctor" in name:
        if (
            "tugboat: ok" not in lowered
            or "mode: proposal_only" not in lowered
            or "auto_apply: disabled" not in lowered
            or "auto_apply: enabled" in lowered
            or _contains_failed_release_signal(lowered)
        ):
            raise ValueError("doctor output evidence did not pass")
        return
    if "index-check" in name:
        if "index: ok" not in lowered or _contains_failed_release_signal(lowered):
            raise ValueError("index check evidence did not pass")
        return
    if "harness" in name:
        if "harness: ok" not in lowered or _contains_failed_release_signal(lowered):
            raise ValueError("harness check evidence did not pass")
        return
    if "ci-report" in name:
        payload = load_json_object_artifact(path, "ci-report.json")
        validate_json_artifact("ci-report.json", payload)
        if bool(payload.get("auto_apply", True)) or not _ci_payload_passed(payload):
            raise ValueError("CI evidence did not pass")
        return
    if "security-review" in name:
        if (
            not _security_review_evidence_approved(lowered)
            or "no open critical or high findings" not in lowered
            or _contains_failed_release_signal(lowered)
        ):
            raise ValueError("security review evidence did not pass")
        return
    if "build-wheel" in name:
        if (
            "python -m build --wheel" not in lowered
            or wheel_filename.lower() not in lowered
            or _contains_failed_release_signal(lowered)
        ):
            raise ValueError("wheel build evidence did not pass")
        return
    if "twine-check" in name:
        if (
            "python -m twine check" not in lowered
            or wheel_filename.lower() not in lowered
            or "passed" not in lowered
            or _contains_failed_release_signal(lowered)
        ):
            raise ValueError("twine check evidence did not pass")


def _contains_failed_release_signal(lowered_text: str) -> bool:
    return bool(
        re.search(
            r"\b(failed|failure|error|traceback|exception)\b",
            lowered_text,
        )
    )


def _release_coverage_percent(text: str) -> float:
    patterns = (
        r"(?im)^TOTAL\s+.*?(\d+(?:\.\d+)?)%",
        r"(?im)^Required test coverage of \d+(?:\.\d+)?% reached\. Total coverage: (\d+(?:\.\d+)?)%",
        r"(?im)^Total coverage: (\d+(?:\.\d+)?)%",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is not None:
            return float(match.group(1))
    return 0.0


def _security_review_evidence_approved(lowered_text: str) -> bool:
    if re.search(r"\bnot\s+approved\s+(as|for)\b", lowered_text):
        return False
    return bool(
        re.search(
            r"\bapproved\s+(as|for)\b",
            lowered_text,
        )
    )


def _provider_backed_release_evidence(
    retained_evidence: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    provider_evidence: list[dict[str, object]] = []
    for entry in retained_evidence:
        path = Path(str(entry["path"]))
        payload = _read_json_file_if_object(path)
        if payload is None:
            continue
        evidence = _provider_backed_evidence_from_payload(path, payload)
        if evidence is not None:
            provider_evidence.append(evidence)
    return provider_evidence


def _read_json_file_if_object(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _provider_backed_evidence_from_payload(
    path: Path, payload: dict[str, object]
) -> dict[str, object] | None:
    candidates = [payload]
    inspect_payload = payload.get("inspect")
    if isinstance(inspect_payload, dict):
        candidates.append(inspect_payload)

    for candidate in candidates:
        if candidate.get("network_required") is not True:
            continue
        providers = _string_list(candidate.get("providers"))
        external_calls = _provider_external_calls(candidate.get("external_calls"), providers)
        if providers and external_calls:
            return {
                "path": str(path),
                "providers": sorted(providers),
                "external_calls": external_calls,
                "network_required": True,
            }
    return None


def _string_list(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _provider_external_calls(value: object, providers: set[str]) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    calls: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        target = item.get("target")
        if kind != "model_provider" or not isinstance(target, str) or target not in providers:
            continue
        calls.append({"kind": kind, "target": target})
    return calls


def _current_git_head(repo: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError("repo must have a current git HEAD") from error
    return result.stdout.strip()


def _file_manifest_entry(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "sha256": CandidatePatch.hash_file(path),
        "size_bytes": path.stat().st_size,
    }


def _project_package_metadata(repo: Path) -> dict[str, str]:
    pyproject_path = repo / "pyproject.toml"
    if not pyproject_path.exists():
        raise FileNotFoundError("pyproject.toml does not exist")
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml missing [project] metadata")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not name:
        raise ValueError("pyproject.toml missing project.name")
    if not isinstance(version, str) or not version:
        raise ValueError("pyproject.toml missing project.version")
    return {"name": name, "version": version}


def _write_retention_report(
    repo: Path,
    *,
    mode: str,
    status: str,
    candidates: Sequence[str],
    deleted: Sequence[str],
    redaction_candidates: Sequence[dict[str, object]],
) -> Path:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "status": status,
        "candidates": list(candidates),
        "deleted": list(deleted),
        "redaction_candidates": list(redaction_candidates),
    }
    validate_json_artifact("retention-report.json", payload)
    path = sidecar_dir(repo) / "ops" / "retention" / "retention-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _write_secret_scanned_json_artifact(temp_path, "retention-report.json", payload)
    mark_private_file(temp_path)
    try:
        temp_path.replace(path)
        mark_private_file(path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise
    return path


def console_main() -> None:
    raise SystemExit(main())


def _write_ci_report(
    repo: Path,
    *,
    max_instruction_lines: int,
    candidate: str | None,
    suite: str,
) -> tuple[Path, dict[str, Any]]:
    policy = load_policy(repo)
    index = index_repo(repo, policy)
    manifest_contracts = validate_manifest_contracts(materialize_manifests(repo))
    harness = check_harness_legibility(repo, max_instruction_lines)
    harness_report = generate_harness_report(repo)
    harness_report_path = _persist_harness_report(repo, harness_report)
    semantic_findings = _semantic_policy_lint(repo, index)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "ci_check",
        "auto_apply": False,
        "checks": {
            "index": {
                "passed": True,
                "indexed_documents": index.indexed_count,
            },
            "harness": {
                "passed": harness.passed,
                "findings": list(harness.findings),
                "report_path": harness_report_path.relative_to(repo).as_posix(),
                "report_sha256": CandidatePatch.hash_file(harness_report_path),
                "doc_gardening_task_count": len(harness_report.doc_gardening_tasks),
            },
            "harness_report": _ci_harness_report_check_payload(harness_report),
            "manifest_contracts": {
                "passed": manifest_contracts.passed,
                "findings": list(manifest_contracts.findings),
            },
            "semantic_policy_lint": {
                "passed": not semantic_findings,
                "findings": semantic_findings,
            },
        },
    }
    if candidate is not None:
        eval_result = run_eval_pipeline(repo, candidate, suite)
        eval_payload = _ci_eval_check_payload(
            repo,
            candidate=candidate,
            suite=suite,
            run_dir=eval_result.run_dir,
            passed=eval_result.exit_code == 0,
        )
        payload["checks"]["eval"] = eval_payload
    validate_json_artifact("ci-report.json", payload)
    report_path = sidecar_dir(repo) / "ci" / "ci-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_secret_scanned_json_artifact(report_path, "ci-report.json", payload)
    mark_private_file(report_path)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "ci.check_completed",
            {
                "artifact": report_path.relative_to(repo).as_posix(),
                "artifact_sha256": CandidatePatch.hash_file(report_path),
                "passed": _ci_payload_passed(payload),
            },
        )
    return report_path, payload


def _ci_harness_report_check_payload(report) -> dict[str, Any]:
    return {
        "passed": not (
            report.missing_docs
            or report.stale_docs
            or report.orphaned_runbooks
            or report.recurring_failures_without_docs
        ),
        "missing_docs": list(report.missing_docs),
        "stale_docs": list(report.stale_docs),
        "orphaned_runbooks": list(report.orphaned_runbooks),
        "recurring_failures_without_docs": list(report.recurring_failures_without_docs),
        "doc_gardening_tasks": list(report.doc_gardening_tasks),
    }


def _ci_eval_check_payload(
    repo: Path,
    *,
    candidate: str,
    suite: str,
    run_dir: Path,
    passed: bool,
) -> dict[str, Any]:
    report_path = run_dir / "eval-report.json"
    report = {}
    if report_path.exists():
        candidate_report = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(candidate_report, dict) and candidate_report.get("suite_id") == suite:
            report = candidate_report
    return {
        "passed": passed,
        "candidate": candidate,
        "suite_id": suite,
        "report_path": report_path.relative_to(repo).as_posix(),
        "trigger_score": float(report.get("trigger_score", 0.0)),
        "held_out_score": float(report.get("held_out_score", 0.0)),
        "governance_passed": bool(report.get("governance_passed", False)),
        "recommendation": str(report.get("recommendation", "reject")),
    }


def _ci_payload_passed(payload: dict[str, Any]) -> bool:
    checks = payload["checks"]
    return bool(
        checks["index"]["passed"]
        and checks["harness"]["passed"]
        and checks["harness_report"]["passed"]
        and checks["manifest_contracts"]["passed"]
        and checks["semantic_policy_lint"]["passed"]
        and ("eval" not in checks or checks["eval"]["passed"])
    )


SEMANTIC_LINT_TERMS = (
    "approval",
    "sandbox",
    "test",
    "review",
    "secret",
    "memory",
    "network",
    "deploy",
    "permission",
)
PERMISSIVE_GOVERNANCE_LANGUAGE = re.compile(
    r"\b(may|can|could|optional)\b(?:\s+\w+){0,4}\s+\b(skip|bypass|ignore)\b",
    re.IGNORECASE,
)
NEGATED_PERMISSIVE_GOVERNANCE_LANGUAGE = re.compile(
    r"\b(can(?:not|'t|\s+not)|could(?:\s+not|n't)|may\s+not)\b"
    r"(?:\s+\w+){0,4}\s+\b(skip|bypass|ignore)\b",
    re.IGNORECASE,
)


def _semantic_policy_lint(repo: Path, index) -> list[str]:
    findings: list[str] = []
    for document in index.documents:
        text = (repo / document.path).read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            if not PERMISSIVE_GOVERNANCE_LANGUAGE.search(line):
                continue
            if NEGATED_PERMISSIVE_GOVERNANCE_LANGUAGE.search(line):
                continue
            for term in SEMANTIC_LINT_TERMS:
                if term in lowered:
                    findings.append(
                        f"{document.path}:{line_number} weakens governance term "
                        f"'{term}' with permissive language."
                    )
                    break
    return findings


def _persist_harness_report(repo: Path, report) -> Path:
    report_path = sidecar_dir(repo) / "harness-report.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "knowledge_map": report.knowledge_map,
        "missing_docs": report.missing_docs,
        "stale_docs": report.stale_docs,
        "orphaned_runbooks": report.orphaned_runbooks,
        "recurring_failures_without_docs": report.recurring_failures_without_docs,
        "doc_gardening_tasks": report.doc_gardening_tasks,
        "token_metrics": report.token_metrics,
    }
    validate_json_artifact("harness-report.json", payload)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    _write_secret_scanned_json_artifact(report_path, "harness-report.json", payload)
    mark_private_file(report_path)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        for finding in report.missing_docs:
            store.record_harness_finding(
                repo_path=repo,
                finding=finding,
                severity="missing_doc",
            )
        for finding in report.stale_docs:
            store.record_harness_finding(
                repo_path=repo,
                finding=finding,
                severity="stale_doc",
            )
        for finding in report.orphaned_runbooks:
            store.record_harness_finding(
                repo_path=repo,
                finding=finding,
                severity="orphaned_runbook",
            )
        for finding in report.recurring_failures_without_docs:
            store.record_harness_finding(
                repo_path=repo,
                finding=finding,
                severity="recurring_failure_without_doc",
            )
        for task in report.doc_gardening_tasks:
            store.record_harness_finding(
                repo_path=repo,
                finding=task,
                severity="task",
            )
        store.record_doc_gardening_run(
            repo_path=repo,
            status="completed",
            report_path=report_path,
        )
    return report_path


def _record_optimization_slow_update(
    store: Store,
    *,
    repo: Path,
    candidate_id: int,
    suite_id: str,
    category: str,
    reason: str,
) -> None:
    note = f"{category}: {reason} for candidate {candidate_id} in suite {suite_id}"
    key = f"slow_update:{candidate_id}:{suite_id}:{hashlib.sha256(note.encode('utf-8')).hexdigest()}"
    store.record_optimizer_memory(
        repo_path=str(repo),
        memory_type="slow_update",
        key=key,
        payload={
            "category": category,
            "legacy_note": note,
            "note": f"{reason} for candidate {candidate_id} in suite {suite_id}",
        },
    )


def _finalize_governed_candidate_evaluation(
    repo: Path,
    run_dir: Path,
    *,
    suite_id: str,
    eval_exit_code: int,
    unseen_suites: tuple[str, ...] = (),
) -> int:
    if not (run_dir / "candidate.raw.json").exists():
        return eval_exit_code
    if not (run_dir / "eval-report.json").exists():
        return eval_exit_code
    if eval_exit_code == 0:
        unseen_suite_results: list[dict[str, object]] | None = None
        try:
            eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
            policy_gate = _read_optional_json_object(run_dir / "policy-gate.json")
            _assert_eval_acceptance(
                eval_report,
                policy_gate,
                validation_baseline_score=_load_validation_baseline_score(
                    repo,
                    suite_id=suite_id,
                ),
            )
        except ValueError:
            pass
        else:
            if unseen_suites:
                unseen_suite_results, unseen_failure = _run_unseen_suite_gates(
                    repo,
                    run_dir,
                    unseen_suites=unseen_suites,
                )
                if unseen_failure is not None:
                    print(f"optimization rejected: {unseen_failure}")
                    return _write_optimization_summary(
                        repo,
                        run_dir,
                        suite_id=suite_id,
                        forced_rejection_reason=unseen_failure,
                        unseen_suite_results=unseen_suite_results,
                    )
            eval_reports_path = (
                _write_eval_report_collection(
                    repo,
                    run_dir,
                    primary_suite=suite_id,
                    unseen_suite_results=unseen_suite_results,
                )
                if unseen_suite_results is not None
                else run_dir / "eval-report.json"
            )
            try:
                _run_acceptance_summary(
                    repo,
                    run_dir,
                    load_policy(repo),
                    eval_reports_path=eval_reports_path,
                )
            except LlmffRunFailed as error:
                print(str(error))
                return error.exit_code
            except (RuntimeError, ValueError) as error:
                print(str(error))
                return 1
            return _write_optimization_summary(
                repo,
                run_dir,
                suite_id=suite_id,
                unseen_suite_results=unseen_suite_results,
            )
    return _write_optimization_summary(repo, run_dir, suite_id=suite_id)


def _run_unseen_suite_gates(
    repo: Path,
    run_dir: Path,
    *,
    unseen_suites: tuple[str, ...],
) -> tuple[list[dict[str, object]], str | None]:
    canonical_paths = (
        run_dir / "eval-suite.json",
        run_dir / "eval-report.raw.json",
        run_dir / "policy-decision.raw.json",
        run_dir / "policy-gate.json",
        run_dir / "eval-report.json",
        run_dir / "patch-eval" / "checkpoint.json",
    )
    primary_artifacts = {
        path: path.read_bytes() for path in canonical_paths if path.exists()
    }
    results: list[dict[str, object]] = []
    failure_reason: str | None = None
    try:
        for suite_id in unseen_suites:
            checkpoint_path = run_dir / "patch-eval" / "checkpoint.json"
            if checkpoint_path.exists():
                checkpoint_path.unlink()
            eval_result = run_eval_pipeline(repo, run_dir.name, suite_id)
            print(eval_result.message)
            report_path = run_dir / "eval-report.json"
            if not report_path.exists():
                failure_reason = f"unseen suite {suite_id} failed"
                continue
            eval_report = json.loads(report_path.read_text(encoding="utf-8"))
            validate_json_artifact("eval-report.json", eval_report)
            suite_dir = run_dir / "unseen-evals" / _artifact_safe_suite_id(suite_id)
            suite_dir.mkdir(parents=True, exist_ok=True)
            for path in canonical_paths:
                if path.exists():
                    shutil.copy2(path, suite_dir / path.name)
                    mark_private_file(suite_dir / path.name)
            result = _unseen_suite_result(eval_report)
            results.append(result)
            if eval_result.exit_code != 0:
                failure_reason = f"unseen suite {suite_id} failed"
                continue
            try:
                _assert_eval_acceptance(
                    eval_report,
                    _read_optional_json_object(run_dir / "policy-gate.json"),
                )
            except ValueError:
                failure_reason = f"unseen suite {suite_id} failed"
    finally:
        for path in canonical_paths:
            if path in primary_artifacts:
                path.write_bytes(primary_artifacts[path])
                mark_private_file(path)
            elif path.exists():
                path.unlink()
    _write_secret_scanned_json_artifact(
        run_dir / "unseen-eval-reports.json",
        "unseen-eval-reports.json",
        {"schema_version": SCHEMA_VERSION, "reports": results},
    )
    mark_private_file(run_dir / "unseen-eval-reports.json")
    return results, failure_reason


def _artifact_safe_suite_id(suite_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", suite_id).strip(".-")
    return safe or "suite"


def _unseen_suite_result(eval_report: dict[str, object]) -> dict[str, object]:
    return {
        "suite_id": str(eval_report["suite_id"]),
        "passed": bool(eval_report["passed"]),
        "governance_passed": bool(eval_report["governance_passed"]),
        "recommendation": str(eval_report["recommendation"]),
        "held_out_score": float(eval_report["held_out_score"]),
        "trigger_score": float(eval_report["trigger_score"]),
    }


def _write_eval_report_collection(
    repo: Path,
    run_dir: Path,
    *,
    primary_suite: str,
    unseen_suite_results: list[dict[str, object]],
) -> Path:
    primary_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    validate_json_artifact("eval-report.json", primary_report)
    reports = [
        {
            **_unseen_suite_result(primary_report),
            "role": "held_out",
            "path": _relative_repo_path(repo, run_dir / "eval-report.json"),
        }
    ]
    reports.extend(
        {
            **result,
            "role": "unseen",
            "path": _relative_repo_path(
                repo,
                run_dir
                / "unseen-evals"
                / _artifact_safe_suite_id(str(result["suite_id"]))
                / "eval-report.json",
            ),
        }
        for result in unseen_suite_results
    )
    path = run_dir / "eval-report-collection.json"
    _write_secret_scanned_json_artifact(
        path,
        "eval-report-collection.json",
        {
            "schema_version": SCHEMA_VERSION,
            "primary_suite": primary_suite,
            "reports": reports,
        },
    )
    mark_private_file(path)
    return path


def _run_acceptance_summary(
    repo: Path,
    run_dir: Path,
    policy,
    *,
    eval_reports_path: Path | None = None,
) -> dict[str, object]:
    manifests = materialize_manifests(repo)
    require_manifest_contracts(manifests)
    if not manifests_are_allowed_by_policy(manifests, policy):
        raise RuntimeError("manifest hash is not allowed by policy")
    manifest = next(record.path for record in manifests if record.name == "acceptance-summary.yaml")
    inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy)
    try:
        run = run_manifest(
            manifest,
            run_dir=run_dir,
            policy=policy,
            timeout_ms=policy.llmff_timeout_ms,
            retry_attempts=policy.llmff_retry_attempts,
            retry_backoff_ms=policy.llmff_retry_backoff_ms,
            checkpoint_path=run_dir / "acceptance-summary" / "checkpoint.json",
            input_paths={
                "audit_report": run_dir / "audit.raw.json",
                "candidate_patch": run_dir / "candidate.raw.json",
                "policy_gate": run_dir / "policy-gate.json",
                "eval_reports": eval_reports_path or run_dir / "eval-report.json",
                "proposal_rationale": run_dir / "proposal-rationale.raw.json",
                "risk_class": run_dir / "candidate.json",
            },
            output_paths={"acceptance_summary": run_dir / "acceptance-summary.raw.json"},
            validate_output_artifacts=False,
        )
    except MissingOutputError:
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.insert_run(
                run_id=run_dir.name,
                stage="acceptance_summary",
                manifest_hash=inspect.manifest_hash,
                status="failed",
                run_dir=run_dir,
            )
        raise
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_llmff_run(
            run_id=run_dir.name,
            manifest_hash=inspect.manifest_hash,
            result=run,
        )
        if run.exit_code != 0:
            store.insert_run(
                run_id=run_dir.name,
                stage="acceptance_summary",
                manifest_hash=inspect.manifest_hash,
                status="failed",
                run_dir=run_dir,
            )
    if run.exit_code != 0:
        raise LlmffRunFailed(
            f"llmff acceptance-summary failed with exit code {run.exit_code}",
            exit_code=run.exit_code,
        )
    try:
        payload = load_json_object_artifact(
            run.output_paths["acceptance_summary"],
            "acceptance-summary.raw.json",
        )
        validate_json_artifact("acceptance-summary.raw.json", payload)
    except ValueError:
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.insert_run(
                run_id=run_dir.name,
                stage="acceptance_summary",
                manifest_hash=inspect.manifest_hash,
                status="failed",
                run_dir=run_dir,
            )
        raise
    return payload


def _candidate_from_artifacts(run_dir: Path) -> CandidatePatch:
    metadata = load_json_object_artifact(run_dir / "candidate.json", "candidate.json")
    diff = (run_dir / "candidate.diff").read_text(encoding="utf-8")
    if hashlib.sha256(diff.encode("utf-8")).hexdigest() != str(metadata["diff_hash"]):
        raise ValueError("candidate diff hash does not match candidate.diff")
    return CandidatePatch(
        audit_id=int(metadata["audit_id"]),
        base_file=str(metadata["base_file"]),
        base_hash=str(metadata["base_hash"]),
        diff=diff,
        risk_class=str(metadata["risk_class"]),
        rationale=str(metadata["rationale"]),
        expected_behavior_change=str(metadata.get("expected_behavior_change", "Not specified.")),
        evals_required=tuple(str(item) for item in metadata.get("evals_required", [])),
        rollback_plan=tuple(str(item) for item in metadata.get("rollback_plan", [])),
        sources=tuple(
            SourceRef(str(source["source_id"]), trusted=bool(source["trusted"]))
            for source in metadata.get("sources", [])
        ),
        pending_audit_eval_definition_paths=tuple(
            str(path) for path in metadata.get("pending_audit_eval_definition_paths", [])
        ),
        bounded_edit_metadata=tuple(
            dict(item) for item in metadata.get("bounded_edit_metadata", []) if isinstance(item, dict)
        ),
    )


def _decision_from_artifact(run_dir: Path):
    from tugboat.policy.gate import PolicyDecision

    payload = load_json_object_artifact(run_dir / "policy-gate.json", "policy-gate.json")
    return PolicyDecision(bool(payload["allowed"]), tuple(payload["reasons"]))


def _latest_llmff_failure_kind(store: Store, job_id: int) -> str | None:
    row = store.connection.execute(
        """
        SELECT payload_json
        FROM llmff_events
        WHERE job_id = ? AND event_type = 'run_failed'
        ORDER BY id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if row is not None:
        try:
            payload = json.loads(str(row[0]))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            details = payload.get("run_failed")
            if isinstance(details, dict) and details.get("failure_kind"):
                return str(details["failure_kind"])
            failure_kind = payload.get("failure_kind")
            if failure_kind:
                return str(failure_kind)
    row = store.connection.execute(
        """
        SELECT ae.payload_json
        FROM llmff_jobs job
        JOIN audit_events ae ON ae.sequence = job.audit_event_sequence
        WHERE job.id = ? AND ae.event_type = 'llmff_job.recorded'
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        job_payload = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return None
    if not isinstance(job_payload, dict):
        return None
    run_failed = job_payload.get("run_failed")
    if isinstance(run_failed, dict) and run_failed.get("failure_kind"):
        return str(run_failed["failure_kind"])
    return None


def _write_apply_plan(
    repo: Path,
    run_dir: Path,
    *,
    mode: str,
    review_actor: str,
    human_review: bool,
    auto_apply: bool = False,
    confirm_auto_apply: bool = False,
    auto_apply_policy_version: int | None = None,
) -> Path:
    candidate = _candidate_from_artifacts(run_dir)
    candidate_meta = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate_id = int(candidate_meta["candidate_id"])
    target_files = (candidate.base_file,)
    policy = load_policy(repo)
    decision = evaluate_candidate(
        repo,
        policy,
        candidate,
        rejected_edit_fingerprints=_load_rejected_edit_fingerprints(repo),
    )
    if not decision.allowed:
        raise ValueError(f"policy gate rejected candidate: {', '.join(decision.reasons)}")
    policy_gate = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    validate_json_artifact("policy-gate.json", policy_gate)
    if not bool(policy_gate["allowed"]):
        raise ValueError("stored policy gate rejected candidate")
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    validate_json_artifact("eval-report.json", eval_report)
    if int(eval_report["candidate_id"]) != candidate_id:
        raise ValueError("eval report candidate_id does not match candidate")
    if not bool(eval_report["passed"]):
        raise ValueError("eval report did not pass")
    _assert_eval_acceptance(eval_report, policy_gate)
    recorded_provenance = _assert_apply_recorded_provenance(
        repo,
        run_dir,
        candidate_id=candidate_id,
        suite_id=str(eval_report["suite_id"]),
    )
    explicit_human_review = bool(decision.review_required_reasons)
    if explicit_human_review and (not human_review or review_actor == "tugboat"):
        raise ValueError("Class C candidates require explicit human review")
    if auto_apply and mode != "commit":
        raise ValueError("auto-apply requires commit mode")
    if mode == "pr":
        if not policy.vcs_pull_request_enabled:
            raise ValueError("PR mode requires vcs.pull_request.enabled")
        if policy.vcs_pull_request_provider != "github_cli":
            raise ValueError(
                f"unsupported pull request provider: {policy.vcs_pull_request_provider}"
            )
        if not policy.vcs_pull_request_remote.strip():
            raise ValueError("PR mode requires vcs.pull_request.remote")

    adapter = VcsAdapter(repo)
    if auto_apply:
        _assert_user_worktree_clean(adapter)
    elif mode in {"branch", "commit", "pr"}:
        adapter.assert_clean_worktree()
    adapter.assert_target_files_clean(target_files)
    adapter.assert_base_hashes({candidate.base_file: candidate.base_hash})
    base_branch = adapter.current_branch()
    branch_name = adapter.branch_name(
        run_id=run_dir.name,
        candidate_id=candidate_id,
        base_file=candidate.base_file,
    )
    commit_message = adapter.commit_message(
        run_id=run_dir.name,
        candidate_id=candidate_id,
        base_file=candidate.base_file,
        rationale=candidate.rationale,
    )
    pr_base_branch = policy.vcs_pull_request_base_branch or base_branch
    planned_pr_metadata_obj = (
        adapter.pull_request_metadata(
            candidate_id=candidate_id,
            base_file=candidate.base_file,
            branch_name=branch_name,
            base_branch=pr_base_branch,
            draft=policy.vcs_pull_request_draft,
            rationale=candidate.rationale,
            run_id=run_dir.name,
            eval_suite_id=str(eval_report["suite_id"]),
            eval_passed=bool(eval_report["passed"]),
            policy_gate_allowed=bool(policy_gate["allowed"]),
            rollback_ready=True,
            artifacts=_pull_request_artifact_refs(repo, run_dir),
        )
        if mode == "pr"
        else None
    )
    planned_pr_metadata = (
        planned_pr_metadata_obj.to_json_dict() if planned_pr_metadata_obj is not None else {}
    )
    _preflight_apply_metadata(
        run_dir / "apply-plan.json",
        {
            "branch_name": branch_name,
            "commit_message": commit_message,
            "decision_rationale": "policy gate and eval report passed",
            "pr_metadata": planned_pr_metadata,
            "pr_result": {},
            "review_actor": review_actor,
            "review_required_reasons": list(decision.review_required_reasons),
        },
    )
    pre_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
    post_hashes: dict[str, str] = {}
    applied_commit = ""
    rollback_command: list[list[str]] = []
    auto_apply_approval: dict[str, object] | None = None
    pr_metadata: dict[str, object] = {}
    pr_result: dict[str, object] = {}
    branch_created = False
    applied_worktree_change = False

    if auto_apply:
        _assert_auto_apply_precheck(
            repo,
            run_dir,
            candidate_id=candidate_id,
            candidate=candidate,
            mode=mode,
            branch_name=branch_name,
            review_actor=review_actor,
            confirmed=confirm_auto_apply,
            policy_version=auto_apply_policy_version,
        )

    try:
        if mode == "branch":
            adapter.create_branch(branch_name)
            branch_created = True
            adapter.apply_diff(run_dir / "candidate.diff", allowed_paths=target_files)
            post_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
            applied_worktree_change = True
            rollback_command = [
                ["git", "restore", "--worktree", "--staged", "--", *target_files],
                ["git", "switch", base_branch],
                ["git", "branch", "-D", branch_name],
            ]
        elif mode == "commit":
            adapter.create_branch(branch_name)
            branch_created = True
            adapter.apply_diff(run_dir / "candidate.diff", allowed_paths=target_files)
            post_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
            applied_worktree_change = True
            applied_commit = adapter.commit_files(target_files, commit_message)
            rollback_command = [
                list(command)
                for command in adapter.rollback_metadata(
                    commit_sha=applied_commit,
                    branch_name=branch_name,
                    files=target_files,
                    reason=f"rollback candidate {candidate_id}",
                ).commands
            ]
            if auto_apply:
                auto_apply_approval = _assert_auto_apply_final(
                    repo,
                    run_dir,
                    candidate_id=candidate_id,
                    candidate=candidate,
                    mode=mode,
                    branch_name=branch_name,
                    applied_commit=applied_commit,
                    review_actor=review_actor,
                    confirmed=confirm_auto_apply,
                    policy_version=auto_apply_policy_version,
                )
        elif mode == "pr":
            adapter.create_branch(branch_name)
            branch_created = True
            adapter.apply_diff(run_dir / "candidate.diff", allowed_paths=target_files)
            post_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
            applied_worktree_change = True
            applied_commit = adapter.commit_files(target_files, commit_message)
            rollback_command = [
                list(command)
                for command in adapter.rollback_metadata(
                    commit_sha=applied_commit,
                    branch_name=branch_name,
                    files=target_files,
                    reason=f"rollback candidate {candidate_id}",
                ).commands
            ]
            pr_metadata = planned_pr_metadata
            adapter.push_branch(policy.vcs_pull_request_remote, branch_name)
            if planned_pr_metadata_obj is None:
                raise ValueError("PR metadata was not prepared")
            created_pr: PullRequestResult = adapter.create_pull_request(
                planned_pr_metadata_obj,
                provider=policy.vcs_pull_request_provider,
            )
            pr_result = created_pr.to_json_dict()
    except VcsStateError:
        if branch_created and not applied_commit:
            adapter.discard_worktree_changes()
            adapter.switch_branch(base_branch)
            adapter.delete_branch(branch_name)
        raise
    except ValueError:
        if auto_apply and branch_created:
            if not applied_commit:
                adapter.discard_worktree_changes()
            adapter.switch_branch(base_branch)
            adapter.delete_branch(branch_name)
        raise

    payload = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "candidate_id": candidate_id,
        "decision_id": run_dir.name,
        "run_id": run_dir.name,
        "target_files": list(target_files),
        "branch_name": branch_name,
        "commit_message": commit_message,
        "pre_hashes": pre_hashes,
        "post_hashes": post_hashes,
        "applied_commit": applied_commit,
        "rollback_command": rollback_command,
        "provenance_bundle": _relative_repo_path(repo, run_dir / "provenance-bundle.json"),
        "pr_metadata": pr_metadata,
        "pr_result": pr_result,
        "review_actor": review_actor,
        "auto_apply": auto_apply,
        "explicit_human_review": explicit_human_review,
        "review_required_reasons": list(decision.review_required_reasons),
        "decision_rationale": "policy gate and eval report passed",
    }
    provenance_bundle = str(payload["provenance_bundle"])
    path = run_dir / "apply-plan.json"
    _write_secret_scanned_json_artifact(path, "apply-plan.json", payload)
    _write_provenance_bundle(
        repo,
        run_dir,
        candidate_id=candidate_id,
        mode=mode,
        target_files=target_files,
        applied_commit=applied_commit,
        rollback_command=rollback_command,
        pre_hashes=pre_hashes,
        post_hashes=post_hashes,
        apply_plan_path=path,
        recorded_provenance=recorded_provenance,
    )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "apply.planned",
            {
                "candidate_id": candidate_id,
                "mode": mode,
                "run_id": run_dir.name,
                "target_files": list(target_files),
                "applied_commit": applied_commit,
                "auto_apply": auto_apply,
                "provenance_bundle": provenance_bundle,
            },
        )
        store.insert_decision(
            candidate_id=candidate_id,
            actor=review_actor,
            policy="apply_controller",
            decision="applied" if applied_worktree_change else "planned",
            reason="policy gate and eval report passed",
            applied_commit=applied_commit,
            rollback_ref=json.dumps(rollback_command, sort_keys=True),
        )
        if applied_worktree_change:
            store.append_audit_event(
                "apply.applied",
                _apply_applied_event_payload(
                    repo,
                    run_dir,
                    candidate_id=candidate_id,
                    mode=mode,
                    target_files=target_files,
                    applied_commit=applied_commit,
                    pre_hashes=pre_hashes,
                    post_hashes=post_hashes,
                    provenance_bundle=provenance_bundle,
                    rollback_command=rollback_command,
                    pr_result=pr_result,
                ),
            )
            _transition_originating_daemon_job(
                repo,
                run_dir,
                candidate_id=candidate_id,
                source_state=JobState.WAITING_REVIEW,
                target_state=JobState.APPLIED,
            )
        if auto_apply and auto_apply_approval is not None:
            approval_path = run_dir / "auto-apply-approval.json"
            _write_secret_scanned_json_artifact(
                approval_path,
                "auto-apply-approval.json",
                auto_apply_approval,
            )
            store.append_audit_event(
                "auto_apply.applied",
                {
                    "candidate_id": candidate_id,
                    "run_id": run_dir.name,
                    "approval_bundle": auto_apply_approval,
                    "reasons": [],
                },
            )
            store.insert_decision(
                candidate_id=candidate_id,
                actor=review_actor,
                policy="auto_apply_controller",
                decision="applied",
                reason="auto-apply policy and CLI confirmation passed",
                applied_commit=applied_commit,
                rollback_ref=json.dumps(auto_apply_approval["rollback_command"], sort_keys=True),
            )
        if explicit_human_review and human_review:
            store.record_review_action(
                candidate_id=candidate_id,
                actor=review_actor,
                action="approved",
                reason=",".join(decision.review_required_reasons),
            )
    return path


def _write_auto_apply_preflight(
    repo: Path,
    run_dir: Path,
    *,
    review_actor: str,
    confirmed: bool,
    policy_version: int | None,
    artifact_name: str = "auto-apply-preflight.json",
    shadow_mode: bool = False,
    phase: str = "preflight",
) -> Path:
    candidate = _candidate_from_artifacts(run_dir)
    candidate_meta = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate_id = int(candidate_meta["candidate_id"])
    target_files = (candidate.base_file,)
    policy = load_policy(repo)
    decision = evaluate_candidate(
        repo,
        policy,
        candidate,
        rejected_edit_fingerprints=_load_rejected_edit_fingerprints(repo),
    )
    policy_gate = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    validate_json_artifact("policy-gate.json", policy_gate)
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    validate_json_artifact("eval-report.json", eval_report)
    candidate_id_matches = int(eval_report["candidate_id"]) == candidate_id
    eval_acceptance_reason = ""
    try:
        if not candidate_id_matches:
            raise ValueError("eval report candidate_id does not match candidate")
        if not bool(eval_report["passed"]):
            raise ValueError("eval report did not pass")
        _assert_eval_acceptance(eval_report, policy_gate)
    except ValueError as error:
        eval_acceptance_reason = str(error)

    adapter = VcsAdapter(repo)
    branch_name = adapter.branch_name(
        run_id=run_dir.name,
        candidate_id=candidate_id,
        base_file=candidate.base_file,
    )
    vcs_checks = _auto_apply_vcs_preflight_checks(adapter, target_files, candidate)
    rollback_command = _auto_apply_rollback_command(repo, run_dir)
    metrics = _auto_apply_ledger_metrics(repo, exclude_candidate_id=candidate_id)
    categories = _auto_apply_candidate_categories(candidate)
    changed_lines = _auto_apply_candidate_changed_lines(candidate)
    instruction_token_delta = _auto_apply_instruction_token_delta(
        run_dir,
        candidate_id=candidate_id,
    )
    held_out_eval_passed, governance_regression_passed = _auto_apply_eval_evidence(
        run_dir,
        candidate_id=candidate_id,
    )
    auto_apply_candidate = AutoApplyCandidate(
        candidate_id=str(candidate_id),
        repository=str(repo.resolve()),
        change_class=candidate.risk_class,
        categories=categories,
        held_out_eval_passed=held_out_eval_passed,
        governance_regression_passed=governance_regression_passed,
        rejection_rate=float(metrics["rejection_rate"]),
        rollback_rate=float(metrics["rollback_rate"]),
        changed_lines=changed_lines,
        instruction_token_delta=instruction_token_delta,
        vcs_proof=VcsProof(
            mode="commit",
            commit_sha="pending",
            branch_name=branch_name,
            rollback_commands=(rollback_command,),
        ),
        skill_report_passed=_auto_apply_skill_report_passed(
            run_dir,
            candidate_id=candidate_id,
            categories=categories,
        ),
    )
    readiness = _auto_apply_readiness(
        repo,
        review_actor=review_actor,
        confirmed=confirmed,
        policy_version=policy_version,
        burn_in_days=int(metrics["burn_in_days"]),
    )
    auto_apply_decision = evaluate_auto_apply(
        candidate=auto_apply_candidate,
        readiness=readiness,
    )
    reasons = list(auto_apply_decision.reasons)
    if not decision.allowed:
        reasons.append("policy_gate_rejected")
    if not bool(policy_gate["allowed"]):
        reasons.append("stored_policy_gate_rejected")
    if eval_acceptance_reason:
        reasons.append("eval_report_rejected")
    if not bool(vcs_checks["preflight_passed"]):
        reasons.append("vcs_preflight_failed")
    reasons = list(dict.fromkeys(reasons))
    approval_bundle = None
    if auto_apply_decision.approval_bundle is not None and not reasons:
        approval_bundle = auto_apply_decision.approval_bundle.to_json_dict()
        approval_bundle["readiness_metrics"] = metrics
    eval_report_check: dict[str, object] = {
        "candidate_id_matches": candidate_id_matches,
        "passed": bool(eval_report["passed"]),
        "recommendation": str(eval_report.get("recommendation", "")),
        "suite_id": str(eval_report["suite_id"]),
    }
    if eval_acceptance_reason:
        eval_report_check["acceptance_reason"] = eval_acceptance_reason
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "candidate_id": candidate_id,
        "mode": "commit",
        "target_files": list(target_files),
        "branch_name": branch_name,
        "eligible": not reasons,
        "would_apply": not reasons,
        "lane": auto_apply_decision.lane,
        "reasons": reasons,
        "approval_bundle": approval_bundle,
        "checks": {
            "policy_gate": {
                "allowed": decision.allowed,
                "reasons": list(decision.reasons),
            },
            "stored_policy_gate": {
                "allowed": bool(policy_gate["allowed"]),
                "reasons": list(policy_gate.get("reasons", [])),
            },
            "eval_report": {
                **eval_report_check,
            },
            "vcs": vcs_checks,
            "auto_apply": _auto_apply_decision_snapshot(
                phase=phase,
                candidate=auto_apply_candidate,
                readiness=readiness,
                metrics=metrics,
            ),
        },
        "readiness_metrics": metrics,
    }
    if shadow_mode:
        payload["shadow_mode"] = True
    path = run_dir / artifact_name
    _write_secret_scanned_json_artifact(path, artifact_name, payload)
    return path


def _write_auto_apply_shadow(
    repo: Path,
    run_dir: Path,
    *,
    review_actor: str,
    confirmed: bool,
    policy_version: int | None,
) -> Path:
    path = _write_auto_apply_preflight(
        repo,
        run_dir,
        review_actor=review_actor,
        confirmed=confirmed,
        policy_version=policy_version,
        artifact_name="auto-apply-shadow.json",
        shadow_mode=True,
        phase="shadow",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    try:
        report_path = path.relative_to(repo).as_posix()
    except ValueError:
        report_path = path.as_posix()
    _record_auto_apply_shadow(
        repo,
        candidate_id=int(payload["candidate_id"]),
        run_id=str(payload["run_id"]),
        actor=review_actor,
        eligible=bool(payload["eligible"]),
        would_apply=bool(payload["would_apply"]),
        lane=payload.get("lane") if isinstance(payload.get("lane"), str) else None,
        reasons=tuple(str(reason) for reason in payload.get("reasons", [])),
        report_path=report_path,
    )
    return path


def _auto_apply_vcs_preflight_checks(
    adapter: VcsAdapter,
    target_files: tuple[str, ...],
    candidate: CandidatePatch,
) -> dict[str, object]:
    worktree_check = adapter.check_clean_worktree()
    user_dirty_paths = tuple(
        path for path in worktree_check.dirty_paths if not path.startswith(".sidecar/")
    )
    target_files_clean = True
    base_hashes_match = True
    reasons: list[str] = []
    try:
        adapter.assert_target_files_clean(target_files)
    except VcsStateError as error:
        target_files_clean = False
        reasons.append(str(error))
    try:
        adapter.assert_base_hashes({candidate.base_file: candidate.base_hash})
    except VcsStateError as error:
        base_hashes_match = False
        reasons.append(str(error))
    return {
        "preflight_passed": not user_dirty_paths and target_files_clean and base_hashes_match,
        "worktree_clean": not user_dirty_paths,
        "dirty_paths": list(user_dirty_paths),
        "target_files_clean": target_files_clean,
        "base_hashes_match": base_hashes_match,
        "reasons": reasons,
    }


def _preflight_apply_metadata(path: Path, payload: dict[str, object]) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    findings = scan_text(path.as_posix(), text)
    if findings:
        raise SecretScanError(findings)


def _write_provenance_bundle(
    repo: Path,
    run_dir: Path,
    *,
    candidate_id: int,
    mode: str,
    target_files: tuple[str, ...],
    applied_commit: str,
    rollback_command: list[list[str]],
    pre_hashes: dict[str, str],
    post_hashes: dict[str, str],
    apply_plan_path: Path,
    recorded_provenance: dict[str, object],
) -> Path:
    def artifact_ref(path: Path) -> dict[str, str]:
        return {
            "path": _relative_repo_path(repo, path),
            "sha256": CandidatePatch.hash_file(path),
        }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "candidate_id": candidate_id,
        "mode": mode,
        "target_files": list(target_files),
        "applied_commit": applied_commit,
        "rollback_command": rollback_command,
        "pre_hashes": pre_hashes,
        "post_hashes": post_hashes,
        "recorded_provenance": recorded_provenance,
        "source_artifacts": {
            "apply_plan": artifact_ref(apply_plan_path),
            "candidate_diff": artifact_ref(run_dir / "candidate.diff"),
            "candidate_metadata": artifact_ref(run_dir / "candidate.json"),
            "eval_report": artifact_ref(run_dir / "eval-report.json"),
            "policy_gate": artifact_ref(run_dir / "policy-gate.json"),
        },
    }
    path = run_dir / "provenance-bundle.json"
    _write_secret_scanned_json_artifact(path, "provenance-bundle.json", payload)
    return path


def _assert_auto_apply_precheck(
    repo: Path,
    run_dir: Path,
    *,
    candidate_id: int,
    candidate: CandidatePatch,
    mode: str,
    branch_name: str,
    review_actor: str,
    confirmed: bool,
    policy_version: int | None,
) -> None:
    rollback_command = _auto_apply_rollback_command(repo, run_dir)
    metrics = _auto_apply_ledger_metrics(repo, exclude_candidate_id=candidate_id)
    categories = _auto_apply_candidate_categories(candidate)
    changed_lines = _auto_apply_candidate_changed_lines(candidate)
    instruction_token_delta = _auto_apply_instruction_token_delta(
        run_dir,
        candidate_id=candidate_id,
    )
    held_out_eval_passed, governance_regression_passed = _auto_apply_eval_evidence(
        run_dir,
        candidate_id=candidate_id,
    )
    vcs_proof = VcsProof(
        mode=mode,
        commit_sha="pending",
        branch_name=branch_name,
        rollback_commands=(rollback_command,),
    )
    auto_apply_candidate = AutoApplyCandidate(
        candidate_id=str(candidate_id),
        repository=str(repo.resolve()),
        change_class=candidate.risk_class,
        categories=categories,
        held_out_eval_passed=held_out_eval_passed,
        governance_regression_passed=governance_regression_passed,
        rejection_rate=float(metrics["rejection_rate"]),
        rollback_rate=float(metrics["rollback_rate"]),
        changed_lines=changed_lines,
        instruction_token_delta=instruction_token_delta,
        vcs_proof=vcs_proof,
        skill_report_passed=_auto_apply_skill_report_passed(
            run_dir,
            candidate_id=candidate_id,
            categories=categories,
        ),
    )
    readiness = _auto_apply_readiness(
        repo,
        review_actor=review_actor,
        confirmed=confirmed,
        policy_version=policy_version,
        burn_in_days=int(metrics["burn_in_days"]),
    )
    decision = evaluate_auto_apply(
        candidate=auto_apply_candidate,
        readiness=readiness,
    )
    _record_auto_apply_decision(
        repo,
        candidate_id,
        run_dir.name,
        decision.reasons,
        review_actor,
        lane=decision.lane,
        snapshot=_auto_apply_decision_snapshot(
            phase="precheck",
            candidate=auto_apply_candidate,
            readiness=readiness,
            metrics=metrics,
        ),
    )
    if not decision.eligible:
        raise ValueError(f"auto-apply rejected candidate: {', '.join(decision.reasons)}")


def _assert_user_worktree_clean(adapter: VcsAdapter) -> None:
    dirty_paths = tuple(
        path for path in adapter.check_clean_worktree().dirty_paths if not path.startswith(".sidecar/")
    )
    if dirty_paths:
        raise VcsStateError(f"worktree is dirty: {', '.join(dirty_paths)}")


def _assert_auto_apply_final(
    repo: Path,
    run_dir: Path,
    *,
    candidate_id: int,
    candidate: CandidatePatch,
    mode: str,
    branch_name: str,
    applied_commit: str,
    review_actor: str,
    confirmed: bool,
    policy_version: int | None,
) -> dict[str, object]:
    rollback_command = _auto_apply_rollback_command(repo, run_dir)
    metrics = _auto_apply_ledger_metrics(repo, exclude_candidate_id=candidate_id)
    categories = _auto_apply_candidate_categories(candidate)
    changed_lines = _auto_apply_candidate_changed_lines(candidate)
    instruction_token_delta = _auto_apply_instruction_token_delta(
        run_dir,
        candidate_id=candidate_id,
    )
    held_out_eval_passed, governance_regression_passed = _auto_apply_eval_evidence(
        run_dir,
        candidate_id=candidate_id,
    )
    vcs_proof = VcsProof(
        mode=mode,
        commit_sha=applied_commit,
        branch_name=branch_name,
        rollback_commands=(rollback_command,),
    )
    auto_apply_candidate = AutoApplyCandidate(
        candidate_id=str(candidate_id),
        repository=str(repo.resolve()),
        change_class=candidate.risk_class,
        categories=categories,
        held_out_eval_passed=held_out_eval_passed,
        governance_regression_passed=governance_regression_passed,
        rejection_rate=float(metrics["rejection_rate"]),
        rollback_rate=float(metrics["rollback_rate"]),
        changed_lines=changed_lines,
        instruction_token_delta=instruction_token_delta,
        vcs_proof=vcs_proof,
        skill_report_passed=_auto_apply_skill_report_passed(
            run_dir,
            candidate_id=candidate_id,
            categories=categories,
        ),
    )
    readiness = _auto_apply_readiness(
        repo,
        review_actor=review_actor,
        confirmed=confirmed,
        policy_version=policy_version,
        burn_in_days=int(metrics["burn_in_days"]),
    )
    decision = evaluate_auto_apply(
        candidate=auto_apply_candidate,
        readiness=readiness,
    )
    _record_auto_apply_decision(
        repo,
        candidate_id,
        run_dir.name,
        decision.reasons,
        review_actor,
        lane=decision.lane,
        snapshot=_auto_apply_decision_snapshot(
            phase="final",
            candidate=auto_apply_candidate,
            readiness=readiness,
            metrics=metrics,
        ),
    )
    if not decision.eligible or decision.approval_bundle is None:
        raise ValueError(f"auto-apply rejected candidate: {', '.join(decision.reasons)}")
    bundle = decision.approval_bundle.to_json_dict()
    bundle["readiness_metrics"] = metrics
    return bundle


def _auto_apply_ledger_metrics(
    repo: Path,
    *,
    exclude_candidate_id: int | None = None,
) -> dict[str, object]:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        reviewed_row = store.connection.execute(
            """
            SELECT COUNT(*),
                   SUM(CASE WHEN decision = 'rejected' THEN 1 ELSE 0 END),
                   MIN(created_at),
                   MIN(audit_event_sequence),
                   MAX(audit_event_sequence)
            FROM decisions
            WHERE policy = 'deterministic_policy_gate'
              AND decision IN ('needs_review', 'rejected')
              AND (? IS NULL OR candidate_id != ?)
            """,
            (exclude_candidate_id, exclude_candidate_id),
        ).fetchone()
        applied_rows = store.connection.execute(
            """
            SELECT candidate_id, audit_event_sequence
            FROM decisions
            WHERE decision = 'applied'
              AND policy = 'auto_apply_controller'
              AND (? IS NULL OR candidate_id != ?)
            """,
            (exclude_candidate_id, exclude_candidate_id),
        ).fetchall()
        rollback_rows = store.connection.execute(
            """
            SELECT sequence, payload_json
            FROM audit_events
            WHERE event_type = 'rollback.applied'
            """
        ).fetchall()

    reviewed_count = int(reviewed_row[0] or 0)
    rejected_count = int(reviewed_row[1] or 0)
    auto_applied_candidate_ids = {
        int(row[0]) for row in applied_rows if row[0] is not None
    }
    applied_count = len(auto_applied_candidate_ids)
    rollback_sequences: list[int] = []
    for sequence, payload_json in rollback_rows:
        try:
            payload = json.loads(str(payload_json))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        raw_candidate_id = payload.get("candidate_id")
        try:
            rollback_candidate_id = int(raw_candidate_id)
        except (TypeError, ValueError):
            continue
        if rollback_candidate_id in auto_applied_candidate_ids:
            rollback_sequences.append(int(sequence))
    rollback_count = len(rollback_sequences)
    burn_in_days = _burn_in_days(str(reviewed_row[2])) if reviewed_row[2] else 0
    source_sequences = [
        value
        for value in (
            reviewed_row[3],
            reviewed_row[4],
            *(row[1] for row in applied_rows),
            *rollback_sequences,
        )
        if value is not None
    ]
    return {
        "applied_count": applied_count,
        "burn_in_days": burn_in_days,
        "rejected_count": rejected_count,
        "rejection_rate": rejected_count / reviewed_count if reviewed_count else 1.0,
        "reviewed_count": reviewed_count,
        "rollback_count": rollback_count,
        "rollback_rate": rollback_count / applied_count if applied_count else 1.0,
        "source_audit_range": {
            "first_sequence": min(source_sequences) if source_sequences else None,
            "last_sequence": max(source_sequences) if source_sequences else None,
        },
    }


def _burn_in_days(created_at: str) -> int:
    started = datetime.fromisoformat(created_at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - started).days)


def _auto_apply_readiness(
    repo: Path,
    *,
    review_actor: str,
    confirmed: bool,
    policy_version: int | None,
    burn_in_days: int,
) -> AutoApplyReadiness:
    policy = load_policy(repo)
    active_incidents = _auto_apply_active_incidents(repo)
    return AutoApplyReadiness(
        burn_in_days=burn_in_days,
        policy=AutoApplyPolicy(
            enabled=policy.auto_apply_enabled,
            version=policy.version,
            allowed_repositories=policy.auto_apply_allowed_repositories,
            allowed_change_classes=policy.auto_apply_allowed_risk_classes,
            paused_repositories=policy.auto_apply_paused_repositories,
            paused_lanes=policy.auto_apply_paused_lanes,
            paused_categories=policy.auto_apply_paused_categories,
            pause_for_incident=policy.auto_apply_pause_for_incident,
            minimum_burn_in_days=policy.auto_apply_minimum_burn_in_days,
            maximum_rejection_rate=policy.auto_apply_maximum_rejection_rate,
            maximum_rollback_rate=policy.auto_apply_maximum_rollback_rate,
            max_changed_lines=policy.auto_apply_max_changed_lines,
            max_instruction_token_delta=policy.auto_apply_max_instruction_token_delta,
            lanes=tuple(
                AutoApplyLanePolicy(
                    name=lane.name,
                    enabled=lane.enabled,
                    allowed_categories=lane.allowed_categories,
                    allowed_change_classes=lane.allowed_risk_classes,
                    max_changed_lines=lane.max_changed_lines,
                    max_instruction_token_delta=lane.max_instruction_token_delta,
                    minimum_burn_in_days=lane.minimum_burn_in_days,
                    maximum_rejection_rate=lane.maximum_rejection_rate,
                    maximum_rollback_rate=lane.maximum_rollback_rate,
                )
                for lane in policy.auto_apply_lanes
            ),
        ),
        confirmation=AutoApplyConfirmation(
            confirmed=confirmed,
            actor=review_actor if confirmed else "",
            policy_version=policy_version if confirmed else None,
        ),
        active_incidents=active_incidents,
    )


def _auto_apply_active_incidents(repo: Path) -> tuple[AutoApplyIncidentState, ...]:
    db_path = sidecar_dir(repo) / "db.sqlite"
    if not db_path.exists():
        return ()
    active_by_candidate: dict[int, AutoApplyIncidentState] = {}
    with Store.open(db_path) as store:
        rows = store.connection.execute(
            """
            SELECT event_type, payload_json
            FROM audit_events
            WHERE event_type IN ('rollback.failed', 'rollback.applied')
            ORDER BY sequence
            """
        ).fetchall()
    for event_type, payload_json in rows:
        try:
            payload = json.loads(str(payload_json))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        candidate_id = payload.get("candidate_id")
        if isinstance(candidate_id, bool):
            continue
        try:
            candidate_key = int(candidate_id)
        except (TypeError, ValueError):
            continue
        if str(event_type) == "rollback.applied":
            active_by_candidate.pop(candidate_key, None)
            continue
        incident_ref = payload.get("incident")
        failure_kind = payload.get("failure_kind")
        incident = str(incident_ref) if isinstance(incident_ref, str) else ""
        artifact_valid, artifact_status = _auto_apply_incident_artifact_status(
            repo,
            incident=incident,
            candidate_id=candidate_key,
        )
        active_by_candidate[candidate_key] = AutoApplyIncidentState(
            artifact_status=artifact_status,
            artifact_valid=artifact_valid,
            candidate_id=candidate_key,
            event_type="rollback.failed",
            failure_kind=str(failure_kind) if isinstance(failure_kind, str) else "",
            incident=incident,
        )
    return tuple(active_by_candidate[candidate_id] for candidate_id in sorted(active_by_candidate))


def _auto_apply_incident_artifact_status(
    repo: Path,
    *,
    incident: str,
    candidate_id: int,
) -> tuple[bool, str]:
    if not incident:
        return False, "missing_incident_ref"
    incident_path = (repo / incident).resolve()
    try:
        incident_path.relative_to(repo.resolve())
    except ValueError:
        return False, "outside_repo"
    if not incident_path.exists():
        return False, "missing"
    try:
        payload = json.loads(incident_path.read_text(encoding="utf-8"))
        validate_json_artifact("rollback-incident.json", payload)
    except (OSError, json.JSONDecodeError, ValueError):
        return False, "invalid"
    try:
        incident_candidate_id = int(payload.get("candidate_id", -1))
    except (TypeError, ValueError):
        return False, "candidate_mismatch"
    if incident_candidate_id != candidate_id:
        return False, "candidate_mismatch"
    if payload.get("rollback_applied") is not False or payload.get("rollback_plan_written") is not False:
        return False, "not_active"
    return True, "valid"


def _auto_apply_rollback_command(repo: Path, run_dir: Path) -> tuple[str, ...]:
    return (
        "tugboat",
        "rollback",
        "--repo",
        str(repo.resolve()),
        "--decision",
        run_dir.name,
        "--execute",
    )


def _auto_apply_candidate_categories(candidate: CandidatePatch) -> tuple[str, ...]:
    categories = [candidate.risk_class]
    for metadata in candidate.bounded_edit_metadata:
        section = metadata.get("section")
        if isinstance(section, str) and section.strip():
            categories.append(section.strip().lower().replace("-", "_").replace(" ", "_"))
    return tuple(categories)


def _auto_apply_candidate_changed_lines(candidate: CandidatePatch) -> int:
    changed_lines = 0
    for metadata in candidate.bounded_edit_metadata:
        raw_value = metadata.get("changed_lines")
        if isinstance(raw_value, bool):
            continue
        if isinstance(raw_value, int):
            changed_lines += max(0, raw_value)
    return changed_lines


def _auto_apply_instruction_token_delta(run_dir: Path, *, candidate_id: int) -> int | None:
    eval_report_path = run_dir / "eval-report.json"
    if not eval_report_path.exists():
        return None
    try:
        eval_report = json.loads(eval_report_path.read_text(encoding="utf-8"))
        validate_json_artifact("eval-report.json", eval_report)
        if int(eval_report["candidate_id"]) != candidate_id:
            return None
        metrics = eval_report.get("metrics", {})
        if not isinstance(metrics, dict):
            return None
        raw_delta = metrics.get("instruction_token_delta")
        if raw_delta is None:
            return None
        if isinstance(raw_delta, bool):
            return None
        return int(raw_delta)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _auto_apply_eval_evidence(run_dir: Path, *, candidate_id: int) -> tuple[bool, bool]:
    eval_report_path = run_dir / "eval-report.json"
    policy_gate_path = run_dir / "policy-gate.json"
    if not eval_report_path.exists() or not policy_gate_path.exists():
        return False, False
    try:
        eval_report = json.loads(eval_report_path.read_text(encoding="utf-8"))
        validate_json_artifact("eval-report.json", eval_report)
        policy_gate = json.loads(policy_gate_path.read_text(encoding="utf-8"))
        validate_json_artifact("policy-gate.json", policy_gate)
        if int(eval_report["candidate_id"]) != candidate_id:
            return False, False
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False, False
    return (
        _auto_apply_held_out_eval_passed(eval_report),
        bool(eval_report.get("governance_passed", False)) and bool(policy_gate.get("allowed", False)),
    )


def _auto_apply_skill_report_passed(
    run_dir: Path,
    *,
    candidate_id: int,
    categories: Sequence[str],
) -> bool:
    category_keys = {
        category.strip().lower().replace("-", "_").replace(" ", "_") for category in categories
    }
    if "skill_improvement" not in category_keys:
        return True
    eval_report_path = run_dir / "eval-report.json"
    if not eval_report_path.exists():
        return False
    try:
        eval_report = json.loads(eval_report_path.read_text(encoding="utf-8"))
        validate_json_artifact("eval-report.json", eval_report)
        if int(eval_report["candidate_id"]) != candidate_id:
            return False
        skill_report = eval_report.get("skill_report")
        if not isinstance(skill_report, dict):
            return False
        return bool(skill_report.get("passed", False))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _auto_apply_held_out_eval_passed(eval_report: dict[str, object]) -> bool:
    try:
        if str(eval_report.get("recommendation")) != "accept":
            return False
        trigger_score = eval_report.get("trigger_score")
        held_out_score = eval_report.get("held_out_score")
        if trigger_score is None or held_out_score is None:
            return False
        if float(held_out_score) <= float(trigger_score):
            return False
        if _eval_validation_split_failure(eval_report) is not None:
            return False
        regression_score = _score_from_eval_report(eval_report, "regression_score")
        baseline_regression_score = _score_from_eval_report(eval_report, "baseline_regression_score")
        if regression_score is None and baseline_regression_score is None:
            return True
        if regression_score is None or baseline_regression_score is None:
            return False
        regression_tolerance = _score_from_eval_report(eval_report, "regression_tolerance") or 0.0
        return regression_score <= baseline_regression_score + regression_tolerance
    except (TypeError, ValueError):
        return False


def _auto_apply_decision_snapshot(
    *,
    phase: str,
    candidate: AutoApplyCandidate,
    readiness: AutoApplyReadiness,
    metrics: dict[str, object],
) -> dict[str, object]:
    policy = readiness.policy
    confirmation = readiness.confirmation
    vcs = candidate.vcs_proof
    return {
        "phase": phase,
        "candidate": {
            "candidate_id": candidate.candidate_id,
            "repository": candidate.repository,
            "change_class": candidate.change_class,
            "categories": list(candidate.categories),
            "held_out_eval_passed": candidate.held_out_eval_passed,
            "governance_regression_passed": candidate.governance_regression_passed,
            "changed_lines": candidate.changed_lines,
            "instruction_token_delta": candidate.instruction_token_delta,
            "skill_report_passed": candidate.skill_report_passed,
        },
        "policy": None
        if policy is None
        else {
            "enabled": policy.enabled,
            "version": policy.version,
            "allowed_repositories": list(policy.allowed_repositories),
            "allowed_change_classes": list(policy.allowed_change_classes),
            "paused_repositories": list(policy.paused_repositories),
            "paused_lanes": list(policy.paused_lanes),
            "paused_categories": list(policy.paused_categories),
            "pause_for_incident": policy.pause_for_incident,
            "minimum_burn_in_days": policy.minimum_burn_in_days,
            "maximum_rejection_rate": policy.maximum_rejection_rate,
            "maximum_rollback_rate": policy.maximum_rollback_rate,
            "max_changed_lines": policy.max_changed_lines,
            "max_instruction_token_delta": policy.max_instruction_token_delta,
            "lanes": [
                {
                    "name": lane.name,
                    "enabled": lane.enabled,
                    "allowed_categories": list(lane.allowed_categories),
                    "allowed_change_classes": list(lane.allowed_change_classes),
                    "max_changed_lines": lane.max_changed_lines,
                    "max_instruction_token_delta": lane.max_instruction_token_delta,
                    "minimum_burn_in_days": lane.minimum_burn_in_days,
                    "maximum_rejection_rate": lane.maximum_rejection_rate,
                    "maximum_rollback_rate": lane.maximum_rollback_rate,
                }
                for lane in policy.lanes
            ],
        },
        "confirmation": None
        if confirmation is None
        else {
            "confirmed": confirmation.confirmed,
            "actor": confirmation.actor,
            "policy_version": confirmation.policy_version,
        },
        "incident_active": bool(readiness.active_incidents),
        "active_incidents": [incident.to_json_dict() for incident in readiness.active_incidents],
        "readiness_metrics": metrics,
        "vcs": {
            "mode": vcs.mode,
            "commit_sha": vcs.commit_sha,
            "branch_name": vcs.branch_name,
            "rollback_commands": [list(command) for command in vcs.rollback_commands],
        },
    }


def _record_auto_apply_decision(
    repo: Path,
    candidate_id: int,
    run_id: str,
    reasons: tuple[str, ...],
    actor: str,
    *,
    lane: str | None,
    snapshot: dict[str, object],
) -> None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": candidate_id,
                "run_id": run_id,
                "actor": actor,
                "eligible": not reasons,
                "lane": lane,
                "reasons": list(reasons),
                **snapshot,
            },
        )


def _record_auto_apply_shadow(
    repo: Path,
    *,
    candidate_id: int,
    run_id: str,
    actor: str,
    eligible: bool,
    would_apply: bool,
    lane: str | None,
    reasons: tuple[str, ...],
    report_path: str,
) -> None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "auto_apply.shadowed",
            {
                "candidate_id": candidate_id,
                "run_id": run_id,
                "actor": actor,
                "eligible": eligible,
                "would_apply": would_apply,
                "lane": lane,
                "reasons": list(reasons),
                "report_path": report_path,
                "phase": "shadow",
            },
        )


def _apply_applied_event_payload(
    repo: Path,
    run_dir: Path,
    *,
    candidate_id: int,
    mode: str,
    target_files: tuple[str, ...],
    applied_commit: str,
    pre_hashes: dict[str, str],
    post_hashes: dict[str, str],
    provenance_bundle: str,
    rollback_command: list[list[str]],
    pr_result: dict[str, object] | None = None,
) -> dict[str, object]:
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    policy_gate = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    payload: dict[str, object] = {
        "candidate_id": candidate_id,
        "mode": mode,
        "run_id": run_dir.name,
        "target_files": list(target_files),
        "applied_commit": applied_commit,
        "eval_report": {
            "path": _relative_repo_path(repo, run_dir / "eval-report.json"),
            "suite_id": str(eval_report["suite_id"]),
            "passed": bool(eval_report["passed"]),
        },
        "policy_gate": {
            "path": _relative_repo_path(repo, run_dir / "policy-gate.json"),
            "allowed": bool(policy_gate["allowed"]),
            "reasons": list(policy_gate["reasons"]),
        },
        "pre_hashes": pre_hashes,
        "post_hashes": post_hashes,
        "provenance_bundle": provenance_bundle,
        "rollback_command": rollback_command,
    }
    if pr_result:
        payload["pr_result"] = pr_result
    return payload


def _relative_repo_path(repo: Path, path: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _pull_request_artifact_refs(repo: Path, run_dir: Path) -> tuple[tuple[str, str], ...]:
    return (
        ("candidate_diff", _relative_repo_path(repo, run_dir / "candidate.diff")),
        ("eval_report", _relative_repo_path(repo, run_dir / "eval-report.json")),
        ("policy_gate", _relative_repo_path(repo, run_dir / "policy-gate.json")),
        ("apply_plan", _relative_repo_path(repo, run_dir / "apply-plan.json")),
        ("provenance_bundle", _relative_repo_path(repo, run_dir / "provenance-bundle.json")),
    )


def _read_optional_json_object(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must be a JSON object")
    return payload


def _assert_eval_acceptance(
    eval_report: dict[str, object],
    policy_gate: dict[str, object] | None = None,
    *,
    validation_baseline_score: float | None = None,
) -> None:
    recommendation = eval_report.get("recommendation")
    if recommendation is not None and str(recommendation) != "accept":
        raise ValueError(f"eval report recommendation was {recommendation}")
    if not bool(eval_report.get("governance_passed", False)):
        raise ValueError("eval governance did not pass")
    if policy_gate is not None and not bool(policy_gate.get("allowed", False)):
        raise ValueError("eval policy gate rejected candidate")
    trigger_score = eval_report.get("trigger_score")
    held_out_score = eval_report.get("held_out_score")
    if trigger_score is None or held_out_score is None:
        metrics = eval_report.get("metrics", {})
        if not isinstance(metrics, dict):
            raise ValueError("eval report metrics must be an object")
        trigger_score = metrics.get("trigger_score")
        held_out_score = metrics.get("held_out_score")
        if trigger_score is None or held_out_score is None:
            raise ValueError("eval report is missing trigger and held-out validation scores")
    if float(held_out_score) <= float(trigger_score):
        raise ValueError("held-out eval score did not improve")
    if validation_baseline_score is not None and float(held_out_score) <= validation_baseline_score:
        raise ValueError("held-out eval score did not improve over baseline")
    split_failure = _eval_validation_split_failure(eval_report)
    if split_failure is not None:
        raise ValueError(split_failure)
    regression_score = _score_from_eval_report(eval_report, "regression_score")
    baseline_regression_score = _score_from_eval_report(eval_report, "baseline_regression_score")
    if regression_score is not None or baseline_regression_score is not None:
        if regression_score is None or baseline_regression_score is None:
            raise ValueError("eval report is missing regression validation scores")
        regression_tolerance = _score_from_eval_report(eval_report, "regression_tolerance") or 0.0
        if regression_score > baseline_regression_score + regression_tolerance:
            raise ValueError("regression score degraded beyond tolerance")
    if not _eval_has_incident_replay_cases(eval_report):
        raise ValueError("eval report cannot accept without incident replay cases")


def _assert_apply_recorded_provenance(
    repo: Path,
    run_dir: Path,
    *,
    candidate_id: int,
    suite_id: str,
) -> dict[str, object]:
    expected_diff_path = (run_dir / "candidate.diff").resolve()
    expected_eval_path = (run_dir / "eval-report.json").resolve()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        candidate_row = store.connection.execute(
            """
            SELECT candidate.audit_id, candidate.diff_path, candidate.audit_event_sequence,
                   audit.audit_event_sequence
            FROM candidates candidate
            JOIN audits audit ON audit.id = candidate.audit_id
            WHERE candidate.id = ?
            """,
            (candidate_id,),
        ).fetchone()
        if candidate_row is None:
            raise ValueError("candidate provenance is not recorded")
        if Path(str(candidate_row[1])).resolve() != expected_diff_path:
            raise ValueError("candidate provenance does not match run artifacts")

        eval_row = store.connection.execute(
            """
            SELECT id, report_path, audit_event_sequence
            FROM evals
            WHERE candidate_id = ? AND suite_id = ? AND passed = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (candidate_id, suite_id),
        ).fetchone()
        if eval_row is None:
            raise ValueError("eval provenance is not recorded")
        if Path(str(eval_row[1])).resolve() != expected_eval_path:
            raise ValueError("eval provenance does not match run artifacts")

        decision_row = store.connection.execute(
            """
            SELECT id, audit_event_sequence
            FROM decisions
            WHERE candidate_id = ?
              AND policy = 'deterministic_policy_gate'
              AND decision = 'needs_review'
            ORDER BY id DESC
            LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        if decision_row is None:
            raise ValueError("policy decision provenance is not recorded")
        return {
            "audit_id": int(candidate_row[0]),
            "eval_id": int(eval_row[0]),
            "policy_decision_id": int(decision_row[0]),
            "audit_event_sequences": {
                "audit": int(candidate_row[3]),
                "candidate": int(candidate_row[2]),
                "eval": int(eval_row[2]),
                "policy_decision": int(decision_row[1]),
            },
        }


def _eval_validation_split_failure(eval_report: dict[str, object]) -> str | None:
    raw_splits = eval_report.get("validation_splits")
    if raw_splits is None:
        return "eval report cannot accept without validation split provenance"
    if not isinstance(raw_splits, dict):
        return "eval report validation_splits must be an object"
    splits: dict[str, set[str]] = {}
    for split_name, raw_case_ids in raw_splits.items():
        if not isinstance(split_name, str) or not split_name.strip():
            return "eval report validation_splits keys must be non-empty strings"
        if not isinstance(raw_case_ids, list) or not all(
            isinstance(case_id, str) and case_id.strip() for case_id in raw_case_ids
        ):
            return f"eval report validation_splits.{split_name} must be a JSON list of strings"
        splits[split_name] = set(raw_case_ids)
    trigger_cases = splits.get("trigger", set())
    held_out_cases = splits.get("held_out", set())
    if not trigger_cases:
        return "eval report cannot accept without triggering validation cases"
    if not held_out_cases:
        return "eval report cannot accept without held-out validation case IDs"
    overlap = sorted(trigger_cases & held_out_cases)
    if overlap:
        return (
            "eval report triggering validation cases overlap held-out validation cases: "
            + ", ".join(overlap)
        )
    return None


def _eval_has_incident_replay_cases(eval_report: dict[str, object]) -> bool:
    metrics = eval_report.get("metrics", {})
    if isinstance(metrics, dict):
        raw = metrics.get("incident_replay_cases")
        if not isinstance(raw, bool) and isinstance(raw, int | float) and raw > 0:
            return True
    raw_splits = eval_report.get("validation_splits")
    if not isinstance(raw_splits, dict):
        return False
    return any(
        isinstance(case_id, str) and case_id.startswith("incident_replay:")
        for raw_case_ids in raw_splits.values()
        if isinstance(raw_case_ids, list)
        for case_id in raw_case_ids
    )


def _validation_baseline_key(suite_id: str) -> str:
    return f"validation_baseline:{suite_id}"


def _repo_memory_path(repo: Path) -> str:
    return str(repo.resolve())


def _load_validation_baseline_score(repo: Path, *, suite_id: str) -> float | None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        return _read_validation_baseline_score(store, repo=repo, suite_id=suite_id)


def _load_rejected_edit_fingerprints(repo: Path) -> tuple[str, ...]:
    db_path = sidecar_dir(repo) / "db.sqlite"
    if not db_path.exists():
        return ()
    try:
        with Store.open(db_path) as store:
            rows = store.connection.execute(
                """
                SELECT key
                FROM optimizer_memory
                WHERE repo_path = ?
                  AND memory_type = 'rejected_edit'
                ORDER BY id
                """,
                (_repo_memory_path(repo),),
            ).fetchall()
    except sqlite3.DatabaseError:
        return ()
    return tuple(str(row[0]) for row in rows)


def _read_validation_baseline_score(store: Store, *, repo: Path, suite_id: str) -> float | None:
    row = store.connection.execute(
        """
        SELECT payload_json
        FROM optimizer_memory
        WHERE repo_path = ?
          AND memory_type = 'validation_baseline'
          AND key = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (_repo_memory_path(repo), _validation_baseline_key(suite_id)),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict):
        raise ValueError("validation baseline memory payload must be an object")
    score = payload.get("held_out_score")
    if score is None:
        raise ValueError("validation baseline memory is missing held_out_score")
    return float(score)


def _record_validation_baseline_score(
    store: Store,
    *,
    repo: Path,
    suite_id: str,
    candidate_id: int,
    held_out_score: float,
) -> None:
    store.record_optimizer_memory(
        repo_path=_repo_memory_path(repo),
        memory_type="validation_baseline",
        key=_validation_baseline_key(suite_id),
        payload={
            "candidate_id": candidate_id,
            "held_out_score": held_out_score,
            "suite_id": suite_id,
        },
    )


def _record_optimize_minibatch_guidance(
    repo: Path,
    run_dir: Path,
    *,
    suite_id: str,
    held_out_episodes: tuple[str, ...] = (),
    unseen_suites: tuple[str, ...] = (),
) -> None:
    outcomes = _episode_outcomes_for_minibatch(repo, current_run_dir=run_dir)
    if not outcomes:
        return
    minibatch = build_success_failure_minibatch(tuple(outcomes))
    batches = build_minibatches(
        train_episodes=tuple(outcome.episode_id for outcome in outcomes),
        held_out_episodes=held_out_episodes,
        unseen_suites=unseen_suites,
    )
    batch_payload = {
        "schema_version": SCHEMA_VERSION,
        "failure_episodes": list(minibatch.failure_episodes),
        "failure_patterns": list(minibatch.failure_patterns),
        "held_out_episodes": list(batches.held_out_episodes),
        "held_out_suite": suite_id,
        "success_episodes": list(minibatch.success_episodes),
        "success_patterns": list(minibatch.success_patterns),
        "train_episodes": list(batches.train_episodes),
        "unseen_suites": list(batches.unseen_suites),
    }
    validate_json_artifact("optimization-batch.json", batch_payload)
    write_json_artifact(run_dir / "optimization-batch.json", batch_payload)
    _write_batch_audit_reports(repo, run_dir, train_episodes=set(batches.train_episodes))
    _write_optimization_reflection_artifact(repo, run_dir, minibatch, suite_id=suite_id)
    guidance = _optimizer_guidance_from_minibatch(minibatch, suite_id=suite_id)
    if guidance is None:
        return
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=_repo_memory_path(repo),
            memory_type="slow_update",
            key=f"optimize_minibatch:{run_dir.name}",
            payload={
                "category": "optimizer_guidance",
                "legacy_note": f"optimizer_guidance: {guidance}",
                "note": guidance,
            },
        )


def run_optimize_workflow(
    repo: Path,
    trace: Path,
    *,
    suite_id: str,
    train_traces: tuple[Path, ...] = (),
    held_out_episodes: tuple[str, ...] = (),
    unseen_suites: tuple[str, ...] = (),
    trace_format: str = "auto",
) -> OptimizeWorkflowResult:
    for train_trace in train_traces:
        train_result = run_audit_pipeline(repo, train_trace, trace_format=trace_format)
        print(train_result.message)
        if train_result.exit_code != 0:
            _print_trace_blocked_next_step(train_trace, train_result.message)
            return OptimizeWorkflowResult(
                train_result.exit_code,
                train_result.run_dir,
                train_result.message,
            )
    audit_result = run_audit_pipeline(repo, trace, trace_format=trace_format)
    print(audit_result.message)
    if audit_result.exit_code != 0:
        _print_trace_blocked_next_step(trace, audit_result.message)
        return OptimizeWorkflowResult(
            audit_result.exit_code,
            audit_result.run_dir,
            audit_result.message,
        )
    run_dir = audit_result.run_dir
    trigger_run_id = run_dir.name
    try:
        _record_optimize_minibatch_guidance(
            repo,
            run_dir,
            suite_id=suite_id,
            held_out_episodes=held_out_episodes,
            unseen_suites=unseen_suites,
        )
    except ValueError as error:
        message = f"optimize blocked: {error}"
        print(message)
        return OptimizeWorkflowResult(1, run_dir, message)
    propose_result = run_propose_pipeline(repo, trigger_run_id)
    print(propose_result.message)
    run_dir = runs_dir(repo) / trigger_run_id
    if propose_result.exit_code != 0:
        if not (run_dir / "candidate.json").exists():
            return OptimizeWorkflowResult(
                propose_result.exit_code,
                run_dir,
                propose_result.message,
            )
        exit_code = _write_optimization_summary(repo, run_dir, suite_id=suite_id)
        return OptimizeWorkflowResult(exit_code, run_dir, "optimization summary written")
    eval_result = run_eval_pipeline(repo, trigger_run_id, suite_id)
    print(eval_result.message)
    exit_code = _finalize_governed_candidate_evaluation(
        repo,
        run_dir,
        suite_id=suite_id,
        eval_exit_code=eval_result.exit_code,
        unseen_suites=unseen_suites,
    )
    if exit_code == 0:
        try:
            report_path = write_report(
                repo,
                trigger_run_id,
                candidate=_candidate_from_artifacts(run_dir),
                decision=_decision_from_artifact(run_dir),
                eval_report_path=run_dir / "eval-report.json",
            )
        except (ArtifactValidationError, FileNotFoundError, KeyError, SecretScanError, ValueError) as error:
            message = f"optimize blocked: report generation failed: {error}"
            print(message)
            return OptimizeWorkflowResult(1, run_dir, message)
        print(f"report: {report_path}")
    return OptimizeWorkflowResult(exit_code, run_dir, eval_result.message)


def _write_optimization_reflection_artifact(repo: Path, run_dir: Path, minibatch, *, suite_id: str) -> None:
    artifact = reflect_on_minibatch(
        failure_patterns=minibatch.failure_patterns,
        success_patterns=minibatch.success_patterns,
        affected_instruction_chunks=_affected_instruction_chunks_for_reflection(repo, run_dir),
        proposed_root_cause=_reflection_root_cause(minibatch),
    )
    payload = {
        "source_ref": "optimization-batch.json",
        "summary": (
            f"SkillOpt reflection for held-out suite {suite_id}: "
            f"{len(artifact.recurring_failure_patterns)} failure pattern"
            f"{'' if len(artifact.recurring_failure_patterns) == 1 else 's'}, "
            f"{len(artifact.preserved_success_patterns)} success pattern"
            f"{'' if len(artifact.preserved_success_patterns) == 1 else 's'}"
        ),
        "recurring_failure_patterns": list(artifact.recurring_failure_patterns),
        "preserved_success_patterns": list(artifact.preserved_success_patterns),
        "affected_instruction_chunks": list(artifact.affected_instruction_chunks),
        "proposed_root_cause": artifact.proposed_root_cause,
    }
    validate_json_artifact("reflection.json", payload)
    artifact_path = run_dir / "reflection.json"
    write_json_artifact(artifact_path, payload)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_reflection(
            run_id=run_dir.name,
            source_ref=str(payload["source_ref"]),
            artifact_path=artifact_path,
        )


def _affected_instruction_chunks_for_reflection(repo: Path, run_dir: Path) -> tuple[str, ...]:
    chunks: list[str] = []
    reports_path = run_dir / "batch-audit-reports.json"
    if reports_path.exists():
        reports_payload = load_json_object_artifact(reports_path, "batch-audit-reports.json")
        validate_json_artifact("batch-audit-reports.json", reports_payload)
        reports = reports_payload.get("reports", [])
        if isinstance(reports, list):
            for report in reports:
                if not isinstance(report, dict):
                    continue
                audit_path = run_dir / str(report.get("path", ""))
                if not audit_path.exists():
                    continue
                audit = load_json_object_artifact(audit_path, "audit.raw.json")
                validate_json_artifact("audit.raw.json", audit)
                chunks.extend(_ordered_json_strings(audit.get("instruction_refs", [])))
    if chunks:
        return tuple(dict.fromkeys(chunks))
    return tuple(item.path for item in load_policy(repo).instruction_files if item.protected)


def _ordered_json_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _reflection_root_cause(minibatch) -> str:
    if minibatch.failure_patterns:
        return "Recurring failures indicate instruction guidance is incomplete."
    if minibatch.success_patterns:
        return "Successful patterns identify instruction guidance to preserve."
    return "No labeled optimization patterns were available."


def _write_batch_audit_reports(
    repo: Path,
    run_dir: Path,
    *,
    train_episodes: set[str],
) -> None:
    reports: list[dict[str, object]] = []
    for candidate_run_dir in sorted(runs_dir(repo).iterdir()):
        if not candidate_run_dir.is_dir() or candidate_run_dir.name > run_dir.name:
            continue
        audit_path = candidate_run_dir / "audit.raw.json"
        if not audit_path.exists():
            continue
        episode_id = _episode_id_for_run(repo, candidate_run_dir)
        if episode_id not in train_episodes:
            continue
        audit = load_json_object_artifact(audit_path, "audit.raw.json")
        validate_json_artifact("audit.raw.json", audit)
        evidence_refs = audit.get("evidence_refs", [])
        if not isinstance(evidence_refs, list):
            raise ValueError("audit.raw.json evidence_refs must be a JSON list")
        raw_evidence_refs = [str(ref) for ref in evidence_refs]
        reports.append(
            {
                "run_id": candidate_run_dir.name,
                "episode_id": episode_id,
                "split": "trigger" if candidate_run_dir == run_dir else "train",
                "path": (
                    "audit.raw.json"
                    if candidate_run_dir == run_dir
                    else f"../{candidate_run_dir.name}/audit.raw.json"
                ),
                "evidence_refs": raw_evidence_refs,
                "source_refs": [
                    f"audit:{candidate_run_dir.name}:{evidence_ref}"
                    for evidence_ref in raw_evidence_refs
                ],
            }
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "primary_audit": "audit.raw.json",
        "reports": reports,
    }
    validate_json_artifact("batch-audit-reports.json", payload)
    write_json_artifact(run_dir / "batch-audit-reports.json", payload)


def _episode_outcomes_for_minibatch(
    repo: Path,
    *,
    current_run_dir: Path,
) -> list[EpisodeOutcome]:
    outcomes: list[EpisodeOutcome] = []
    candidate_run_dirs = [
        path
        for path in sorted(runs_dir(repo).iterdir())
        if path.is_dir() and path.name <= current_run_dir.name
    ]
    for run_dir in candidate_run_dirs:
        canonical_episode_path = run_dir / "canonical-episode.json"
        if not canonical_episode_path.exists():
            continue
        episode = json.loads(canonical_episode_path.read_text(encoding="utf-8"))
        if not isinstance(episode, dict):
            raise ValueError("canonical-episode.json must be a JSON object")
        outcome = _episode_outcome_from_labels(episode.get("outcome_labels", []))
        if outcome is None:
            continue
        outcomes.append(
            EpisodeOutcome(
                episode_id=_episode_id_for_run(repo, run_dir),
                outcome=outcome,
                pattern=_episode_pattern_from_canonical_episode(episode, outcome=outcome),
            )
        )
    return outcomes


def _episode_outcome_from_labels(labels: object) -> str | None:
    if not isinstance(labels, list):
        return None
    normalized = {str(label).strip().lower().replace("-", "_") for label in labels}
    if normalized & {"accepted", "success", "succeeded", "passed", "ci_passed"}:
        return "success"
    if normalized & {"rejected", "failure", "failed", "ci_failed"}:
        return "failure"
    return None


def _episode_pattern_from_canonical_episode(episode: dict[str, object], *, outcome: str) -> str:
    corrections = episode.get("user_corrections", [])
    if isinstance(corrections, list):
        for correction in corrections:
            if isinstance(correction, dict):
                payload = correction.get("payload", {})
                if isinstance(payload, dict):
                    content = str(payload.get("content", "")).strip()
                    if content:
                        return content
    request = str(episode.get("request", "")).strip()
    if request:
        return request
    return f"{outcome} episode"


def _episode_id_for_run(repo: Path, run_dir: Path) -> str:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        row = store.connection.execute(
            "SELECT episode_id FROM runs WHERE id = ?",
            (run_dir.name,),
        ).fetchone()
    if row is None or row[0] is None:
        return run_dir.name
    return str(row[0])


def _optimizer_guidance_from_minibatch(minibatch, *, suite_id: str) -> str | None:
    if minibatch.failure_patterns:
        return (
            f"SkillOpt minibatch before held-out suite {suite_id}: "
            f"avoid repeating failure patterns: {'; '.join(minibatch.failure_patterns)}"
        )
    if minibatch.success_patterns:
        return (
            f"SkillOpt minibatch before held-out suite {suite_id}: "
            f"preserve success patterns: {'; '.join(minibatch.success_patterns)}"
        )
    return None


def _record_missing_rejected_edit_memory(
    store: Store,
    *,
    repo: Path,
    candidate: dict[str, object],
    candidate_id: int,
    suite_id: str,
    reason: str,
) -> None:
    raw_metadata = candidate.get("bounded_edit_metadata", [])
    if not isinstance(raw_metadata, list):
        return
    for item in raw_metadata:
        if not isinstance(item, dict):
            continue
        operator = str(item.get("operator", ""))
        target_file = str(item.get("file", candidate.get("base_file", "")))
        section = str(item.get("section", ""))
        if not operator or not target_file or not section:
            continue
        fingerprint = _bounded_edit_fingerprint(operator, target_file, section)
        row = store.connection.execute(
            """
            SELECT 1
            FROM optimizer_memory
            WHERE repo_path = ?
              AND memory_type = 'rejected_edit'
              AND key = ?
            """,
            (_repo_memory_path(repo), fingerprint),
        ).fetchone()
        if row is not None:
            continue
        store.record_optimizer_memory(
            repo_path=_repo_memory_path(repo),
            memory_type="rejected_edit",
            key=fingerprint,
            payload={
                "future_proposal_suppression_signal": REJECTED_EDIT_SUPPRESSION_SIGNAL,
                "rejection_reason": reason,
                "semantic_fingerprint": fingerprint,
                "source_refs": [f"candidate:{candidate_id}", f"suite:{suite_id}"],
            },
        )


def _write_human_review_rejection(
    repo: Path,
    run_dir: Path,
    *,
    actor: str,
    template: str | None,
    reason: str | None,
    category: str | None,
    failure_pattern: str | None,
) -> None:
    resolved = _resolve_review_rejection(template, reason, category, failure_pattern)
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    validate_json_artifact("candidate.json", candidate)
    candidate_id = int(candidate["candidate_id"])
    rejected_memory = _human_rejected_edit_memory_payloads(
        candidate,
        candidate_id=candidate_id,
        actor=actor,
        reason=resolved["reason"],
        category=resolved["category"],
        failure_pattern=resolved["failure_pattern"],
        template=template,
    )
    rejected_cluster_memory = _human_rejected_cluster_memory_payloads(
        run_dir,
        candidate,
        candidate_id=candidate_id,
        actor=actor,
        reason=resolved["reason"],
        category=resolved["category"],
        failure_pattern=resolved["failure_pattern"],
        template=template,
    )
    _scan_human_review_rejection(
        actor=actor,
        reason=resolved["reason"],
        category=resolved["category"],
        failure_pattern=resolved["failure_pattern"],
        template=template,
        rejected_memory=rejected_memory,
        rejected_cluster_memory=rejected_cluster_memory,
    )

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        row = store.connection.execute(
            "SELECT state FROM candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"candidate not found: {candidate_id}")
        if str(row[0]) != "needs_review":
            raise ValueError("candidate is not awaiting review")
        store.update_candidate_state(candidate_id=candidate_id, state="rejected", reason=resolved["reason"])
        store.record_review_action(
            candidate_id=candidate_id,
            actor=actor,
            action="rejected",
            reason=resolved["reason"],
        )
        store.insert_decision(
            candidate_id=candidate_id,
            actor=actor,
            policy="human_review",
            decision="rejected",
            reason=resolved["reason"],
        )
        for fingerprint, payload in rejected_memory:
            store.record_optimizer_memory(
                repo_path=_repo_memory_path(repo),
                memory_type="rejected_edit",
                key=fingerprint,
                payload=payload,
            )
        for cluster_key, payload in rejected_cluster_memory:
            store.record_optimizer_memory(
                repo_path=_repo_memory_path(repo),
                memory_type="rejected_cluster",
                key=cluster_key,
                payload=payload,
            )
        store.append_audit_event(
            "review.rejected",
            {
                "actor": actor,
                "candidate_id": candidate_id,
                "category": resolved["category"],
                "failure_pattern": resolved["failure_pattern"],
                "reason": resolved["reason"],
                "run_id": run_dir.name,
                **({"template": template} if template is not None else {}),
            },
        )
    _merge_json(
        run_dir / "decision.json",
        {
            "decision": "rejected",
            "policy_allowed": False,
            "policy_reasons": [resolved["reason"]],
            "review_actor": actor,
            **({"review_template": template} if template is not None else {}),
        },
    )
    _transition_originating_daemon_job(
        repo,
        run_dir,
        candidate_id=candidate_id,
        source_state=JobState.WAITING_REVIEW,
        target_state=JobState.REJECTED,
    )


def _resolve_review_rejection(
    template: str | None,
    reason: str | None,
    category: str | None,
    failure_pattern: str | None,
) -> dict[str, str]:
    manual_fields = {
        "--reason": reason,
        "--category": category,
        "--failure-pattern": failure_pattern,
    }
    if template is not None and any(value is not None for value in manual_fields.values()):
        raise ValueError("review rejection template cannot be combined with manual fields")
    if template is not None and template not in REVIEW_REJECTION_TEMPLATES:
        raise ValueError(f"unknown review rejection template: {template}")
    values = dict(REVIEW_REJECTION_TEMPLATES.get(template or "", {}))
    if reason is not None:
        values["reason"] = reason
    if category is not None:
        values["category"] = category
    if failure_pattern is not None:
        values["failure_pattern"] = failure_pattern
    missing = [
        flag
        for flag, field in (
            ("--reason", "reason"),
            ("--category", "category"),
            ("--failure-pattern", "failure_pattern"),
        )
        if not values.get(field)
    ]
    if missing:
        raise ValueError(
            "review rejection requires --template or explicit fields: "
            + ", ".join(missing)
        )
    return values


def _human_rejected_edit_memory_payloads(
    candidate: dict[str, object],
    *,
    candidate_id: int,
    actor: str,
    reason: str,
    category: str,
    failure_pattern: str,
    template: str | None = None,
) -> list[tuple[str, dict[str, object]]]:
    raw_metadata = candidate.get("bounded_edit_metadata", [])
    if not isinstance(raw_metadata, list):
        raise ValueError("candidate bounded_edit_metadata is required for rejected edit memory")

    records: list[tuple[str, dict[str, object]]] = []
    for item in raw_metadata:
        if not isinstance(item, dict):
            continue
        operator = str(item.get("operator", ""))
        target_file = str(item.get("file", candidate.get("base_file", "")))
        section = str(item.get("section", ""))
        if not operator or not target_file or not section:
            continue
        fingerprint = _bounded_edit_fingerprint(operator, target_file, section)
        payload = {
            "category": category,
            "failure_pattern": failure_pattern,
            "file": target_file,
            "future_proposal_suppression_signal": REJECTED_EDIT_SUPPRESSION_SIGNAL,
            "operator": operator,
            "rejection_reason": reason,
            "review_actor": actor,
            "section": section,
            "semantic_fingerprint": fingerprint,
            "source_refs": [f"candidate:{candidate_id}", "suite:human_review"],
        }
        if template is not None:
            payload["review_template"] = template
        records.append((fingerprint, payload))
    if not records:
        raise ValueError("candidate bounded_edit_metadata is required for rejected edit memory")
    return records


def _human_rejected_cluster_memory_payloads(
    run_dir: Path,
    candidate: dict[str, object],
    *,
    candidate_id: int,
    actor: str,
    reason: str,
    category: str,
    failure_pattern: str,
    template: str | None = None,
) -> list[tuple[str, dict[str, object]]]:
    drift_path = run_dir / "drift.raw.json"
    if not drift_path.exists():
        return []
    drift = json.loads(drift_path.read_text(encoding="utf-8"))
    validate_json_artifact("drift.raw.json", drift)
    candidate_sources = {
        str(source.get("source_id"))
        for source in candidate.get("sources", [])
        if isinstance(source, dict) and source.get("source_id")
    }
    if not candidate_sources:
        return []

    records: list[tuple[str, dict[str, object]]] = []
    for cluster in drift.get("clusters", []):
        if not isinstance(cluster, dict):
            continue
        evidence_refs = [
            str(ref)
            for ref in cluster.get("evidence_refs", [])
            if isinstance(ref, str)
        ]
        if not candidate_sources.intersection(evidence_refs):
            continue
        cluster_id = str(cluster["cluster_id"])
        payload = {
            "category": category,
            "cluster_id": cluster_id,
            "evidence_refs": evidence_refs,
            "failure_pattern": failure_pattern,
            "rejection_reason": reason,
            "review_actor": actor,
            "source_refs": [
                f"candidate:{candidate_id}",
                f"cluster:{cluster_id}",
                "suite:human_review",
            ],
        }
        if template is not None:
            payload["review_template"] = template
        records.append((_rejected_cluster_memory_key(cluster_id, evidence_refs), payload))
    return records


def _rejected_cluster_memory_key(cluster_id: str, evidence_refs: list[str]) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "cluster_id": cluster_id,
                "evidence_refs": sorted(evidence_refs),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"rejected_cluster:{digest}"


def _scan_human_review_rejection(
    *,
    actor: str,
    reason: str,
    category: str,
    failure_pattern: str,
    template: str | None,
    rejected_memory: list[tuple[str, dict[str, object]]],
    rejected_cluster_memory: list[tuple[str, dict[str, object]]],
) -> None:
    payload = {
        "actor": actor,
        "category": category,
        "failure_pattern": failure_pattern,
        "reason": reason,
        "rejected_cluster_memory": [memory for _, memory in rejected_cluster_memory],
        "rejected_memory": [memory for _, memory in rejected_memory],
    }
    if template is not None:
        payload["template"] = template
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    findings = scan_text("review-rejection.json", text)
    if findings:
        raise SecretScanError(findings)


def _bounded_edit_fingerprint(operator: str, target_file: str, section: str) -> str:
    return hashlib.sha256(f"{operator}\n{target_file}\n{section}".encode("utf-8")).hexdigest()


def _write_optimization_summary(
    repo: Path,
    run_dir: Path,
    *,
    suite_id: str,
    forced_rejection_reason: str | None = None,
    unseen_suite_results: list[dict[str, object]] | None = None,
) -> int:
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate_id = int(candidate["candidate_id"])
    eval_report_path = run_dir / "eval-report.json"
    policy_gate_path = run_dir / "policy-gate.json"
    decision = "rejected"
    reason = "proposal rejected"
    trigger_score: float | None = None
    held_out_score: float | None = None
    validation_baseline_score: float | None = None
    governance_passed = False
    recommendation = "reject"
    validation_baseline_score = _load_validation_baseline_score(repo, suite_id=suite_id)
    accepted_bounded_edit_metadata: list[object] = []
    if eval_report_path.exists():
        eval_report = json.loads(eval_report_path.read_text(encoding="utf-8"))
        validate_json_artifact("eval-report.json", eval_report)
        trigger_score = _score_from_eval_report(eval_report, "trigger_score")
        held_out_score = _score_from_eval_report(eval_report, "held_out_score")
        governance_passed = bool(eval_report.get("governance_passed", False))
        recommendation = str(eval_report.get("recommendation", "reject"))
        try:
            _assert_eval_acceptance(
                eval_report,
                _read_optional_json_object(policy_gate_path),
                validation_baseline_score=validation_baseline_score,
            )
        except ValueError as error:
            reason = str(error)
        else:
            raw_metadata = candidate.get("bounded_edit_metadata", [])
            accepted_bounded_edit_metadata = raw_metadata if isinstance(raw_metadata, list) else []
            if accepted_bounded_edit_metadata:
                decision = "needs_review"
                reason = "held_out_improved"
            else:
                reason = "accepted candidate missing bounded edit metadata"
    if forced_rejection_reason is not None:
        decision = "rejected"
        reason = forced_rejection_reason

    acceptance_summary: dict[str, object] | None = None
    acceptance_summary_path = run_dir / "acceptance-summary.raw.json"
    if decision == "needs_review":
        if not acceptance_summary_path.exists():
            raise ArtifactValidationError("acceptance-summary.raw.json is required for needs_review")
        acceptance_summary = load_json_object_artifact(
            acceptance_summary_path,
            "acceptance-summary.raw.json",
        )
        validate_json_artifact("acceptance-summary.raw.json", acceptance_summary)

    summary = {
        "schema_version": SCHEMA_VERSION,
        "audit_run": run_dir.name,
        "candidate_id": candidate_id,
        "decision": decision,
        "governance_passed": governance_passed,
        "held_out_score": held_out_score,
        "recommendation": recommendation,
        "suite_id": suite_id,
        "trigger_score": trigger_score,
        "validation_baseline_score": validation_baseline_score,
    }
    if unseen_suite_results is not None:
        summary["unseen_suite_results"] = unseen_suite_results
    reflection_path = run_dir / "reflection.json"
    if reflection_path.exists():
        summary["reflection_artifact_path"] = reflection_path.relative_to(repo).as_posix()
    if decision == "needs_review":
        summary["accepted_bounded_edit_metadata"] = accepted_bounded_edit_metadata
        if acceptance_summary is None:
            raise ArtifactValidationError("acceptance-summary.raw.json is required for needs_review")
        summary.update(
            {
                "acceptance_decision_recommendation": acceptance_summary["decision_recommendation"],
                "acceptance_evidence": acceptance_summary["evidence"],
                "acceptance_reasons": acceptance_summary["reasons"],
                "acceptance_summary_path": acceptance_summary_path.relative_to(repo).as_posix(),
                "reviewer_checklist": acceptance_summary["reviewer_checklist"],
                "rollback_command": acceptance_summary["rollback_command"],
            }
        )
    summary_text = _serialize_secret_scanned_json_artifact(
        run_dir / "optimization-summary.json",
        "optimization-summary.json",
        summary,
    )

    _merge_json(
        run_dir / "decision.json",
        {
            "decision": decision,
            "policy_allowed": decision == "needs_review",
            "policy_reasons": [reason],
        },
    )
    optimization_summary_path = run_dir / "optimization-summary.json"
    optimization_summary_path.write_text(summary_text, encoding="utf-8")
    mark_private_file(optimization_summary_path)
    try:
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.update_candidate_state(
                candidate_id=candidate_id,
                state=decision,
                reason=reason,
            )
            store.insert_decision(
                candidate_id=candidate_id,
                actor="tugboat",
                policy="optimization_acceptance_gate",
                decision=decision,
                reason=reason,
            )
            _record_optimization_slow_update(
                store,
                repo=repo,
                candidate_id=candidate_id,
                suite_id=suite_id,
                category="successful" if decision == "needs_review" else "rejected",
                reason=reason,
            )
            if decision == "rejected":
                _record_missing_rejected_edit_memory(
                    store,
                    repo=repo,
                    candidate=candidate,
                    candidate_id=candidate_id,
                    suite_id=suite_id,
                    reason=reason,
                )
            if decision == "needs_review" and held_out_score is not None:
                _record_validation_baseline_score(
                    store,
                    repo=repo,
                    suite_id=suite_id,
                    candidate_id=candidate_id,
                    held_out_score=held_out_score,
                )
    except ValueError as error:
        print(f"optimization rejected: {error}")
        return 1

    print(f"optimization: {decision}")
    return 0 if decision == "needs_review" else 1


def _score_from_eval_report(eval_report: dict[str, object], field: str) -> float | None:
    value = eval_report.get(field)
    if value is None:
        metrics = eval_report.get("metrics", {})
        if isinstance(metrics, dict):
            value = metrics.get(field)
    return None if value is None else float(value)


def _write_rollback_plan(repo: Path, run_dir: Path, *, execute: bool = False) -> Path:
    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    validate_json_artifact("apply-plan.json", apply_plan)
    _assert_apply_plan_matches_provenance(repo, run_dir, apply_plan)
    commit_sha = str(apply_plan["applied_commit"])
    if not commit_sha:
        raise ValueError("apply plan has no applied commit")
    target_files = tuple(str(path) for path in apply_plan["target_files"])
    adapter = VcsAdapter(repo)
    if execute:
        _assert_user_worktree_clean(adapter)
    metadata = adapter.rollback_metadata(
        commit_sha=commit_sha,
        branch_name=str(apply_plan["branch_name"]),
        files=target_files,
        reason=f"rollback decision {apply_plan['decision_id']}",
    )
    source_artifacts = {
        "apply_plan": {
            "path": _relative_repo_path(repo, run_dir / "apply-plan.json"),
            "sha256": CandidatePatch.hash_file(run_dir / "apply-plan.json"),
        }
    }
    revert_commit = ""
    if execute:
        try:
            revert_commit = adapter.revert_commit(
                branch_name=str(apply_plan["branch_name"]),
                commit_sha=commit_sha,
            )
        except VcsStateError as error:
            _write_rollback_incident(
                repo,
                run_dir,
                apply_plan=apply_plan,
                commit_sha=commit_sha,
                target_files=target_files,
                failure_kind="git_revert_failed",
                failure_message=str(error),
                source_artifacts=source_artifacts,
            )
            raise
    pre_hashes = apply_plan.get("pre_hashes", {})
    post_rollback_hashes = _target_file_hashes(repo, target_files=target_files) if execute else {}
    restored_pre_hashes = _rollback_restored_pre_hashes(
        repo,
        target_files=target_files,
        pre_hashes=pre_hashes,
        executed=execute,
    )
    post_rollback_eval_result = {
        "executed": execute,
        "restored_pre_hashes": restored_pre_hashes,
        "target_files": list(target_files),
    }
    provenance_bundle = apply_plan.get("provenance_bundle")
    if isinstance(provenance_bundle, str):
        provenance_bundle_path = repo / provenance_bundle
        if provenance_bundle_path.exists():
            source_artifacts["provenance_bundle"] = {
                "path": provenance_bundle,
                "sha256": CandidatePatch.hash_file(provenance_bundle_path),
            }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": str(apply_plan["decision_id"]),
        "candidate_id": int(apply_plan["candidate_id"]),
        "metadata": metadata.to_json_dict(),
        "executed": execute,
        "revert_commit": revert_commit,
        "pre_hashes": pre_hashes if isinstance(pre_hashes, dict) else {},
        "post_rollback_hashes": post_rollback_hashes,
        "restored_pre_hashes": restored_pre_hashes,
        "source_artifacts": source_artifacts,
    }
    path = run_dir / "rollback-plan.json"
    rollback_plan = _relative_repo_path(repo, path)
    _write_secret_scanned_json_artifact(path, "rollback-plan.json", payload)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        rollback_decision_id = _latest_decision_id_for_candidate(
            store,
            candidate_id=int(apply_plan["candidate_id"]),
        )
        store.record_rollback(
            decision_id=str(rollback_decision_id),
            candidate_id=int(apply_plan["candidate_id"]),
            reason=f"rollback decision {apply_plan['decision_id']}",
            revert_commit=revert_commit,
            post_rollback_eval_result=post_rollback_eval_result,
            rollback_plan=rollback_plan,
            executed=execute,
        )
        store.append_audit_event(
            "rollback.planned",
            {
                "candidate_id": int(apply_plan["candidate_id"]),
                "commit_sha": commit_sha,
                "decision_id": str(apply_plan["decision_id"]),
                "rollback_plan": rollback_plan,
                "target_files": list(target_files),
            },
        )
        if execute:
            store.append_audit_event(
                "rollback.applied",
                {
                    "candidate_id": int(apply_plan["candidate_id"]),
                    "commit_sha": commit_sha,
                    "decision_id": str(apply_plan["decision_id"]),
                    "pre_hashes": pre_hashes if isinstance(pre_hashes, dict) else {},
                    "post_rollback_hashes": post_rollback_hashes,
                    "revert_commit": revert_commit,
                    "restored_pre_hashes": restored_pre_hashes,
                    "rollback_plan": rollback_plan,
                    "source_artifacts": source_artifacts,
                    "target_files": list(target_files),
                },
            )
            _transition_originating_daemon_job(
                repo,
                run_dir,
                candidate_id=int(apply_plan["candidate_id"]),
                source_state=JobState.APPLIED,
                target_state=JobState.ROLLED_BACK,
            )
    return path


def _write_rollback_incident(
    repo: Path,
    run_dir: Path,
    *,
    apply_plan: dict[str, object],
    commit_sha: str,
    target_files: tuple[str, ...],
    failure_kind: str,
    failure_message: str,
    source_artifacts: dict[str, object],
) -> Path:
    redacted_failure_message = _bounded_rollback_failure_message(failure_message)
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": str(apply_plan["decision_id"]),
        "candidate_id": int(apply_plan["candidate_id"]),
        "failure_kind": failure_kind,
        "failure_message": redacted_failure_message,
        "commit_sha": commit_sha,
        "target_files": list(target_files),
        "rollback_plan_written": False,
        "rollback_applied": False,
        "source_artifacts": source_artifacts,
    }
    path = run_dir / "rollback-incident.json"
    incident = _relative_repo_path(repo, path)
    _write_secret_scanned_json_artifact(path, "rollback-incident.json", payload)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "rollback.failed",
            {
                "candidate_id": int(apply_plan["candidate_id"]),
                "commit_sha": commit_sha,
                "decision_id": str(apply_plan["decision_id"]),
                "failure_kind": failure_kind,
                "incident": incident,
                "rollback_applied": False,
                "rollback_plan_written": False,
                "target_files": list(target_files),
            },
        )
    return path


def _bounded_rollback_failure_message(message: str, *, limit: int = 2000) -> str:
    redacted = redact_text(message)
    if len(redacted) <= limit:
        return redacted
    return f"{redacted[:limit]}...[truncated]"


def _print_decision_inspection_summary(trace_path: Path) -> None:
    payload = json.loads(trace_path.read_text(encoding="utf-8"))
    decision = _json_object_field(payload, "decision")
    candidate = _json_object_field(payload, "candidate")
    artifacts = _json_object_field(payload, "artifacts")
    print(f"run_id: {payload.get('run_id', '')}")
    print(f"decision: {decision.get('decision', '')}")
    print(f"candidate_id: {candidate.get('candidate_id', '')}")
    print(f"candidate_file: {candidate.get('base_file', '')}")
    print(f"candidate_state: {candidate.get('state', '')}")
    print(f"risk_class: {candidate.get('risk_class', '')}")
    print(f"risk_explanation: {_decision_trace_risk_explanation(trace_path, candidate, artifacts)}")
    print(f"evals: {_decision_trace_eval_summary(payload)}")
    print(f"rollback_ready: {_decision_trace_rollback_ready(decision, artifacts)}")
    print(f"rollback_readiness: {_decision_trace_rollback_readiness(trace_path, decision)}")
    for summary in _decision_trace_review_rejection_summaries(payload):
        print(f"review_rejection: {summary}")
    for summary in _decision_trace_rejected_edit_memory_summaries(payload):
        print(f"rejected_edit_memory: {summary}")
    print(f"highest_impact: {_decision_trace_highest_impact_summary(trace_path, artifacts)}")
    next_artifact = artifacts.get("report") or artifacts.get("candidate_diff") or trace_path.as_posix()
    print(f"review_next: inspect {next_artifact}")


def _print_decision_comparison_summary(primary_trace_path: Path, compare_trace_path: Path) -> None:
    primary = json.loads(primary_trace_path.read_text(encoding="utf-8"))
    compare = json.loads(compare_trace_path.read_text(encoding="utf-8"))
    primary_summary = _decision_comparison_fields(primary)
    compare_summary = _decision_comparison_fields(compare)
    changed_fields = [
        field
        for field in primary_summary
        if primary_summary[field] != compare_summary[field]
    ]
    print("comparison:")
    for field in primary_summary:
        print(f"{field}: {primary_summary[field]} -> {compare_summary[field]}")
    if changed_fields:
        print(f"changed_fields: {', '.join(changed_fields)}")
    else:
        print("changed_fields: none")


def _decision_comparison_fields(payload: dict[str, object]) -> dict[str, str]:
    decision = _json_object_field(payload, "decision")
    candidate = _json_object_field(payload, "candidate")
    artifacts = _json_object_field(payload, "artifacts")
    return {
        "candidate_id": str(candidate.get("candidate_id", "")),
        "candidate_file": str(candidate.get("base_file", "")),
        "candidate_state": str(candidate.get("state", "")),
        "risk_class": str(candidate.get("risk_class", "")),
        "decision": str(decision.get("decision", "")),
        "evals": _decision_trace_eval_summary(payload),
        "rollback_ready": _decision_trace_rollback_ready(decision, artifacts),
    }


def _decision_trace_highest_impact_summary(
    trace_path: Path,
    artifacts: dict[str, object],
) -> str:
    eval_report = _decision_trace_artifact_path(trace_path, artifacts, "eval_report")
    optimization_summary = _decision_trace_artifact_path(
        trace_path,
        artifacts,
        "optimization_summary",
    )
    if eval_report is None or optimization_summary is None:
        return "none"
    try:
        eval_payload = load_json_object_artifact(eval_report, "eval-report.json")
        validate_json_artifact("eval-report.json", eval_payload)
        optimization_payload = load_json_object_artifact(
            optimization_summary,
            "optimization-summary.json",
        )
        validate_json_artifact("optimization-summary.json", optimization_payload)
    except OSError:
        return "none"
    fields = highest_impact_summary_fields(eval_payload, optimization_payload)
    if fields is None:
        return "none"
    return (
        f"target={fields['target']} "
        f"operator={fields['operator']} "
        f"changed_lines={fields['changed_lines']} "
        f"normative_changes={fields['normative_changes']} "
        f"held_out_delta={fields['held_out_delta']} "
        f"instruction_token_delta={fields['instruction_token_delta']} "
        f"governance_passed={fields['governance_passed']}"
    )


def _decision_trace_risk_explanation(
    trace_path: Path,
    candidate: dict[str, object],
    artifacts: dict[str, object],
) -> str:
    policy_gate_path = _decision_trace_artifact_path(trace_path, artifacts, "policy_gate")
    policy_allowed = "unknown"
    policy_reasons = "unknown"
    if policy_gate_path is not None:
        try:
            policy_gate = load_json_object_artifact(policy_gate_path, "policy-gate.json")
            validate_json_artifact("policy-gate.json", policy_gate)
        except OSError:
            policy_gate = None
        if policy_gate is not None:
            policy_allowed = "true" if bool(policy_gate.get("allowed", False)) else "false"
            raw_reasons = policy_gate.get("reasons", [])
            if isinstance(raw_reasons, list) and raw_reasons:
                policy_reasons = ",".join(str(reason) for reason in raw_reasons)
            else:
                policy_reasons = "none"
    allowed = policy_allowed == "true"
    reasons = [] if policy_reasons in {"none", "unknown"} else policy_reasons.split(",")
    return risk_explanation_summary(candidate.get("risk_class", ""), allowed, reasons)


def _decision_trace_rollback_readiness(
    trace_path: Path,
    decision: dict[str, object],
) -> str:
    repo = trace_path.resolve().parents[3]
    run_dir = trace_path.parent
    return rollback_readiness_summary(
        repo,
        run_dir,
        applied_commit=str(decision.get("applied_commit", "")),
    )


def _decision_trace_artifact_path(
    trace_path: Path,
    artifacts: dict[str, object],
    artifact_name: str,
) -> Path | None:
    raw_ref = artifacts.get(artifact_name)
    if not isinstance(raw_ref, str) or not raw_ref:
        return None
    repo = trace_path.resolve().parents[3]
    path = (repo / raw_ref).resolve()
    if not path.is_relative_to(repo):
        return None
    return path


def _json_object_field(payload: dict[str, object], field: str) -> dict[str, object]:
    value = payload.get(field, {})
    return value if isinstance(value, dict) else {}


def _decision_trace_review_rejection_summaries(payload: dict[str, object]) -> list[str]:
    review_actions = payload.get("review_actions", [])
    if not isinstance(review_actions, list):
        return []
    template = _candidate_rejection_template(payload)
    summaries: list[str] = []
    for action in review_actions:
        if not isinstance(action, dict) or action.get("action") != "rejected":
            continue
        summary = (
            f"actor={action.get('actor', '')} "
            f"reason={action.get('reason', '')}"
        )
        if template:
            summary += f" template={template}"
        summaries.append(summary)
    return summaries


def _decision_trace_rejected_edit_memory_summaries(payload: dict[str, object]) -> list[str]:
    candidate = _json_object_field(payload, "candidate")
    candidate_id = str(candidate.get("candidate_id", ""))
    optimizer_memory = payload.get("optimizer_memory", [])
    if not isinstance(optimizer_memory, list):
        return []
    summaries: list[str] = []
    for record in optimizer_memory:
        if not isinstance(record, dict) or record.get("memory_type") != "rejected_edit":
            continue
        memory_payload = record.get("payload", {})
        if not isinstance(memory_payload, dict):
            continue
        if candidate_id and not _memory_payload_matches_candidate(memory_payload, candidate_id):
            continue
        file = str(memory_payload.get("file", ""))
        section = str(memory_payload.get("section", ""))
        target = f"{file}#{section}" if file and section else file or section or str(record.get("key", ""))
        summaries.append(
            f"{target} "
            f"operator={memory_payload.get('operator', '')} "
            f"category={memory_payload.get('category', '')} "
            f"failure_pattern={memory_payload.get('failure_pattern', '')} "
            f"suppression={memory_payload.get('future_proposal_suppression_signal', '')}"
        )
    return summaries


def _candidate_rejection_template(payload: dict[str, object]) -> str:
    candidate = _json_object_field(payload, "candidate")
    candidate_id = str(candidate.get("candidate_id", ""))
    optimizer_memory = payload.get("optimizer_memory", [])
    if not isinstance(optimizer_memory, list):
        return ""
    for record in optimizer_memory:
        if not isinstance(record, dict) or record.get("memory_type") != "rejected_edit":
            continue
        memory_payload = record.get("payload", {})
        if not isinstance(memory_payload, dict):
            continue
        if candidate_id and not _memory_payload_matches_candidate(memory_payload, candidate_id):
            continue
        template = memory_payload.get("review_template")
        if isinstance(template, str):
            return template
    return ""


def _memory_payload_matches_candidate(memory_payload: dict[str, object], candidate_id: str) -> bool:
    source_refs = memory_payload.get("source_refs", [])
    return isinstance(source_refs, list) and f"candidate:{candidate_id}" in source_refs


def _decision_trace_eval_summary(payload: dict[str, object]) -> str:
    evals = payload.get("evals", [])
    if not isinstance(evals, list) or not evals:
        return "none"
    summaries: list[str] = []
    for eval_record in evals:
        if not isinstance(eval_record, dict):
            continue
        suite_id = str(eval_record.get("suite_id", "unknown"))
        status = "passed" if eval_record.get("passed") is True else "failed"
        summaries.append(f"{suite_id}={status}")
    return ", ".join(summaries) if summaries else "none"


def _decision_trace_rollback_ready(
    decision: dict[str, object],
    artifacts: dict[str, object],
) -> str:
    applied_commit = str(decision.get("applied_commit", ""))
    return "yes" if applied_commit and artifacts.get("rollback_plan") else "no"


def _latest_decision_id_for_candidate(store: Store, *, candidate_id: int) -> int:
    row = store.connection.execute(
        """
        SELECT id
        FROM decisions
        WHERE candidate_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"candidate has no decision: {candidate_id}")
    return int(row[0])


def _transition_originating_daemon_job(
    repo: Path,
    run_dir: Path,
    *,
    candidate_id: int,
    source_state: JobState,
    target_state: JobState,
) -> None:
    queue_path = repo / ".sidecar" / "daemon.sqlite"
    if not queue_path.exists():
        return

    matched_job_ids: list[str] = []
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        rows = store.connection.execute(
            """
            SELECT job_id, payload_json
            FROM daemon_jobs
            WHERE repo_path = ? AND state = ?
            ORDER BY rowid DESC
            """,
            (str(repo), source_state.value),
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(str(row[1]))
            except json.JSONDecodeError:
                continue
            if _daemon_payload_matches_run_candidate(payload, run_dir.name, candidate_id):
                matched_job_ids.append(str(row[0]))

    if not matched_job_ids:
        return

    with DaemonQueue.open_sidecar(repo) as queue:
        for job_id in matched_job_ids:
            try:
                numeric_job_id = int(job_id)
            except ValueError:
                continue
            job = queue.get_job(numeric_job_id)
            if job is None:
                continue
            if job.state is target_state:
                _record_daemon_job_cli_state(repo, job.id, target_state, payload=job.payload)
                return
            if job.state is not source_state:
                continue
            updated = queue.transition(job.id, target_state)
            _record_daemon_job_cli_state(repo, updated.id, updated.state, payload=updated.payload)
            return


def _daemon_payload_matches_run_candidate(
    payload: object,
    run_id: str,
    candidate_id: int,
) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("run_id") == run_id:
        return True
    candidate_value = payload.get("candidate_id")
    if candidate_value is not None and str(candidate_value) == str(candidate_id):
        return True
    for key in ("execution_payload", "resume", "payload"):
        nested = payload.get(key)
        if isinstance(nested, dict) and _daemon_payload_matches_run_candidate(
            nested,
            run_id,
            candidate_id,
        ):
            return True
    return False


def _record_daemon_job_cli_state(
    repo: Path,
    job_id: int,
    state: JobState,
    *,
    payload: dict[str, Any],
) -> None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.update_daemon_job_state(
            job_id=str(job_id),
            repo_path=repo,
            state=state.value,
            payload=payload,
        )


def _assert_apply_plan_matches_provenance(
    repo: Path,
    run_dir: Path,
    apply_plan: dict[str, object],
) -> None:
    provenance_bundle = apply_plan.get("provenance_bundle")
    if not isinstance(provenance_bundle, str) or not provenance_bundle:
        raise ValueError("apply plan is missing provenance bundle")
    provenance_bundle_path = (repo / provenance_bundle).resolve()
    if not provenance_bundle_path.is_relative_to(repo.resolve()):
        raise ValueError("apply plan provenance bundle must resolve inside repo")
    if not provenance_bundle_path.is_file():
        raise ValueError("apply plan provenance bundle is missing")
    payload = json.loads(provenance_bundle_path.read_text(encoding="utf-8"))
    validate_json_artifact("provenance-bundle.json", payload)
    source_artifacts = payload.get("source_artifacts", {})
    if not isinstance(source_artifacts, dict):
        raise ValueError("provenance bundle source_artifacts must be an object")
    apply_plan_ref = source_artifacts.get("apply_plan")
    if not isinstance(apply_plan_ref, dict):
        raise ValueError("provenance bundle missing apply plan reference")
    expected_path = _relative_repo_path(repo, run_dir / "apply-plan.json")
    if apply_plan_ref.get("path") != expected_path:
        raise ValueError("apply plan does not match provenance bundle")
    if apply_plan_ref.get("sha256") != CandidatePatch.hash_file(run_dir / "apply-plan.json"):
        raise ValueError("apply plan does not match provenance bundle")
    if payload.get("run_id") != run_dir.name or payload.get("candidate_id") != apply_plan.get("candidate_id"):
        raise ValueError("apply plan does not match provenance bundle")


def _rollback_restored_pre_hashes(
    repo: Path,
    *,
    target_files: tuple[str, ...],
    pre_hashes: object,
    executed: bool,
) -> bool:
    if not executed or not isinstance(pre_hashes, dict):
        return False
    for target_file in target_files:
        expected_hash = pre_hashes.get(target_file)
        if not isinstance(expected_hash, str):
            return False
        if CandidatePatch.hash_file(repo / target_file) != expected_hash:
            return False
    return True


def _target_file_hashes(repo: Path, *, target_files: tuple[str, ...]) -> dict[str, str]:
    return {
        target_file: CandidatePatch.hash_file(repo / target_file)
        for target_file in target_files
        if (repo / target_file).exists()
    }


def _decision_json(
    *,
    candidate_id: int,
    decision_value: str,
    policy_allowed: bool,
    policy_reasons: list[str],
) -> str:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "decision": decision_value,
        "policy_allowed": policy_allowed,
        "policy_reasons": policy_reasons,
    }
    validate_json_artifact("decision.json", payload)
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _merge_json(path: Path, updates: dict[str, object]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parse_profile_app_boot(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("app boot metadata must be a JSON object") from error
    if not isinstance(payload, dict):
        raise ValueError("app boot metadata must be a JSON object")
    return payload


def _validate_observability_refs(refs: list[str]) -> list[str]:
    normalized: list[str] = []
    for ref in refs:
        value = ref.strip()
        if not value:
            raise ValueError("observability refs must be local-only")
        parsed = urlparse(value)
        if parsed.scheme in {"http", "https"}:
            host = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port is not None else ""
            _validate_local_observability_ref(f"{host}{port}")
        elif parsed.scheme and parsed.scheme != "unix":
            raise ValueError("observability refs must be local-only")
        else:
            _validate_local_observability_ref(value)
        normalized.append(value)
    return normalized


def _validate_local_observability_ref(value: str) -> None:
    try:
        validate_local_bind_address(value)
    except ValueError as error:
        raise ValueError("observability refs must be local-only") from error


if __name__ == "__main__":
    console_main()
