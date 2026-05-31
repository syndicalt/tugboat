from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tugboat.artifacts import validate_json_artifact
from tugboat.cli import main
from tugboat.daemon.queue import DaemonQueue
from tugboat.db import Store
from tugboat.paths import sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef


def _insert_candidate_for_audit(
    store: Store,
    tmp_path: Path,
    *,
    audit_id: int,
) -> int:
    diff = "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use regression tests.\n"
    diff_path = tmp_path / f"candidate-{audit_id}.diff"
    diff_path.write_text(diff, encoding="utf-8")
    candidate = CandidatePatch(
        audit_id=audit_id,
        base_file="CODEX.md",
        base_hash="base",
        diff=diff,
        risk_class="instruction_clarification",
        rationale="seeded observability candidate",
        sources=(SourceRef("ev-1", trusted=True),),
    )
    return store.insert_candidate(
        audit_id=audit_id,
        candidate=candidate,
        diff_path=diff_path,
        state="needs_review",
    )


def test_ops_observability_cli_writes_summary_from_sidecar_state(tmp_path: Path, capsys):
    repo = tmp_path
    sidecar = sidecar_dir(repo)
    run_dir = sidecar / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    eval_report = run_dir / "eval-report.json"
    with Store.open(sidecar / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="eval",
            manifest_hash="manifest-hash",
            status="completed",
            run_dir=run_dir,
        )
        for run_id in ("run-2", "run-3", "run-4"):
            store.insert_run(
                run_id=run_id,
                stage="proposal",
                manifest_hash="manifest-hash",
                status="completed",
                run_dir=sidecar / "runs" / run_id,
            )
        audit_id = store.insert_audit(
            run_id="run-2",
            failure_class="missing_tests",
            severity="medium",
            confidence=0.9,
            evidence_refs=["ev-1"],
            instruction_refs=[],
        )
        second_audit_id = store.insert_audit(
            run_id="run-3",
            failure_class="missing_tests",
            severity="medium",
            confidence=0.8,
            evidence_refs=["ev-2"],
            instruction_refs=[],
        )
        store.insert_audit(
            run_id="run-4",
            failure_class="stale_runbook",
            severity="low",
            confidence=0.7,
            evidence_refs=["ev-3"],
            instruction_refs=[],
        )
        candidate_id = _insert_candidate_for_audit(store, tmp_path, audit_id=audit_id)
        rejected_candidate_id = _insert_candidate_for_audit(
            store,
            tmp_path,
            audit_id=second_audit_id,
        )
        eval_report.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "candidate_id": candidate_id,
                    "suite_id": "all",
                    "passed": True,
                    "trigger_score": 0.7,
                    "held_out_score": 0.9,
                    "governance_passed": True,
                    "recommendation": "accept",
                    "metrics": {"governance_regressions": 2},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id="all",
            report_path=eval_report,
            passed=True,
            metrics={"governance_regressions": 2, "held_out_score": 0.9},
        )
        store.insert_decision(
            candidate_id=candidate_id,
            actor="reviewer",
            policy="apply_controller",
            decision="applied",
            reason="accepted",
        )
        store.insert_decision(
            candidate_id=rejected_candidate_id,
            actor="reviewer",
            policy="apply_controller",
            decision="rejected",
            reason="too broad",
        )
        store.append_audit_event(
            "run_failed",
            {
                "run_id": "run-2",
                "failure_kind": "provider_error",
                "provider": "openai",
                "backend": "responses",
                "status": "failed",
                "duration_seconds": 12,
            },
        )
        store.append_audit_event(
            "apply.applied",
            {"candidate_id": candidate_id, "changed_lines": 4},
        )
        store.append_audit_event("rollback.applied", {"candidate_id": 9})
        store.record_harness_finding(
            repo_path=repo,
            finding="Duplicate instruction rule appears 2 times: run tests.",
            severity="duplicate_rule",
        )
        trace_event = store.append_audit_event(
            "trace_event.recorded",
            {
                "episode_id": None,
                "evidence_id": "ev-1",
                "event_type": "user_correction",
            },
        )
        document_event = store.append_audit_event(
            "document.indexed",
            {"repo": str(repo), "path": "CODEX.md", "kind": "agent_policy", "hash": "abc"},
        )
    with closing(sqlite3.connect(sidecar / "db.sqlite")) as connection:
        connection.execute(
            """
            INSERT INTO trace_events(
              episode_id, evidence_id, event_type, line_number, payload_json, audit_event_sequence
            )
            VALUES (NULL, 'ev-1', 'user_correction', 1, ?, ?)
            """,
            (json.dumps({"content": "Run tests before final."}, sort_keys=True), trace_event.sequence),
        )
        connection.execute(
            """
            INSERT INTO documents(
              repo_path, path, kind, precedence, protected, hash, mtime,
              parser_version, audit_event_sequence
            )
            VALUES (?, 'CODEX.md', 'agent_policy', 70, 1, 'abc', 1, 'test', ?)
            """,
            (str(repo), document_event.sequence),
        )
        connection.commit()
    with DaemonQueue.open_sidecar(repo) as queue:
        queue.enqueue(kind="trace_audit", payload={"trace_path": "trace.jsonl"})

    assert main(["ops", "observability", "--repo", str(repo)]) == 0

    output = capsys.readouterr().out
    output_path = sidecar / "ops" / "observability" / "summary.json"
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    validate_json_artifact("observability-summary.json", payload)
    assert f"observability summary: {output_path}" in output
    assert payload["schema_version"] == 1
    summary = payload["summary"]
    assert summary["run_duration"]["count"] == 5
    assert summary["failure_kind_counts"] == {"provider_error": 1}
    assert summary["edits"] == {"accepted": 1, "rejected": 1, "rolled_back": 1}
    assert summary["eval_suite_trends"]["all"]["latest_score"] == 0.9
    assert summary["governance_regression_count"] == 2
    assert summary["provider_backend_failure_rate"] == {"failed": 1, "rate": 1, "total": 1}
    assert summary["corpus_growth"] == {"earliest_count": 1, "latest_count": 1, "delta": 0}
    assert summary["duplicate_rule_count"] == 1
    assert summary["user_correction_recurrence"]["correction_count"] == 1
    assert summary["recurring_incident_rate"] == {
        "incident_count": 3,
        "recurring_incident_count": 2,
        "rate": 0.666667,
        "unique_incident_class_count": 2,
    }
    assert summary["daemon_queue"] == {
        "jobs_by_state": {"queued": 1},
        "oldest_queued_job_id": 1,
        "kill_switch_enabled": False,
    }
    assert summary["auto_apply_lanes"] == {
        "docs_hygiene": {
            "eligible": 0,
            "rejected": 0,
            "staged": 0,
            "applied": 0,
            "rolled_back": 0,
            "paused": 0,
        },
        "skill_improvement": {
            "eligible": 0,
            "rejected": 0,
            "staged": 0,
            "applied": 0,
            "rolled_back": 0,
            "paused": 0,
        },
    }
