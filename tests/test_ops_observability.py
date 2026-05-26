from __future__ import annotations

import gc
import json
import warnings
from pathlib import Path

from tugboat.db import Store
from tugboat.llmff.contracts import RunResult
from tugboat.ops.observability import summarize_observability, summarize_sidecar_observability


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
            },
            {
                "suite_id": "governance",
                "completed_at": "2026-05-25T11:00:00Z",
                "score": 0.9,
            },
            {
                "suite_id": "provider-smoke",
                "completed_at": "2026-05-25T11:30:00Z",
                "passed": False,
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
