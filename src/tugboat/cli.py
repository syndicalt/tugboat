from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
from tugboat.audit.service import write_audit
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
from tugboat.eval.service import write_eval_report
from tugboat.evals import run_offline_eval_suite
from tugboat.harness.checks import check_harness_legibility, generate_harness_report
from tugboat.llmff.runner import FixtureLlmffRunner, inspect_manifest, run_manifest
from tugboat.manifests import manifests_are_allowed_by_policy, materialize_manifests
from tugboat.mcp import run_stdio_server
from tugboat.optimization import OptimizationMemory
from tugboat.paths import latest_run_dir, new_run_dir, runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef, evaluate_candidate
from tugboat.propose.service import write_candidate
from tugboat.report.service import write_report
from tugboat.security.redaction import redact_text
from tugboat.security.secrets import SecretScanError, scan_path
from tugboat.traces.ingest import ingest_jsonl_trace
from tugboat.vcs import VcsAdapter, VcsStateError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tugboat")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor")

    status = subcommands.add_parser("status")
    status.add_argument("--repo", required=True)

    index = subcommands.add_parser("index")
    index.add_argument("--repo", required=True)
    index.add_argument("--check", action="store_true")

    audit = subcommands.add_parser("audit")
    audit.add_argument("--repo", required=True)
    audit.add_argument("--trace", required=True)
    audit.add_argument("--mock-llmff-inspect", action="store_true")

    propose = subcommands.add_parser("propose")
    propose.add_argument("--repo", required=True)
    propose.add_argument("--audit", required=True)

    evaluate = subcommands.add_parser("eval")
    evaluate.add_argument("--repo", required=True)
    evaluate.add_argument("--candidate", required=True)
    evaluate.add_argument("--suite", required=True)

    apply = subcommands.add_parser("apply")
    apply.add_argument("--repo", required=True)
    apply.add_argument("--candidate", required=True)
    apply.add_argument("--mode", choices=("proposal", "branch", "commit", "pr"), default="proposal")
    apply.add_argument("--review-actor", default="tugboat")
    apply.add_argument("--human-review", action="store_true")

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
        trace = Path(args.trace)
        policy = load_policy(repo)
        run_dir = new_run_dir(repo)
        shutil.copyfile(trace, run_dir / "trace-input.jsonl")
        _write_instruction_snapshot(repo, run_dir)
        try:
            scan_path(run_dir / "trace-input.jsonl")
            scan_path(run_dir / "instruction-snapshot")
        except SecretScanError as error:
            write_audit(
                run_dir,
                {
                    "audit_id": 0,
                    "edit_warranted": False,
                    "evidence_refs": [],
                    "failure_class": "secret_detected",
                    "severity": "critical",
                    "confidence": 1.0,
                    "secret_findings": [
                        {
                            "path": finding.path,
                            "line_number": finding.line_number,
                            "kind": finding.kind,
                        }
                        for finding in error.findings
                    ],
                },
            )
            print("audit blocked: secret detected")
            return 1
        redacted_trace = run_dir / "trace-redacted.jsonl"
        redacted_trace.write_text(
            redact_text((run_dir / "trace-input.jsonl").read_text(encoding="utf-8")),
            encoding="utf-8",
        )
        manifests = materialize_manifests(repo)
        if not manifests_are_allowed_by_policy(manifests, policy):
            print("manifest hash is not allowed by policy")
            return 1
        manifest = next(record.path for record in manifests if record.name == "episode-audit.yaml")
        runner = (
            FixtureLlmffRunner(
                {
                    "manifest": "episode-audit",
                    "network_required": False,
                    "providers": [],
                }
            )
            if args.mock_llmff_inspect
            else None
        )
        inspect = inspect_manifest(manifest, run_dir=run_dir, policy=policy, runner=runner)
        bundle = ingest_jsonl_trace(trace)
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            episode_id = store.record_trace_episode(repo=repo, bundle=bundle)
        audit_payload = {
            "edit_warranted": True,
            "evidence_refs": [event.evidence_id for event in bundle.events],
            "failure_class": "instruction_missing",
            "severity": "medium",
            "confidence": 0.75,
        }
        if not args.mock_llmff_inspect:
            run = run_manifest(
                manifest,
                run_dir=run_dir,
                policy=policy,
                timeout_ms=60_000,
                retry_attempts=0,
                retry_backoff_ms=0,
                input_paths={
                    "episode_trace": redacted_trace,
                    "instruction_index": run_dir / "instruction-snapshot",
                    "policy": sidecar_dir(repo) / "policy.yaml",
                },
                output_paths={"audit_report": run_dir / "audit.raw.json"},
            )
            if run.exit_code != 0:
                with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
                    store.insert_run(
                        run_id=run_dir.name,
                        stage="audit",
                        manifest_hash=inspect.manifest_hash,
                        status="failed",
                        run_dir=run_dir,
                        episode_id=episode_id,
                    )
                write_audit(
                    run_dir,
                    {
                        "edit_warranted": False,
                        "evidence_refs": audit_payload["evidence_refs"],
                        "failure_class": "llmff_run_failed",
                        "severity": "high",
                        "confidence": 1.0,
                        "llmff_exit_code": run.exit_code,
                        "llmff_failure_kind": run.failure_kind,
                        "llmff_failure_message": run.failure_message,
                    },
                )
                print(f"audit run failed: {run.exit_code}")
                return run.exit_code
            raw_audit = json.loads(run.output_paths["audit_report"].read_text(encoding="utf-8"))
            if not isinstance(raw_audit, dict):
                raise ValueError("llmff audit_report output must be a JSON object")
            audit_payload.update(raw_audit)
        evidence_refs = [str(ref) for ref in audit_payload.get("evidence_refs", [])]
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.insert_run(
                run_id=run_dir.name,
                stage="audit",
                manifest_hash=inspect.manifest_hash,
                status="completed",
                run_dir=run_dir,
                episode_id=episode_id,
            )
            audit_id = store.insert_audit(
                run_id=run_dir.name,
                failure_class=str(audit_payload["failure_class"]),
                severity=str(audit_payload["severity"]),
                confidence=float(audit_payload["confidence"]),
                evidence_refs=evidence_refs,
                instruction_refs=[document.path for document in index_repo(repo, policy).documents],
            )
        audit_payload["audit_id"] = audit_id
        audit_payload["evidence_refs"] = evidence_refs
        write_audit(run_dir, audit_payload)
        print(f"audit run: {run_dir.name}")
        return 0

    if args.command == "propose":
        repo = Path(args.repo)
        run_dir = latest_run_dir(repo) if args.audit == "latest" else runs_dir(repo) / args.audit
        audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
        if not audit.get("edit_warranted", False):
            print("audit does not warrant an instruction edit")
            return 1
        policy = load_policy(repo)
        candidate = (
            _run_patch_propose(repo, run_dir, policy, audit_id=int(audit["audit_id"]))
            if (run_dir / "audit.raw.json").exists()
            else _default_candidate(repo, audit_id=int(audit["audit_id"]))
        )
        decision = evaluate_candidate(repo, policy, candidate)
        artifacts = write_candidate(repo, run_dir.name, candidate)
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            candidate_id = store.insert_candidate(
                audit_id=int(audit["audit_id"]),
                candidate=candidate,
                diff_path=artifacts.diff_path,
                state="needs_review" if decision.allowed else "rejected",
            )
        _merge_json(artifacts.json_path, {"candidate_id": candidate_id})
        (run_dir / "policy-gate.json").write_text(
            json.dumps(
                {"allowed": decision.allowed, "reasons": list(decision.reasons)},
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (run_dir / "decision.json").write_text(
            _decision_json(
                candidate_id=candidate_id,
                decision_value="needs_review" if decision.allowed else "rejected",
                policy_allowed=decision.allowed,
                policy_reasons=list(decision.reasons),
            ),
            encoding="utf-8",
        )
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.insert_decision(
                candidate_id=candidate_id,
                actor="tugboat",
                policy="deterministic_policy_gate",
                decision="needs_review" if decision.allowed else "rejected",
                reason=",".join(decision.reasons),
            )
        print(f"candidate: {run_dir / 'candidate.diff'}")
        return 0 if decision.allowed else 1

    if args.command == "eval":
        repo = Path(args.repo)
        run_dir = latest_run_dir(repo) if args.candidate == "latest" else runs_dir(repo) / args.candidate
        candidate_meta = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
        candidate_id = int(candidate_meta["candidate_id"])
        policy = load_policy(repo)
        passed = True
        metrics = {"governance_regressions": 0}
        policy_decision_payload: dict[str, object] | None = None
        if args.suite == "all" and not (run_dir / "candidate.raw.json").exists():
            offline_report = run_offline_eval_suite(repo, suite_id=args.suite)
            passed = offline_report.passed
            metrics = {
                **offline_report.metrics,
                "trigger_score": offline_report.trigger_score,
                "held_out_score": offline_report.held_out_score,
                "governance_passed": offline_report.governance_passed,
                "recommendation": offline_report.recommendation,
            }
        elif (run_dir / "candidate.raw.json").exists():
            eval_payload, policy_decision_payload = _run_patch_eval(
                repo,
                run_dir,
                policy,
                suite_id=args.suite,
            )
            passed = bool(eval_payload["passed"])
            raw_metrics = eval_payload.get("metrics", {})
            if not isinstance(raw_metrics, dict):
                raise ValueError("llmff eval_report metrics must be a JSON object")
            metrics = raw_metrics
        report_path = write_eval_report(
            repo,
            run_dir.name,
            candidate_id=candidate_id,
            suite_id=args.suite,
            passed=passed,
            metrics=metrics,
        )
        if policy_decision_payload is not None:
            (run_dir / "policy-gate.json").write_text(
                json.dumps(
                    {
                        "allowed": bool(policy_decision_payload["allowed"]),
                        "reasons": list(policy_decision_payload.get("reasons", [])),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.insert_eval(
                candidate_id=candidate_id,
                suite_id=args.suite,
                report_path=report_path,
                passed=passed,
                metrics=metrics,
            )
        print(f"eval suite: {args.suite} {'passed' if passed else 'failed'}")
        return 0 if passed else 1

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
            )
        except (FileNotFoundError, KeyError, VcsStateError, ValueError) as error:
            print(f"apply blocked: {error}")
            return 1
        print(f"apply plan: {apply_path}")
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
        report = generate_harness_report(Path(args.repo))
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
        print("## Doc Gardening Tasks")
        for item in report.doc_gardening_tasks:
            print(f"- {item}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


def console_main() -> None:
    raise SystemExit(main())


def _write_instruction_snapshot(repo: Path, run_dir: Path) -> None:
    snapshot = run_dir / "instruction-snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    for document in index_repo(repo, load_policy(repo)).documents:
        source = repo / document.path
        target = snapshot / document.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _default_candidate(repo: Path, audit_id: int) -> CandidatePatch:
    base_file = "CODEX.md"
    base_path = repo / base_file
    if not base_path.exists():
        base_file = "AGENTS.md"
        base_path = repo / base_file
    return CandidatePatch(
        audit_id=audit_id,
        base_file=base_file,
        base_hash=CandidatePatch.hash_file(base_path),
        diff=f"--- a/{base_file}\n+++ b/{base_file}\n@@\n+Add regression tests for bug fixes.\n",
        risk_class="instruction_clarification",
        rationale="User correction showed missing regression-test guidance.",
        sources=(SourceRef("audit:latest", trusted=True),),
    )


def _run_patch_propose(repo: Path, run_dir: Path, policy, *, audit_id: int) -> CandidatePatch:
    manifests = materialize_manifests(repo)
    if not manifests_are_allowed_by_policy(manifests, policy):
        raise RuntimeError("manifest hash is not allowed by policy")
    manifest = next(record.path for record in manifests if record.name == "patch-propose.yaml")
    inspect_manifest(manifest, run_dir=run_dir, policy=policy)
    optimizer_memory_path = _write_optimizer_memory_artifact(repo, run_dir)
    run = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=policy,
        timeout_ms=60_000,
        retry_attempts=0,
        retry_backoff_ms=0,
        checkpoint_path=run_dir / "checkpoint-patch-propose.json",
        input_paths={
            "instruction_index": run_dir / "instruction-snapshot",
            "drift_clusters": run_dir / "audit.raw.json",
            "optimizer_notes": run_dir / "audit.json",
            "optimizer_memory": optimizer_memory_path,
            "policy": sidecar_dir(repo) / "policy.yaml",
        },
        output_paths={"candidate_patch": run_dir / "candidate.raw.json"},
    )
    if run.exit_code != 0:
        raise RuntimeError(f"llmff patch-propose failed with exit code {run.exit_code}")
    payload = json.loads(run.output_paths["candidate_patch"].read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("llmff candidate_patch output must be a JSON object")
    return _candidate_from_payload(payload, audit_id=audit_id)


def _write_optimizer_memory_artifact(repo: Path, run_dir: Path) -> Path:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        memory = OptimizationMemory.load(store, repo=repo)
    payload = {
        "rejected_edits": [
            {
                "semantic_fingerprint": record.semantic_fingerprint,
                "rejection_reason": record.rejection_reason,
                "source_refs": list(record.source_refs),
            }
            for _, record in sorted(memory.rejected_edits.items())
        ],
        "slow_update_notes": list(memory.slow_update_notes),
    }
    path = run_dir / "optimizer-memory.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _candidate_from_payload(payload: dict[str, object], *, audit_id: int) -> CandidatePatch:
    return CandidatePatch(
        audit_id=audit_id,
        base_file=str(payload["base_file"]),
        base_hash=str(payload["base_hash"]),
        diff=str(payload["diff"]),
        risk_class=str(payload["risk_class"]),
        rationale=str(payload["rationale"]),
        sources=tuple(
            SourceRef(str(source["source_id"]), trusted=bool(source["trusted"]))
            for source in payload.get("sources", [])
            if isinstance(source, dict)
        ),
        bounded_edit_metadata=_bounded_edit_metadata_from_payload(payload),
    )


def _bounded_edit_metadata_from_payload(payload: dict[str, object]) -> tuple[dict[str, object], ...]:
    raw_metadata = payload.get("bounded_edit_metadata", payload.get("operator_metadata", []))
    if not isinstance(raw_metadata, list):
        return ()
    return tuple(dict(item) for item in raw_metadata if isinstance(item, dict))


def _run_patch_eval(
    repo: Path,
    run_dir: Path,
    policy,
    *,
    suite_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    manifests = materialize_manifests(repo)
    if not manifests_are_allowed_by_policy(manifests, policy):
        raise RuntimeError("manifest hash is not allowed by policy")
    manifest = next(record.path for record in manifests if record.name == "patch-eval.yaml")
    inspect_manifest(manifest, run_dir=run_dir, policy=policy)
    suite_path = run_dir / "eval-suite.json"
    suite_path.write_text(json.dumps({"suite_id": suite_id}, indent=2) + "\n", encoding="utf-8")
    run = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=policy,
        timeout_ms=60_000,
        retry_attempts=0,
        retry_backoff_ms=0,
        checkpoint_path=run_dir / "checkpoint-patch-eval.json",
        input_paths={
            "candidate_patch": run_dir / "candidate.raw.json",
            "eval_suite": suite_path,
            "policy": sidecar_dir(repo) / "policy.yaml",
        },
        output_paths={
            "eval_report": run_dir / "eval-report.raw.json",
            "policy_decision": run_dir / "policy-decision.raw.json",
        },
    )
    if run.exit_code != 0:
        raise RuntimeError(f"llmff patch-eval failed with exit code {run.exit_code}")
    eval_payload = json.loads(run.output_paths["eval_report"].read_text(encoding="utf-8"))
    decision_payload = json.loads(run.output_paths["policy_decision"].read_text(encoding="utf-8"))
    if not isinstance(eval_payload, dict) or not isinstance(decision_payload, dict):
        raise ValueError("llmff patch-eval outputs must be JSON objects")
    return eval_payload, decision_payload


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
    _assert_eval_acceptance(eval_report)
    explicit_human_review = _requires_explicit_human_review(candidate.risk_class)
    if explicit_human_review and (not human_review or review_actor == "tugboat"):
        raise ValueError("Class C candidates require explicit human review")

    adapter = VcsAdapter(repo)
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
    pr_metadata: dict[str, object] = {}

    if mode == "branch":
        adapter.create_branch(branch_name)
        adapter.apply_diff(run_dir / "candidate.diff")
        post_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
    elif mode == "commit":
        adapter.create_branch(branch_name)
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
    elif mode == "pr":
        adapter.create_branch(branch_name)
        adapter.apply_diff(run_dir / "candidate.diff")
        post_hashes = {path: CandidatePatch.hash_file(repo / path) for path in target_files}
        pr_metadata = adapter.pull_request_metadata(
            candidate_id=candidate_id,
            base_file=candidate.base_file,
            branch_name=branch_name,
            base_branch=base_branch,
            rationale=candidate.rationale,
        ).to_json_dict()

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
        "explicit_human_review": explicit_human_review,
        "review_required_reasons": list(decision.review_required_reasons),
        "decision_rationale": "policy gate and eval report passed",
    }
    path = run_dir / "apply-plan.json"
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
            },
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
    return path


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


def _assert_eval_acceptance(eval_report: dict[str, object]) -> None:
    metrics = eval_report.get("metrics", {})
    if not isinstance(metrics, dict):
        raise ValueError("eval report metrics must be an object")
    recommendation = metrics.get("recommendation")
    if recommendation is not None and str(recommendation) != "accept":
        raise ValueError(f"eval report recommendation was {recommendation}")
    trigger_score = metrics.get("trigger_score")
    held_out_score = metrics.get("held_out_score")
    if trigger_score is None or held_out_score is None:
        return
    if float(held_out_score) < float(trigger_score):
        raise ValueError("held-out eval score did not improve")


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
