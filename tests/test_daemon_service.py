from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tugboat.cli import main
from tugboat.daemon.queue import DaemonQueue, FileKillSwitch, JobState
from tugboat.daemon.service import (
    DaemonRunConfig,
    daemon_status,
    run_daemon_once,
)
from tugboat.mcp import tugboat_daemon_status


def test_daemon_status_summarizes_queue_and_kill_switch(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(kind="audit", payload={"trace_id": "trace-1"}, now=_at(0))
        active = queue.enqueue(kind="proposal", payload={"audit_id": "audit-1"}, now=_at(1))
        queue.transition(active.id, JobState.INSPECTING, now=_at(2))
    kill_switch = tmp_path / ".sidecar" / "read-only.kill"
    kill_switch.parent.mkdir(parents=True, exist_ok=True)
    kill_switch.write_text("enabled\n", encoding="utf-8")

    status = daemon_status(tmp_path, kill_switch=FileKillSwitch(kill_switch))

    assert status == {
        "queue_path": ".sidecar/daemon.sqlite",
        "kill_switch_enabled": True,
        "jobs_by_state": {"inspecting": 1, "queued": 1},
        "oldest_queued_job_id": 1,
    }


def test_run_daemon_once_processes_one_job_through_waiting_review(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.enqueue(kind="audit", payload={"trace_id": "trace-1"}, now=_at(0))

    result = run_daemon_once(
        tmp_path,
        DaemonRunConfig(
            worker_id="worker-a",
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result == {
        "processed": True,
        "job_id": job.id,
        "final_state": "waiting_review",
        "recovered_jobs": [],
    }
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(job.id).state is JobState.WAITING_REVIEW  # type: ignore[union-attr]


def test_run_daemon_once_respects_read_only_kill_switch(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.enqueue(kind="audit", payload={"trace_id": "trace-1"}, now=_at(0))
    kill_switch = tmp_path / ".sidecar" / "read-only.kill"
    kill_switch.parent.mkdir(parents=True, exist_ok=True)
    kill_switch.write_text("enabled\n", encoding="utf-8")

    result = run_daemon_once(
        tmp_path,
        DaemonRunConfig(
            worker_id="worker-a",
            lease_duration=timedelta(seconds=30),
            kill_switch=FileKillSwitch(kill_switch),
            now=_at(10),
        ),
    )

    assert result == {
        "processed": False,
        "job_id": None,
        "final_state": None,
        "recovered_jobs": [],
    }
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(job.id).state is JobState.QUEUED  # type: ignore[union-attr]


def test_daemon_status_cli_and_mcp_read_queue_state(tmp_path: Path, capsys):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(kind="eval", payload={"candidate_id": "candidate-1"}, now=_at(0))

    exit_code = main(["daemon", "status", "--repo", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "kill_switch_enabled: false" in output
    assert "queued: 1" in output
    assert tugboat_daemon_status(tmp_path)["jobs_by_state"] == {"queued": 1}


def test_daemon_run_once_cli_returns_processed_summary(tmp_path: Path, capsys):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(kind="audit", payload={"trace_id": "trace-1"}, now=_at(0))

    exit_code = main(["daemon", "run-once", "--repo", str(tmp_path), "--worker-id", "cli-worker"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["processed"] is True
    assert payload["final_state"] == "waiting_review"


def _at(seconds: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, seconds, tzinfo=timezone.utc)
