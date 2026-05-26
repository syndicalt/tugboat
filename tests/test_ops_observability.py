from __future__ import annotations

import json

from tugboat.ops.observability import summarize_observability


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
