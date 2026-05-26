from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
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
from tugboat.daemon.runner import DaemonLoopConfig, default_trace_dirs, run_daemon_cycle
from tugboat.daemon.service import (
    DaemonRunConfig,
    daemon_status,
    default_kill_switch,
    run_daemon_once,
    serve_daemon_socket,
)
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
from tugboat.ops.observability import summarize_sidecar_observability
from tugboat.ops.retention import apply_retention_policy
from tugboat.paths import latest_run_dir, runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef, evaluate_candidate
from tugboat.propose.pipeline import run_propose_pipeline
from tugboat.report.service import write_report
from tugboat.vcs import VcsAdapter, VcsStateError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tugboat")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor")

    status = subcommands.add_parser("status")
    status.add_argument("--repo", required=True)

    retention = subcommands.add_parser("retention")
    retention.add_argument("--repo", required=True)
    retention.add_argument("--apply", action="store_true")

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
    daemon_serve = daemon_subcommands.add_parser("serve")
    daemon_serve.add_argument("--repo", required=True)
    daemon_serve.add_argument("--worker-id", default="tugboat-daemon")
    daemon_serve.add_argument("--lease-seconds", type=int, default=300)
    daemon_serve.add_argument("--socket")
    daemon_serve.add_argument("--max-requests", type=int)

    report = subcommands.add_parser("report")
    report.add_argument("--repo", required=True)
    report.add_argument("--run", required=True)

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
    ops_observability = ops_subcommands.add_parser("observability")
    ops_observability.add_argument("--repo", required=True)
    ops_observability.add_argument("--output")
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
                "SELECT stage, status FROM runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            pending_candidates = int(
                store.connection.execute(
                    "SELECT COUNT(*) FROM candidates WHERE state = 'needs_review'"
                ).fetchone()[0]
            )
            indexed_documents = store.count("documents")
        print(f"mode: {policy.mode}")
        print(f"auto_apply: {'enabled' if policy.auto_apply_enabled else 'disabled'}")
        print(f"indexed_documents: {indexed_documents}")
        print(f"latest_run: {latest[0]} {latest[1]}" if latest else "latest_run: none")
        print(f"pending_candidates: {pending_candidates}")
        return 0

    if args.command == "retention":
        repo = Path(args.repo)
        result = apply_retention_policy(repo, load_policy(repo), dry_run=not args.apply)
        print(f"retention_mode: {'apply' if args.apply else 'dry-run'}")
        print(f"candidates: {len(result.candidates)}")
        print(f"deleted: {len(result.deleted)}")
        for candidate in result.candidates:
            print(f"candidate: {candidate}")
        for deleted in result.deleted:
            print(f"deleted: {deleted}")
        return 0

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
        return result.exit_code

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
        propose_exit = main(["propose", "--repo", str(repo), "--audit", "latest"])
        run_dir = latest_run_dir(repo)
        if propose_exit != 0:
            return _write_optimization_summary(repo, run_dir, suite_id=args.suite)
        eval_exit = main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", args.suite])
        if eval_exit == 0:
            try:
                eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
                policy_gate = _read_optional_json_object(run_dir / "policy-gate.json")
                _assert_eval_acceptance(eval_report, policy_gate)
            except ValueError:
                pass
            else:
                try:
                    _run_acceptance_summary(repo, run_dir, load_policy(repo))
                except (RuntimeError, ValueError) as error:
                    print(str(error))
                    return 1
        return _write_optimization_summary(repo, run_dir, suite_id=args.suite)

    if args.command == "apply":
        repo = Path(args.repo)
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

    if args.command == "daemon" and args.daemon_command == "cycle":
        repo = Path(args.repo)
        trace_dirs = (
            tuple(Path(trace_dir) for trace_dir in args.trace_dir)
            if args.trace_dir
            else tuple(default_trace_dirs(repo))
        )
        result = run_daemon_cycle(
            repo,
            DaemonLoopConfig(
                worker_id=args.worker_id,
                max_jobs_per_cycle=args.max_jobs,
                concurrency_limit=args.concurrency,
                lease_duration=timedelta(seconds=args.lease_seconds),
                trace_dirs=trace_dirs,
                kill_switch=default_kill_switch(repo),
            ),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "daemon" and args.daemon_command == "serve":
        repo = Path(args.repo)
        socket_path = Path(args.socket) if args.socket else sidecar_dir(repo) / "daemon.sock"
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
        print(json.dumps(result, indent=2, sort_keys=True))
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
        _persist_harness_report(repo, report)
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
        report = generate_harness_report(repo)
        _persist_harness_report(repo, report)
        candidates = generate_cleanup_candidates(repo)
        path = sidecar_dir(repo) / "harness-cleanup-candidates.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "candidates": [candidate.to_json_dict() for candidate in candidates],
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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

    parser.error(f"unknown command: {args.command}")
    return 2


def console_main() -> None:
    raise SystemExit(main())


def _persist_harness_report(repo: Path, report) -> Path:
    report_path = sidecar_dir(repo) / "harness-report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "knowledge_map": report.knowledge_map,
                "missing_docs": report.missing_docs,
                "stale_docs": report.stale_docs,
                "orphaned_runbooks": report.orphaned_runbooks,
                "recurring_failures_without_docs": report.recurring_failures_without_docs,
                "doc_gardening_tasks": report.doc_gardening_tasks,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
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
        payload={"note": note},
    )


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
        timeout_ms=60_000,
        retry_attempts=0,
        retry_backoff_ms=0,
        checkpoint_path=run_dir / "acceptance-summary" / "checkpoint.json",
        input_paths={
            "candidate_patch": run_dir / "candidate.raw.json",
            "policy_gate": run_dir / "policy-gate.json",
            "eval_reports": run_dir / "eval-report.json",
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
    payload = json.loads(run.output_paths["acceptance_summary"].read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("llmff acceptance_summary output must be a JSON object")
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
        sources=tuple(
            SourceRef(str(source["source_id"]), trusted=bool(source["trusted"]))
            for source in metadata.get("sources", [])
        ),
        bounded_edit_metadata=tuple(
            dict(item) for item in metadata.get("bounded_edit_metadata", []) if isinstance(item, dict)
        ),
    )


def _decision_from_artifact(run_dir: Path):
    from tugboat.policy.gate import PolicyDecision

    payload = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    return PolicyDecision(bool(payload["allowed"]), tuple(payload["reasons"]))


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
    if not bool(policy_gate["allowed"]):
        raise ValueError("stored policy gate rejected candidate")
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    if not bool(eval_report["passed"]):
        raise ValueError("eval report did not pass")
    _assert_eval_acceptance(eval_report, policy_gate)
    explicit_human_review = _requires_explicit_human_review(candidate.risk_class)
    if explicit_human_review and (not human_review or review_actor == "tugboat"):
        raise ValueError("Class C candidates require explicit human review")
    if auto_apply and mode != "commit":
        raise ValueError("auto-apply requires commit mode")

    adapter = VcsAdapter(repo)
    if auto_apply:
        _assert_auto_apply_user_worktree_clean(adapter)
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
            pr_metadata = adapter.pull_request_metadata(
                candidate_id=candidate_id,
                base_file=candidate.base_file,
                branch_name=branch_name,
                base_branch=base_branch,
                rationale=candidate.rationale,
            ).to_json_dict()
    except VcsStateError:
        if branch_created and not applied_commit:
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
        "pr_metadata": pr_metadata,
        "review_actor": review_actor,
        "auto_apply": auto_apply,
        "explicit_human_review": explicit_human_review,
        "review_required_reasons": list(decision.review_required_reasons),
        "decision_rationale": "policy gate and eval report passed",
    }
    path = run_dir / "apply-plan.json"
    validate_json_artifact("apply-plan.json", payload)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
                    rollback_command=rollback_command,
                ),
            )
        if auto_apply and auto_apply_approval is not None:
            approval_path = run_dir / "auto-apply-approval.json"
            validate_json_artifact("auto-apply-approval.json", auto_apply_approval)
            approval_path.write_text(
                json.dumps(auto_apply_approval, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
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
            categories=(candidate.risk_class,),
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


def _assert_auto_apply_user_worktree_clean(adapter: VcsAdapter) -> None:
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
            categories=(candidate.risk_class,),
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
        "rollback_command": rollback_command,
    }


def _relative_repo_path(repo: Path, path: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def _requires_explicit_human_review(risk_class: str) -> bool:
    normalized = risk_class.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized in {"c", "class_c", "restricted_policy_change"}


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


def _write_optimization_summary(repo: Path, run_dir: Path, *, suite_id: str) -> int:
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate_id = int(candidate["candidate_id"])
    eval_report_path = run_dir / "eval-report.json"
    policy_gate_path = run_dir / "policy-gate.json"
    decision = "rejected"
    reason = "proposal rejected"
    trigger_score: float | None = None
    held_out_score: float | None = None
    governance_passed = False
    recommendation = "reject"
    if eval_report_path.exists():
        eval_report = json.loads(eval_report_path.read_text(encoding="utf-8"))
        trigger_score = _score_from_eval_report(eval_report, "trigger_score")
        held_out_score = _score_from_eval_report(eval_report, "held_out_score")
        governance_passed = bool(eval_report.get("governance_passed", False))
        recommendation = str(eval_report.get("recommendation", "reject"))
        try:
            _assert_eval_acceptance(eval_report, _read_optional_json_object(policy_gate_path))
        except ValueError as error:
            reason = str(error)
        else:
            decision = "needs_review"
            reason = "held_out_improved"

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
    }
    validate_json_artifact("optimization-summary.json", summary)
    (run_dir / "optimization-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
    commit_sha = str(apply_plan["applied_commit"])
    if not commit_sha:
        raise ValueError("apply plan has no applied commit")
    target_files = tuple(str(path) for path in apply_plan["target_files"])
    adapter = VcsAdapter(repo)
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
    payload = {
        "schema_version": SCHEMA_VERSION,
        "decision_id": str(apply_plan["decision_id"]),
        "candidate_id": int(apply_plan["candidate_id"]),
        "metadata": metadata.to_json_dict(),
        "executed": execute,
        "revert_commit": revert_commit,
    }
    path = run_dir / "rollback-plan.json"
    validate_json_artifact("rollback-plan.json", payload)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "rollback.planned",
            {
                "candidate_id": int(apply_plan["candidate_id"]),
                "commit_sha": commit_sha,
                "decision_id": str(apply_plan["decision_id"]),
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
                    "revert_commit": revert_commit,
                    "target_files": list(target_files),
                },
            )
    return path


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


if __name__ == "__main__":
    console_main()
