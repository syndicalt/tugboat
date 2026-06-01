from __future__ import annotations

import gc
import json
import warnings
from pathlib import Path

from tugboat.daemon.queue import DaemonQueue, JobState
from tugboat.db import Store
from tugboat.llmff.contracts import RunResult
from tugboat.ops.observability import (
    observability_metrics_text,
    summarize_observability,
    summarize_sidecar_observability,
)
from tugboat.policy.gate import CandidatePatch, SourceRef


def _insert_candidate(store: Store, tmp_path: Path) -> int:
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    store.insert_run(
        run_id="run-1",
        stage="proposal",
        manifest_hash="fixture-manifest",
        status="completed",
        run_dir=run_dir,
    )
    audit_id = store.insert_audit(
        run_id="run-1",
        failure_class="instruction_missing",
        severity="medium",
        confidence=0.75,
        evidence_refs=["ev-1"],
        instruction_refs=["CODEX.md"],
    )
    diff = "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use regression tests.\n"
    diff_path = tmp_path / "candidate.diff"
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


def test_summarize_observability_returns_json_safe_phase_10_metrics() -> None:
    summary = summarize_observability(
        runs=[
            {
                "run_id": "run-1",
                "started_at": "2026-05-25T10:00:00Z",
                "finished_at": "2026-05-25T10:00:09Z",
                "provider": "openai",
                "backend": "responses",
                "status": "succeeded",
            },
            {
                "run_id": "run-2",
                "duration_seconds": 21.5,
                "provider": "openai",
                "backend": "responses",
                "status": "failed",
                "failure_kind": "provider_error",
            },
            {
                "run_id": "run-3",
                "duration_seconds": 4,
                "provider": "local",
                "backend": "mock",
                "status": "failed",
                "failure_kind": "backend_error",
            },
        ],
        jobs=[
            {"job_id": 1, "state": "applied", "changed_lines": 4},
            {"job_id": 2, "state": "rejected", "changed_lines": 8},
            {"job_id": 3, "state": "rolled_back", "changed_lines": 12},
            {"job_id": 4, "state": "waiting_review"},
        ],
        evals=[
            {
                "suite_id": "governance",
                "completed_at": "2026-05-25T10:00:00Z",
                "score": 0.7,
                "governance_regressions": 1,
            },
            {
                "suite_id": "governance",
                "completed_at": "2026-05-25T11:00:00Z",
                "score": 0.9,
                "governance_regressions": 0,
            },
            {
                "suite_id": "provider-smoke",
                "completed_at": "2026-05-25T11:30:00Z",
                "passed": False,
                "governance_regressions": 2,
            },
        ],
        corpus_snapshots=[
            {"captured_at": "2026-05-25T09:00:00Z", "document_count": 8},
            {"captured_at": "2026-05-25T12:00:00Z", "document_count": 11},
        ],
        harness_findings=[
            "Duplicate instruction rule appears 2 times: run tests.",
            "CODEX.md has no repo-local markdown references; keep instruction files as short maps.",
            "Duplicate instruction rule appears 3 times: cite evidence.",
            {"finding": "docs/runbook.md is missing ownership metadata.", "severity": "stale_doc"},
            {
                "finding": "docs/runbook.md is missing verification-status metadata.",
                "severity": "stale_doc",
            },
        ],
        trace_events=[
            {"type": "user_correction", "content": "Run regression tests before final."},
            {"type": "user_correction", "content": "Run regression tests before final."},
            {"type": "user_correction", "content": "Use Zaxy memory first."},
            {"type": "tool_call", "content": "pytest"},
        ],
        incidents=[
            {"failure_class": "missing_tests"},
            {"failure_class": "missing_tests"},
            {"failure_class": "stale_runbook"},
        ],
    )

    json.dumps(summary)
    assert summary["run_duration"] == {
        "count": 3,
        "total_seconds": 34.5,
        "average_seconds": 11.5,
        "max_seconds": 21.5,
    }
    assert summary["failure_kind_counts"] == {
        "backend_error": 1,
        "provider_error": 1,
    }
    assert summary["edits"] == {
        "accepted": 1,
        "rejected": 1,
        "rolled_back": 1,
    }
    assert summary["edit_rates"] == {
        "acceptance_rate": 0.333333,
        "rejection_rate": 0.333333,
        "rollback_rate": 0.333333,
        "reviewed_count": 3,
    }
    assert summary["mean_changed_lines"] == 8
    assert summary["governance_regression_count"] == 3
    assert summary["eval_suite_trends"]["governance"] == {
        "count": 2,
        "latest_score": 0.9,
        "previous_score": 0.7,
        "delta": 0.2,
    }
    assert summary["corpus_growth"] == {
        "earliest_count": 8,
        "latest_count": 11,
        "delta": 3,
    }
    assert summary["provider_backend_failure_rate"] == {
        "failed": 2,
        "rate": 0.666667,
        "total": 3,
    }
    assert summary["duplicate_rule_count"] == 2
    assert summary["stale_doc_count"] == 2
    assert summary["user_correction_recurrence"] == {
        "correction_count": 3,
        "recurring_correction_count": 1,
        "unique_correction_count": 2,
    }
    assert summary["recurring_incident_rate"] == {
        "incident_count": 3,
        "recurring_incident_count": 2,
        "rate": 0.666667,
        "unique_incident_class_count": 2,
    }
    assert summary["auto_apply_lanes"] == {}


def test_summarize_sidecar_observability_closes_sqlite_connection(tmp_path: Path):
    with Store.open(tmp_path / ".sidecar" / "db.sqlite"):
        pass

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        summarize_sidecar_observability(tmp_path)
        gc.collect()

    assert [
        warning
        for warning in caught
        if issubclass(warning.category, ResourceWarning)
    ] == []


def test_sidecar_observability_includes_daemon_queue_state(tmp_path: Path):
    repo = tmp_path
    with Store.open(repo / ".sidecar" / "db.sqlite"):
        pass
    with DaemonQueue.open_sidecar(repo) as queue:
        queued = queue.enqueue(kind="trace_audit", payload={"trace_path": "trace.jsonl"})
        active = queue.enqueue(kind="proposal", payload={"audit_id": "1"})
        queue.transition(active.id, JobState.INSPECTING)

    summary = summarize_sidecar_observability(repo)

    assert summary["daemon_queue"] == {
        "jobs_by_state": {"inspecting": 1, "queued": 1},
        "oldest_queued_job_id": queued.id,
        "kill_switch_enabled": False,
        "leased_job_count": 1,
        "stuck_job_count": 0,
        "oldest_stuck_job_id": None,
        "oldest_stuck_lease_expires_at": None,
        "recovery_hint": None,
    }


def test_sidecar_observability_reports_auto_apply_counts_by_lane(tmp_path: Path):
    repo = tmp_path
    with Store.open(repo / ".sidecar" / "db.sqlite") as store:
        store.append_audit_event(
            "auto_apply.shadowed",
            {
                "candidate_id": 6,
                "eligible": True,
                "would_apply": True,
                "lane": "docs_hygiene",
                "phase": "shadow",
                "reasons": [],
            },
        )
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 7,
                "eligible": True,
                "lane": "docs_hygiene",
                "phase": "precheck",
                "reasons": [],
            },
        )
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 7,
                "eligible": True,
                "lane": "docs_hygiene",
                "phase": "final",
                "reasons": [],
            },
        )
        store.append_audit_event(
            "auto_apply.applied",
            {
                "candidate_id": 7,
                "approval_bundle": {"lane": "docs_hygiene"},
            },
        )
        store.append_audit_event("rollback.applied", {"candidate_id": 7})
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 8,
                "eligible": False,
                "lane": "skill_improvement",
                "phase": "precheck",
                "reasons": ["rollback_rate_too_high"],
            },
        )
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 9,
                "eligible": False,
                "lane": None,
                "phase": "precheck",
                "reasons": ["auto_apply_change_type_not_allowed"],
            },
        )

    summary = summarize_sidecar_observability(repo)

    assert summary["auto_apply_lanes"] == {
        "docs_hygiene": {
            "shadowed": 1,
            "eligible": 1,
            "rejected": 0,
            "staged": 1,
            "applied": 1,
            "rolled_back": 1,
            "paused": 0,
        },
        "skill_improvement": {
            "shadowed": 0,
            "eligible": 0,
            "rejected": 1,
            "staged": 0,
            "applied": 0,
            "rolled_back": 0,
            "paused": 0,
        },
        "unmatched": {
            "shadowed": 0,
            "eligible": 0,
            "rejected": 1,
            "staged": 0,
            "applied": 0,
            "rolled_back": 0,
            "paused": 0,
        },
    }


def test_observability_metrics_text_renders_bounded_local_metrics() -> None:
    summary = summarize_observability(
        runs=[
            {
                "duration_seconds": 12,
                "provider": "openai",
                "backend": "responses",
                "status": "failed",
                "failure_kind": 'provider"error',
            }
        ],
        jobs=[{"state": "applied", "changed_lines": 4}],
        auto_apply_events=[
            {
                "event_type": "auto_apply.decided",
                "candidate_id": 1,
                "eligible": True,
                "lane": "docs_hygiene",
                "phase": "precheck",
            }
        ],
        auto_apply_lane_names=["docs_hygiene"],
    ) | {
        "daemon_queue": {
            "jobs_by_state": {"queued": 2},
            "kill_switch_enabled": True,
            "leased_job_count": 1,
            "stuck_job_count": 0,
        }
    }

    metrics = observability_metrics_text(summary).splitlines()

    assert "tugboat_run_duration_seconds_count 1" in metrics
    assert "tugboat_edits_accepted 1" in metrics
    assert 'tugboat_failure_kind_total{failure_kind="provider\\"error"} 1' in metrics
    assert "tugboat_provider_backend_failure_rate 1" in metrics
    assert (
        'tugboat_auto_apply_lane_candidates_total{lane="docs_hygiene",state="eligible"} 1'
        in metrics
    )
    assert 'tugboat_daemon_queue_jobs_total{state="queued"} 2' in metrics
    assert "tugboat_daemon_kill_switch_enabled 1" in metrics


def test_sidecar_observability_reports_paused_auto_apply_lane_from_kill_switch(
    tmp_path: Path,
):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "read-only.kill").write_text("enabled\n", encoding="utf-8")
    with Store.open(sidecar / "db.sqlite") as store:
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 7,
                "eligible": True,
                "lane": "docs_hygiene",
                "phase": "precheck",
                "reasons": [],
            },
        )

    summary = summarize_sidecar_observability(repo)

    assert summary["auto_apply_lanes"]["docs_hygiene"] == {
        "shadowed": 0,
        "eligible": 1,
        "rejected": 0,
        "staged": 1,
        "applied": 0,
        "rolled_back": 0,
        "paused": 1,
    }


def test_sidecar_observability_reports_policy_paused_auto_apply_lane(
    tmp_path: Path,
):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        """
version: 1
auto_apply:
  paused_lanes:
    - docs_hygiene
""".lstrip(),
        encoding="utf-8",
    )
    with Store.open(sidecar / "db.sqlite") as store:
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 7,
                "eligible": True,
                "lane": "docs_hygiene",
                "phase": "precheck",
                "reasons": [],
            },
        )

    summary = summarize_sidecar_observability(repo)

    assert summary["auto_apply_lanes"]["docs_hygiene"] == {
        "shadowed": 0,
        "eligible": 1,
        "rejected": 0,
        "staged": 1,
        "applied": 0,
        "rolled_back": 0,
        "paused": 1,
    }


def test_sidecar_observability_counts_pause_decisions_as_paused_not_rejected(
    tmp_path: Path,
):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        """
version: 1
auto_apply:
  paused_lanes:
    - docs_hygiene
""".lstrip(),
        encoding="utf-8",
    )
    with Store.open(repo / ".sidecar" / "db.sqlite") as store:
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 7,
                "eligible": True,
                "lane": "docs_hygiene",
                "phase": "precheck",
                "reasons": [],
            },
        )
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 7,
                "eligible": False,
                "lane": "docs_hygiene",
                "phase": "final",
                "reasons": ["auto_apply_lane_paused"],
            },
        )
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 7,
                "eligible": False,
                "lane": "docs_hygiene",
                "phase": "final",
                "reasons": ["auto_apply_lane_paused"],
            },
        )
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": 8,
                "eligible": False,
                "lane": "skill_improvement",
                "phase": "precheck",
                "reasons": ["auto_apply_incident_pause_active"],
            },
        )

    summary = summarize_sidecar_observability(repo)

    assert summary["auto_apply_lanes"]["docs_hygiene"] == {
        "shadowed": 0,
        "eligible": 1,
        "rejected": 0,
        "staged": 1,
        "applied": 0,
        "rolled_back": 0,
        "paused": 1,
    }
    assert summary["auto_apply_lanes"]["skill_improvement"] == {
        "shadowed": 0,
        "eligible": 0,
        "rejected": 0,
        "staged": 0,
        "applied": 0,
        "rolled_back": 0,
        "paused": 1,
    }


def test_sidecar_observability_counts_llmff_job_failure_without_events(
    tmp_path: Path,
):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    run_dir = sidecar / "runs" / "run-1"
    lifecycle_dir = run_dir / "patch-eval"
    lifecycle_dir.mkdir(parents=True)
    manifest = tmp_path / "patch-eval.yaml"
    manifest.write_text("name: patch-eval\n", encoding="utf-8")
    with Store.open(sidecar / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="eval",
            manifest_hash="abc",
            status="failed",
            run_dir=run_dir,
        )
        store.record_llmff_run(
            run_id="run-1",
            manifest_hash="abc",
            result=RunResult(
                manifest_path=manifest,
                exit_code=124,
                trace_path=lifecycle_dir / "llmff-trace.jsonl",
                events_path=lifecycle_dir / "missing-events.jsonl",
                checkpoint_path=lifecycle_dir / "checkpoint.json",
                output_paths={},
                failure_kind="timeout",
                failure_message="Timed out after 12000 ms",
            ),
        )

    summary = summarize_sidecar_observability(repo)

    assert summary["failure_kind_counts"] == {"timeout": 1}


def test_sidecar_observability_reports_corpus_growth_from_index_audit_events(
    tmp_path: Path,
):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    with Store.open(sidecar / "db.sqlite") as store:
        store.append_audit_event("documents.indexed", {"repo": str(repo), "documents": 1})
        store.append_audit_event("documents.indexed", {"repo": str(repo), "documents": 3})
        store.append_audit_event("documents.indexed", {"repo": str(repo / "other"), "documents": 9})

    summary = summarize_sidecar_observability(repo)

    assert summary["corpus_growth"] == {"earliest_count": 1, "latest_count": 3, "delta": 2}


def test_sidecar_observability_ignores_non_numeric_governance_regression_metric(
    tmp_path: Path,
):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    run_dir = sidecar / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    eval_report = run_dir / "eval-report.json"
    with Store.open(sidecar / "db.sqlite") as store:
        candidate_id = _insert_candidate(store, tmp_path)
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
                    "metrics": {"governance_regressions": "n/a"},
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
            metrics={},
        )

    summary = summarize_sidecar_observability(repo)

    assert summary["governance_regression_count"] == 0


def test_sidecar_observability_deduplicates_llmff_job_failure_with_run_event(
    tmp_path: Path,
):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    run_dir = sidecar / "runs" / "run-1"
    lifecycle_dir = run_dir / "patch-eval"
    lifecycle_dir.mkdir(parents=True)
    manifest = tmp_path / "patch-eval.yaml"
    manifest.write_text("name: patch-eval\n", encoding="utf-8")
    with Store.open(sidecar / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="eval",
            manifest_hash="abc",
            status="failed",
            run_dir=run_dir,
        )
        store.record_llmff_run(
            run_id="run-1",
            manifest_hash="abc",
            result=RunResult(
                manifest_path=manifest,
                exit_code=124,
                trace_path=lifecycle_dir / "llmff-trace.jsonl",
                events_path=lifecycle_dir / "llmff-events.jsonl",
                checkpoint_path=lifecycle_dir / "checkpoint.json",
                output_paths={},
                failure_kind="timeout",
                failure_message="Timed out after 12000 ms",
            ),
        )
        store.append_audit_event(
            "run_failed",
            {
                "run_id": "run-1",
                "failure_kind": "timeout",
                "status": "failed",
            },
        )

    summary = summarize_sidecar_observability(repo)

    assert summary["failure_kind_counts"] == {"timeout": 1}
