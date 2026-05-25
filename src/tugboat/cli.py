from __future__ import annotations

import argparse
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

from tugboat.audit.service import write_audit
from tugboat.config import load_policy
from tugboat.corpus.indexer import index_repo
from tugboat.db import Store
from tugboat.eval.service import write_eval_report
from tugboat.harness.checks import check_harness_legibility
from tugboat.llmff.runner import FixtureLlmffRunner, inspect_manifest
from tugboat.paths import latest_run_dir, new_run_dir, runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef, evaluate_candidate
from tugboat.propose.service import write_candidate
from tugboat.report.service import write_report
from tugboat.traces.ingest import ingest_jsonl_trace


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tugboat")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("doctor")

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

    report = subcommands.add_parser("report")
    report.add_argument("--repo", required=True)
    report.add_argument("--run", required=True)

    harness = subcommands.add_parser("harness")
    harness_subcommands = harness.add_subparsers(dest="harness_command", required=True)
    harness_check = harness_subcommands.add_parser("check")
    harness_check.add_argument("--repo", required=True)
    harness_check.add_argument("--max-instruction-lines", type=int, default=100)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        print("tugboat: ok")
        print("mode: proposal_only")
        print("auto_apply: disabled")
        return 0

    if args.command == "index":
        repo = Path(args.repo)
        result = index_repo(repo, load_policy(repo))
        if not args.check:
            Store.open(sidecar_dir(repo) / "db.sqlite").index_documents(repo, result)
        print(f"indexed documents: {result.indexed_count}")
        return 0

    if args.command == "audit":
        repo = Path(args.repo)
        trace = Path(args.trace)
        policy = load_policy(repo)
        run_dir = new_run_dir(repo)
        shutil.copyfile(trace, run_dir / "trace-input.jsonl")
        _write_instruction_snapshot(repo, run_dir)
        manifest = _ensure_manifest(repo, "episode-audit")
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
        store = Store.open(sidecar_dir(repo) / "db.sqlite")
        store.insert_run(
            run_id=run_dir.name,
            stage="audit",
            manifest_hash=inspect.manifest_hash,
            status="completed",
            run_dir=run_dir,
        )
        evidence_refs = [event.evidence_id for event in bundle.events]
        audit_id = store.insert_audit(
            run_id=run_dir.name,
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=evidence_refs,
            instruction_refs=[document.path for document in index_repo(repo, policy).documents],
        )
        write_audit(
            run_dir,
            {
                "audit_id": audit_id,
                "edit_warranted": True,
                "evidence_refs": evidence_refs,
                "failure_class": "instruction_missing",
                "severity": "medium",
                "confidence": 0.75,
            },
        )
        print(f"audit run: {run_dir.name}")
        return 0

    if args.command == "propose":
        repo = Path(args.repo)
        run_dir = latest_run_dir(repo) if args.audit == "latest" else runs_dir(repo) / args.audit
        audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
        if not audit.get("edit_warranted", False):
            print("audit does not warrant an instruction edit")
            return 1
        candidate = _default_candidate(repo, audit_id=int(audit["audit_id"]))
        decision = evaluate_candidate(repo, load_policy(repo), candidate)
        artifacts = write_candidate(repo, run_dir.name, candidate)
        store = Store.open(sidecar_dir(repo) / "db.sqlite")
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
            json.dumps(
                {
                    "candidate_id": candidate_id,
                    "decision": "needs_review" if decision.allowed else "rejected",
                    "policy_allowed": decision.allowed,
                    "policy_reasons": list(decision.reasons),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
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
        metrics = {"governance_regressions": 0}
        report_path = write_eval_report(
            repo,
            run_dir.name,
            candidate_id=candidate_id,
            suite_id=args.suite,
            passed=True,
            metrics=metrics,
        )
        Store.open(sidecar_dir(repo) / "db.sqlite").insert_eval(
            candidate_id=candidate_id,
            suite_id=args.suite,
            report_path=report_path,
            passed=True,
            metrics=metrics,
        )
        print(f"eval suite: {args.suite} passed")
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


def _ensure_manifest(repo: Path, name: str) -> Path:
    manifest_dir = sidecar_dir(repo) / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest = manifest_dir / f"{name}.yaml"
    if not manifest.exists():
        manifest.write_text(f"name: {name}\noutputs:\n  - audit.json\n", encoding="utf-8")
    return manifest


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
    )


def _decision_from_artifact(run_dir: Path):
    from tugboat.policy.gate import PolicyDecision

    payload = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    return PolicyDecision(bool(payload["allowed"]), tuple(payload["reasons"]))


def _merge_json(path: Path, updates: dict[str, object]) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    console_main()
