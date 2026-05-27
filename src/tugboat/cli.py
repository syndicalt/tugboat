from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    AutoApplyPolicy,
    AutoApplyReadiness,
    VcsProof,
    evaluate_auto_apply,
)
from tugboat.config import load_policy
from tugboat.corpus.indexer import index_repo
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
from tugboat.daemon.queue import validate_local_bind_address
from tugboat.db import Store
from tugboat.eval.pipeline import run_eval_pipeline
from tugboat.harness.checks import (
    check_harness_legibility,
    generate_cleanup_candidates,
    generate_harness_report,
)
from tugboat.llmff.runner import inspect_manifest, run_manifest
from tugboat.manifests import manifests_are_allowed_by_policy, materialize_manifests
from tugboat.mcp import run_stdio_server
from tugboat.ops.backup import build_sidecar_backup_bundle, build_sidecar_restore_bundle
from tugboat.ops.migrations import dry_run_migration_plan, execute_migration_plan
from tugboat.ops.observability import summarize_sidecar_observability
from tugboat.ops.retention import apply_retention_policy
from tugboat.optimization import (
    REJECTED_EDIT_SUPPRESSION_SIGNAL,
    EpisodeOutcome,
    build_success_failure_minibatch,
)
from tugboat.paths import latest_run_dir, mark_private_file, runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef, evaluate_candidate
from tugboat.report.decision_trace import write_decision_trace
from tugboat.propose.pipeline import run_propose_pipeline
from tugboat.report.service import write_report
from tugboat.security.secrets import SecretScanError, scan_path, scan_text
from tugboat.vcs import VcsAdapter, VcsStateError


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tugboat")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor")

    status = subcommands.add_parser("status")
    status.add_argument("--repo", required=True)

    retention = subcommands.add_parser("retention")
    retention.add_argument("--repo", required=True)
    retention.add_argument("--apply", action="store_true")

    ci = subcommands.add_parser("ci")
    ci.add_argument("--repo", required=True)
    ci.add_argument("--max-instruction-lines", type=int, default=100)
    ci.add_argument("--candidate")
    ci.add_argument("--suite", default="all")

    index = subcommands.add_parser("index")
    index.add_argument("--repo", required=True)
    index.add_argument("--check", action="store_true")

    audit = subcommands.add_parser("audit")
    audit.add_argument("--repo", required=True)
    audit.add_argument("--trace", required=True)
    audit.add_argument(
        "--trace-format",
        choices=("generic-jsonl", "codex", "claude", "ci", "mcp"),
        default="generic-jsonl",
    )
    audit.add_argument("--mock-llmff-inspect", action="store_true")

    propose = subcommands.add_parser("propose")
    propose.add_argument("--repo", required=True)
    propose.add_argument("--audit", required=True)

    evaluate = subcommands.add_parser("eval")
    evaluate.add_argument("--repo", required=True)
    evaluate.add_argument("--candidate", required=True)
    evaluate.add_argument("--suite", required=True)

    optimize = subcommands.add_parser("optimize")
    optimize.add_argument("--repo", required=True)
    optimize.add_argument("--trace", required=True)
    optimize.add_argument("--suite", required=True)
    optimize.add_argument(
        "--trace-format",
        choices=("generic-jsonl", "codex", "claude", "ci", "mcp"),
        default="generic-jsonl",
    )

    apply = subcommands.add_parser("apply")
    apply.add_argument("--repo", required=True)
    apply.add_argument("--candidate", required=True)
    apply.add_argument("--mode", choices=("proposal", "branch", "commit", "pr"), default="proposal")
    apply.add_argument("--review-actor", default="tugboat")
    apply.add_argument("--human-review", action="store_true")
    apply.add_argument("--auto-apply", action="store_true")
    apply.add_argument("--confirm-auto-apply", action="store_true")
    apply.add_argument("--auto-apply-policy-version", type=int)
    apply.add_argument("--burn-in-days", type=int, default=0)
    apply.add_argument("--rejection-rate", type=float, default=1.0)
    apply.add_argument("--rollback-rate", type=float, default=1.0)

    auto_apply = subcommands.add_parser("auto-apply")
    auto_apply.add_argument("--repo", required=True)
    auto_apply.add_argument("--candidate", required=True)
    auto_apply.add_argument("--confirm-auto-apply", action="store_true")
    auto_apply.add_argument("--auto-apply-policy-version", type=int)
    auto_apply.add_argument("--actor", required=True)
    auto_apply.add_argument("--burn-in-days", type=int, default=0)
    auto_apply.add_argument("--rejection-rate", type=float, default=1.0)
    auto_apply.add_argument("--rollback-rate", type=float, default=1.0)

    rollback = subcommands.add_parser("rollback")
    rollback.add_argument("--repo", required=True)
    rollback.add_argument("--decision", required=True)
    rollback.add_argument("--execute", action="store_true")

    mcp = subcommands.add_parser("mcp")
    mcp_subcommands = mcp.add_subparsers(dest="mcp_command", required=True)
    mcp_subcommands.add_parser("stdio")

    daemon = subcommands.add_parser("daemon")
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

    report = subcommands.add_parser("report")
    report.add_argument("--repo", required=True)
    report.add_argument("--run", required=True)

    inspect_decision = subcommands.add_parser("inspect-decision")
    inspect_decision.add_argument("--repo", required=True)
    inspect_decision.add_argument("--decision", required=True)

    harness = subcommands.add_parser("harness")
    harness_subcommands = harness.add_subparsers(dest="harness_command", required=True)
    harness_check = harness_subcommands.add_parser("check")
    harness_check.add_argument("--repo", required=True)
    harness_check.add_argument("--max-instruction-lines", type=int, default=100)
    harness_report = harness_subcommands.add_parser("report")
    harness_report.add_argument("--repo", required=True)
    harness_cleanup = harness_subcommands.add_parser("cleanup")
    harness_cleanup.add_argument("--repo", required=True)
    ops = subcommands.add_parser("ops")
    ops_subcommands = ops.add_subparsers(dest="ops_command", required=True)
    ops_backup = ops_subcommands.add_parser("backup")
    ops_backup.add_argument("--repo", required=True)
    ops_backup.add_argument("--archive", required=True)
    ops_migrate = ops_subcommands.add_parser("migrate")
    ops_migrate.add_argument("--repo", required=True)
    ops_migrate.add_argument("--apply", action="store_true")
    ops_observability = ops_subcommands.add_parser("observability")
    ops_observability.add_argument("--repo", required=True)
    ops_observability.add_argument("--output")
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
    ops_restore = ops_subcommands.add_parser("restore")
    ops_restore.add_argument("--repo", required=True)
    ops_restore.add_argument("--archive", required=True)
    ops_restore.add_argument("--staging", required=True)
    ops_restore.add_argument("--pre-restore", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        print("tugboat: ok")
        print("mode: proposal_only")
        print("auto_apply: disabled")
        return 0

    if args.command == "status":
        repo = Path(args.repo)
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
        retention = apply_retention_policy(repo, policy, dry_run=True)
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
        policy = load_policy(repo)
        if args.apply:
            if _write_blocked_by_read_only(repo, "retention"):
                return 1
            preflight = apply_retention_policy(repo, policy, dry_run=True)
            report_path = _write_retention_report(
                repo,
                mode="apply",
                status="planned",
                candidates=preflight.candidates,
                deleted=(),
                redaction_candidates=preflight.redaction_candidates,
            )
            result = apply_retention_policy(repo, policy, dry_run=False)
            report_path = _write_retention_report(
                repo,
                mode="apply",
                status="complete",
                candidates=result.candidates,
                deleted=result.deleted,
                redaction_candidates=result.redaction_candidates,
            )
        else:
            result = apply_retention_policy(repo, policy, dry_run=True)
            report_path = _write_retention_report(
                repo,
                mode="dry-run",
                status="complete",
                candidates=result.candidates,
                deleted=result.deleted,
                redaction_candidates=result.redaction_candidates,
            )
        print(f"retention_mode: {'apply' if args.apply else 'dry-run'}")
        print(f"candidates: {len(result.candidates)}")
        print(f"deleted: {len(result.deleted)}")
        print(f"redaction_candidates: {len(result.redaction_candidates)}")
        print(f"retention_report: {report_path}")
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
        try:
            report_path, payload = _write_ci_report(
                repo,
                max_instruction_lines=args.max_instruction_lines,
                candidate=args.candidate,
                suite=args.suite,
            )
        except SecretScanError as error:
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
        semantic_lint = payload["checks"]["semantic_policy_lint"]
        if not semantic_lint["passed"]:
            print("semantic policy lint failed")
            for finding in semantic_lint["findings"]:
                print(finding)
        if eval_check is not None and not eval_check["passed"]:
            print(f"eval suite {eval_check['suite_id']} failed")
        print(f"report: {report_path}")
        return 1

    if args.command == "index":
        repo = Path(args.repo)
        result = index_repo(repo, load_policy(repo))
        if not args.check:
            with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
                store.index_documents(repo, result)
        print(f"indexed documents: {result.indexed_count}")
        return 0

    if args.command == "audit":
        repo = Path(args.repo)
        result = run_audit_pipeline(
            repo,
            Path(args.trace),
            trace_format=args.trace_format,
            mock_llmff_inspect=args.mock_llmff_inspect,
        )
        print(result.message)
        return result.exit_code

    if args.command == "propose":
        repo = Path(args.repo)
        result = run_propose_pipeline(repo, args.audit)
        print(result.message)
        return result.exit_code

    if args.command == "eval":
        repo = Path(args.repo)
        result = run_eval_pipeline(repo, args.candidate, args.suite)
        print(result.message)
        return _finalize_governed_candidate_evaluation(
            repo,
            result.run_dir,
            suite_id=args.suite,
            eval_exit_code=result.exit_code,
        )

    if args.command == "optimize":
        repo = Path(args.repo)
        audit_exit = main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(Path(args.trace)),
                "--trace-format",
                args.trace_format,
            ]
        )
        if audit_exit != 0:
            return audit_exit
        run_dir = latest_run_dir(repo)
        _record_optimize_minibatch_guidance(repo, run_dir, suite_id=args.suite)
        propose_exit = main(["propose", "--repo", str(repo), "--audit", "latest"])
        run_dir = latest_run_dir(repo)
        if propose_exit != 0:
            return _write_optimization_summary(repo, run_dir, suite_id=args.suite)
        eval_result = run_eval_pipeline(repo, "latest", args.suite)
        print(eval_result.message)
        return _finalize_governed_candidate_evaluation(
            repo,
            run_dir,
            suite_id=args.suite,
            eval_exit_code=eval_result.exit_code,
        )

    if args.command == "apply":
        repo = Path(args.repo)
        if _write_blocked_by_read_only(repo, "apply"):
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
                burn_in_days=args.burn_in_days,
                rejection_rate=args.rejection_rate,
                rollback_rate=args.rollback_rate,
            )
        except (FileNotFoundError, KeyError, VcsStateError, ValueError) as error:
            print(f"apply blocked: {error}")
            return 1
        print(f"apply plan: {apply_path}")
        return 0

    if args.command == "auto-apply":
        repo = Path(args.repo)
        if _write_blocked_by_read_only(repo, "auto-apply"):
            return 1
        run_dir = latest_run_dir(repo) if args.candidate == "latest" else runs_dir(repo) / args.candidate
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
                burn_in_days=args.burn_in_days,
                rejection_rate=args.rejection_rate,
                rollback_rate=args.rollback_rate,
            )
        except (FileNotFoundError, KeyError, VcsStateError, ValueError) as error:
            print(f"auto-apply blocked: {error}")
            return 1
        print(f"auto-apply plan: {apply_path}")
        return 0

    if args.command == "rollback":
        repo = Path(args.repo)
        if args.execute and _write_blocked_by_read_only(repo, "rollback"):
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
        return run_stdio_server(sys.stdin, sys.stdout)

    if args.command == "daemon" and args.daemon_command == "status":
        repo = Path(args.repo)
        status = daemon_status(repo, kill_switch=default_kill_switch(repo))
        print(f"queue_path: {status['queue_path']}")
        print(f"kill_switch_enabled: {str(status['kill_switch_enabled']).lower()}")
        for state, count in sorted(status["jobs_by_state"].items()):
            print(f"{state}: {count}")
        print(f"oldest_queued_job_id: {status['oldest_queued_job_id']}")
        return 0

    if args.command == "daemon" and args.daemon_command == "run-once":
        repo = Path(args.repo)
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
        trace_dirs = (
            tuple(Path(trace_dir) for trace_dir in args.trace_dir)
            if args.trace_dir
            else tuple(default_trace_dirs(repo))
        )
        config = DaemonLoopConfig(
            worker_id=args.worker_id,
            max_jobs_per_cycle=args.max_jobs,
            concurrency_limit=args.concurrency,
            lease_duration=timedelta(seconds=args.lease_seconds),
            trace_dirs=trace_dirs,
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
        run_dir = latest_run_dir(repo) if args.run == "latest" else runs_dir(repo) / args.run
        candidate = _candidate_from_artifacts(run_dir)
        decision = _decision_from_artifact(run_dir)
        report_path = write_report(
            repo,
            run_dir.name,
            candidate=candidate,
            decision=decision,
            eval_report_path=run_dir / "eval-report.json",
        )
        print(f"report: {report_path}")
        return 0

    if args.command == "inspect-decision":
        repo = Path(args.repo)
        try:
            trace_path = write_decision_trace(repo, args.decision)
        except (FileNotFoundError, KeyError, ValueError, SecretScanError) as error:
            print(f"inspect decision blocked: {error}")
            return 1
        print(f"decision_trace: {trace_path}")
        return 0

    if args.command == "harness" and args.harness_command == "check":
        result = check_harness_legibility(Path(args.repo), args.max_instruction_lines)
        if result.passed:
            print("harness: ok")
            return 0
        for finding in result.findings:
            print(finding)
        return 1

    if args.command == "harness" and args.harness_command == "report":
        repo = Path(args.repo)
        report = generate_harness_report(repo)
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
        return 0

    if args.command == "harness" and args.harness_command == "cleanup":
        repo = Path(args.repo)
        if _write_blocked_by_read_only(repo, "cleanup"):
            return 1
        report = generate_harness_report(repo)
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
        print(f"observability summary: {output_path}")
        return 0

    if args.command == "ops" and args.ops_command == "release-manifest":
        repo = Path(args.repo)
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
        plan = execute_migration_plan(repo) if args.apply else dry_run_migration_plan(repo)
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
        try:
            bundle = build_sidecar_backup_bundle(repo=repo, archive_path=Path(args.archive))
        except ValueError as error:
            print(f"backup plan blocked: {error}")
            return 1
        path = sidecar_dir(repo) / "ops" / "backup-plan.json"
        _write_ops_command_bundle(path, bundle.to_dict())
        print(f"backup plan: {path}")
        return 0

    if args.command == "ops" and args.ops_command == "restore":
        repo = Path(args.repo)
        try:
            bundle = build_sidecar_restore_bundle(
                repo=repo,
                archive_path=Path(args.archive),
                staging_path=Path(args.staging),
                pre_restore_path=Path(args.pre_restore),
            )
        except ValueError as error:
            print(f"restore plan blocked: {error}")
            return 1
        path = sidecar_dir(repo) / "ops" / "restore-plan.json"
        _write_ops_command_bundle(path, bundle.to_dict())
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
        retained_evidence.append(_file_manifest_entry(resolved_evidence))
    if not any("pytest-coverage" in Path(entry["path"]).name for entry in retained_evidence):
        raise ValueError("pytest coverage evidence is required")
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
            "python -m pytest --cov=src --cov-report=term-missing -q",
        ],
        "retained_evidence": retained_evidence,
    }
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
    harness = check_harness_legibility(repo, max_instruction_lines)
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
) -> int:
    if not (run_dir / "candidate.raw.json").exists():
        return eval_exit_code
    if not (run_dir / "eval-report.json").exists():
        return eval_exit_code
    if eval_exit_code == 0:
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
            try:
                _run_acceptance_summary(repo, run_dir, load_policy(repo))
            except (RuntimeError, ValueError) as error:
                print(str(error))
                return 1
    return _write_optimization_summary(repo, run_dir, suite_id=suite_id)


def _run_acceptance_summary(repo: Path, run_dir: Path, policy) -> dict[str, object]:
    manifests = materialize_manifests(repo)
    if not manifests_are_allowed_by_policy(manifests, policy):
        raise RuntimeError("manifest hash is not allowed by policy")
    manifest = next(record.path for record in manifests if record.name == "acceptance-summary.yaml")
    inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy)
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
            "eval_reports": run_dir / "eval-report.json",
            "proposal_rationale": run_dir / "proposal-rationale.raw.json",
            "risk_class": run_dir / "candidate.json",
        },
        output_paths={"acceptance_summary": run_dir / "acceptance-summary.raw.json"},
    )
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
        raise RuntimeError(f"llmff acceptance-summary failed with exit code {run.exit_code}")
    payload = load_json_object_artifact(
        run.output_paths["acceptance_summary"],
        "acceptance-summary.raw.json",
    )
    validate_json_artifact("acceptance-summary.raw.json", payload)
    return payload


def _candidate_from_artifacts(run_dir: Path) -> CandidatePatch:
    metadata = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    diff = (run_dir / "candidate.diff").read_text(encoding="utf-8")
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

    payload = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
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
    burn_in_days: int = 0,
    rejection_rate: float = 1.0,
    rollback_rate: float = 1.0,
) -> Path:
    candidate = _candidate_from_artifacts(run_dir)
    candidate_meta = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate_id = int(candidate_meta["candidate_id"])
    target_files = (candidate.base_file,)
    policy = load_policy(repo)
    decision = evaluate_candidate(repo, policy, candidate)
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
    explicit_human_review = bool(decision.review_required_reasons)
    if explicit_human_review and (not human_review or review_actor == "tugboat"):
        raise ValueError("Class C candidates require explicit human review")
    if auto_apply and mode != "commit":
        raise ValueError("auto-apply requires commit mode")

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
    pre_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
    post_hashes: dict[str, str] = {}
    applied_commit = ""
    rollback_command: list[list[str]] = []
    auto_apply_approval: dict[str, object] | None = None
    pr_metadata: dict[str, object] = {}
    branch_created = False

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
            burn_in_days=burn_in_days,
            rejection_rate=rejection_rate,
            rollback_rate=rollback_rate,
        )

    try:
        if mode == "branch":
            adapter.create_branch(branch_name)
            branch_created = True
            adapter.apply_diff(run_dir / "candidate.diff")
            post_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
        elif mode == "commit":
            adapter.create_branch(branch_name)
            branch_created = True
            adapter.apply_diff(run_dir / "candidate.diff")
            post_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
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
                    burn_in_days=burn_in_days,
                    rejection_rate=rejection_rate,
                    rollback_rate=rollback_rate,
                )
        elif mode == "pr":
            adapter.create_branch(branch_name)
            branch_created = True
            adapter.apply_diff(run_dir / "candidate.diff")
            post_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
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
            pr_metadata = adapter.pull_request_metadata(
                candidate_id=candidate_id,
                base_file=candidate.base_file,
                branch_name=branch_name,
                base_branch=base_branch,
                rationale=candidate.rationale,
            ).to_json_dict()
    except VcsStateError:
        if branch_created and not applied_commit:
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
            decision="applied" if applied_commit else "planned",
            reason="policy gate and eval report passed",
            applied_commit=applied_commit,
            rollback_ref=json.dumps(rollback_command, sort_keys=True),
        )
        if applied_commit:
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
                ),
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
    burn_in_days: int,
    rejection_rate: float,
    rollback_rate: float,
) -> None:
    rollback_command = _auto_apply_rollback_command(repo, run_dir)
    metrics = _auto_apply_ledger_metrics(repo)
    decision = evaluate_auto_apply(
        candidate=AutoApplyCandidate(
            candidate_id=str(candidate_id),
            repository=str(repo.resolve()),
            change_class=candidate.risk_class,
            categories=_auto_apply_candidate_categories(candidate),
            held_out_eval_passed=True,
            governance_regression_passed=True,
            rejection_rate=float(metrics["rejection_rate"]),
            rollback_rate=float(metrics["rollback_rate"]),
            vcs_proof=VcsProof(
                mode=mode,
                commit_sha="pending",
                branch_name=branch_name,
                rollback_commands=(rollback_command,),
            ),
        ),
        readiness=_auto_apply_readiness(
            repo,
            review_actor=review_actor,
            confirmed=confirmed,
            policy_version=policy_version,
            burn_in_days=int(metrics["burn_in_days"]),
        ),
    )
    _record_auto_apply_decision(repo, candidate_id, run_dir.name, decision.reasons, review_actor)
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
    burn_in_days: int,
    rejection_rate: float,
    rollback_rate: float,
) -> dict[str, object]:
    rollback_command = _auto_apply_rollback_command(repo, run_dir)
    metrics = _auto_apply_ledger_metrics(repo)
    decision = evaluate_auto_apply(
        candidate=AutoApplyCandidate(
            candidate_id=str(candidate_id),
            repository=str(repo.resolve()),
            change_class=candidate.risk_class,
            categories=_auto_apply_candidate_categories(candidate),
            held_out_eval_passed=True,
            governance_regression_passed=True,
            rejection_rate=float(metrics["rejection_rate"]),
            rollback_rate=float(metrics["rollback_rate"]),
            vcs_proof=VcsProof(
                mode=mode,
                commit_sha=applied_commit,
                branch_name=branch_name,
                rollback_commands=(rollback_command,),
            ),
        ),
        readiness=_auto_apply_readiness(
            repo,
            review_actor=review_actor,
            confirmed=confirmed,
            policy_version=policy_version,
            burn_in_days=int(metrics["burn_in_days"]),
        ),
    )
    _record_auto_apply_decision(repo, candidate_id, run_dir.name, decision.reasons, review_actor)
    if not decision.eligible or decision.approval_bundle is None:
        raise ValueError(f"auto-apply rejected candidate: {', '.join(decision.reasons)}")
    bundle = decision.approval_bundle.to_json_dict()
    bundle["readiness_metrics"] = metrics
    return bundle


def _auto_apply_ledger_metrics(repo: Path) -> dict[str, object]:
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
            """
        ).fetchone()
        applied_row = store.connection.execute(
            """
            SELECT COUNT(*)
            FROM decisions
            WHERE decision = 'applied'
              AND policy IN ('apply_controller', 'auto_apply_controller')
            """
        ).fetchone()
        rollback_row = store.connection.execute(
            """
            SELECT COUNT(*), MIN(sequence), MAX(sequence)
            FROM audit_events
            WHERE event_type = 'rollback.applied'
            """
        ).fetchone()

    reviewed_count = int(reviewed_row[0] or 0)
    rejected_count = int(reviewed_row[1] or 0)
    applied_count = int(applied_row[0] or 0)
    rollback_count = int(rollback_row[0] or 0)
    burn_in_days = _burn_in_days(str(reviewed_row[2])) if reviewed_row[2] else 0
    source_sequences = [
        value
        for value in (
            reviewed_row[3],
            reviewed_row[4],
            rollback_row[1],
            rollback_row[2],
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
    return AutoApplyReadiness(
        burn_in_days=burn_in_days,
        policy=AutoApplyPolicy(
            enabled=policy.auto_apply_enabled,
            version=policy.version,
            allowed_repositories=policy.auto_apply_allowed_repositories,
            minimum_burn_in_days=policy.auto_apply_minimum_burn_in_days,
            maximum_rejection_rate=policy.auto_apply_maximum_rejection_rate,
            maximum_rollback_rate=policy.auto_apply_maximum_rollback_rate,
        ),
        confirmation=AutoApplyConfirmation(
            confirmed=confirmed,
            actor=review_actor if confirmed else "",
            policy_version=policy_version if confirmed else None,
        ),
    )


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


def _record_auto_apply_decision(
    repo: Path,
    candidate_id: int,
    run_id: str,
    reasons: tuple[str, ...],
    actor: str,
) -> None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": candidate_id,
                "run_id": run_id,
                "actor": actor,
                "eligible": not reasons,
                "reasons": list(reasons),
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
) -> dict[str, object]:
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    policy_gate = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    return {
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


def _relative_repo_path(repo: Path, path: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


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


def _validation_baseline_key(suite_id: str) -> str:
    return f"validation_baseline:{suite_id}"


def _repo_memory_path(repo: Path) -> str:
    return str(repo.resolve())


def _load_validation_baseline_score(repo: Path, *, suite_id: str) -> float | None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        return _read_validation_baseline_score(store, repo=repo, suite_id=suite_id)


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


def _record_optimize_minibatch_guidance(repo: Path, run_dir: Path, *, suite_id: str) -> None:
    canonical_episode_path = run_dir / "canonical-episode.json"
    if not canonical_episode_path.exists():
        return
    episode = json.loads(canonical_episode_path.read_text(encoding="utf-8"))
    if not isinstance(episode, dict):
        raise ValueError("canonical-episode.json must be a JSON object")
    outcome = _episode_outcome_from_labels(episode.get("outcome_labels", []))
    if outcome is None:
        return
    pattern = _episode_pattern_from_canonical_episode(episode, outcome=outcome)
    minibatch = build_success_failure_minibatch(
        (
            EpisodeOutcome(
                episode_id=_episode_id_for_run(repo, run_dir),
                outcome=outcome,
                pattern=pattern,
            ),
        )
    )
    batch_payload = {
        "schema_version": SCHEMA_VERSION,
        "failure_episodes": list(minibatch.failure_episodes),
        "failure_patterns": list(minibatch.failure_patterns),
        "held_out_suite": suite_id,
        "success_episodes": list(minibatch.success_episodes),
        "success_patterns": list(minibatch.success_patterns),
    }
    (run_dir / "optimization-batch.json").write_text(
        json.dumps(batch_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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


def _bounded_edit_fingerprint(operator: str, target_file: str, section: str) -> str:
    return hashlib.sha256(f"{operator}\n{target_file}\n{section}".encode("utf-8")).hexdigest()


def _write_optimization_summary(repo: Path, run_dir: Path, *, suite_id: str) -> int:
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

    optimization_summary_path = run_dir / "optimization-summary.json"
    optimization_summary_path.write_text(summary_text, encoding="utf-8")
    mark_private_file(optimization_summary_path)
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
    revert_commit = ""
    if execute:
        revert_commit = adapter.revert_commit(
            branch_name=str(apply_plan["branch_name"]),
            commit_sha=commit_sha,
        )
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
    source_artifacts = {
        "apply_plan": {
            "path": _relative_repo_path(repo, run_dir / "apply-plan.json"),
            "sha256": CandidatePatch.hash_file(run_dir / "apply-plan.json"),
        }
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
        store.record_rollback(
            decision_id=str(apply_plan["decision_id"]),
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
    return path


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
