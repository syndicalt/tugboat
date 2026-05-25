from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tugboat.daemon.queue import DaemonQueue, JobState
from tugboat.daemon.runner import (
    DaemonLoopConfig,
    discover_trace_jobs,
    run_daemon_cycle,
    write_worktree_profile,
)


def test_discover_trace_jobs_enqueues_new_jsonl_traces_once(tmp_path: Path):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    (trace_dir / "episode.jsonl").write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    first = discover_trace_jobs(tmp_path, [trace_dir], now=_at(0))
    second = discover_trace_jobs(tmp_path, [trace_dir], now=_at(1))

    assert first == {"discovered": 1, "skipped": 0}
    assert second == {"discovered": 0, "skipped": 1}
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.get_job(1)
        assert job is not None
        assert job.kind == "trace_audit"
        assert job.payload["trace_path"] == str(trace_dir / "episode.jsonl")


def test_run_daemon_cycle_watches_configured_trace_dirs_without_duplicate_enqueue(
    tmp_path: Path,
):
    trace_dir = tmp_path / "configured-traces"
    trace_dir.mkdir()
    (trace_dir / "episode.jsonl").write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    first = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=0,
            concurrency_limit=0,
            lease_duration=timedelta(seconds=30),
            trace_dirs=(trace_dir,),
            now=_at(0),
        ),
    )
    second = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=0,
            concurrency_limit=0,
            lease_duration=timedelta(seconds=30),
            trace_dirs=(trace_dir,),
            now=_at(1),
        ),
    )

    assert first["trace_discovery"] == {"discovered": 1, "skipped": 0}
    assert second["trace_discovery"] == {"discovered": 0, "skipped": 1}
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        rows = queue.connection.execute(
            "SELECT kind, payload_json FROM daemon_jobs ORDER BY id"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "trace_audit"
    assert json.loads(rows[0]["payload_json"]) == {
        "trace_path": str(trace_dir / "episode.jsonl")
    }


def test_run_daemon_cycle_applies_rate_and_concurrency_limits(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        for number in range(3):
            queue.enqueue(kind="audit", payload={"n": number}, now=_at(number))

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=2,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["processed_jobs"] == [1]
    assert result["rate_limited"] is True
    assert result["concurrency_limited"] is True
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.WAITING_REVIEW  # type: ignore[union-attr]
        assert queue.get_job(2).state is JobState.QUEUED  # type: ignore[union-attr]


def test_run_daemon_cycle_requeues_checkpoint_resume_when_manifest_hash_matches(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    checkpoint = run_dir / "checkpoint.json"
    checkpoint.write_text(json.dumps({"manifest_hash": "abc123"}) + "\n", encoding="utf-8")
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(
            kind="llmff_resume",
            payload={
                "run_id": "run-1",
                "manifest_hash": "abc123",
                "checkpoint_path": str(checkpoint),
            },
            now=_at(0),
        )

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["resume_jobs"] == [
        {
            "job_id": 1,
            "run_id": "run-1",
            "checkpoint_path": str(checkpoint),
            "manifest_hash": "abc123",
        }
    ]


def test_run_daemon_cycle_fails_checkpoint_resume_on_manifest_mismatch(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    checkpoint = run_dir / "checkpoint.json"
    checkpoint.write_text(json.dumps({"manifest_hash": "old"}) + "\n", encoding="utf-8")
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(
            kind="llmff_resume",
            payload={
                "run_id": "run-1",
                "manifest_hash": "new",
                "checkpoint_path": str(checkpoint),
            },
            now=_at(0),
        )

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["failed_jobs"] == [{"job_id": 1, "reason": "checkpoint_manifest_mismatch"}]
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.FAILED  # type: ignore[union-attr]


def test_write_worktree_profile_records_local_observability_refs(tmp_path: Path):
    profile_path = write_worktree_profile(
        tmp_path,
        app_boot={"command": "python -m app"},
        observability_refs=["http://127.0.0.1:3000/health"],
    )

    assert profile_path == tmp_path / ".sidecar" / "worktree-profile.json"
    assert json.loads(profile_path.read_text(encoding="utf-8")) == {
        "app_boot": {"command": "python -m app"},
        "observability_refs": ["http://127.0.0.1:3000/health"],
        "runs_dir": ".sidecar/runs",
    }


def _at(seconds: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, seconds, tzinfo=timezone.utc)
