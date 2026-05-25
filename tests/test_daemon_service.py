from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tugboat.cli import main
from tugboat.daemon.queue import DaemonQueue, FileKillSwitch, JobState
from tugboat.daemon.service import (
    DaemonRunConfig,
    daemon_status,
    run_daemon_once,
    serve_daemon_socket,
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


def test_daemon_cycle_cli_watches_trace_dir_and_reports_discovery(tmp_path: Path, capsys):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    (trace_dir / "episode.jsonl").write_text(
        '{"type":"user_request","text":"Keep the runbook current"}\n',
        encoding="utf-8",
    )

    exit_code = main(
        [
            "daemon",
            "cycle",
            "--repo",
            str(tmp_path),
            "--trace-dir",
            str(trace_dir),
            "--max-jobs",
            "0",
            "--concurrency",
            "0",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["trace_discovery"] == {"discovered": 1, "skipped": 0}
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.get_job(1)
        assert job is not None
        assert job.kind == "trace_audit"
        assert job.payload == {"trace_path": str(trace_dir / "episode.jsonl")}


def test_daemon_unix_socket_serves_status_and_exits_after_bounded_requests(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(kind="audit", payload={"trace_id": "trace-1"}, now=_at(0))
    socket_path = tmp_path / ".sidecar" / "daemon.sock"
    result: dict[str, object] = {}

    thread = threading.Thread(
        target=lambda: result.update(
            serve_daemon_socket(
                tmp_path,
                socket_path=socket_path,
                config=DaemonRunConfig(
                    worker_id="socket-worker",
                    lease_duration=timedelta(seconds=30),
                    now=_at(10),
                ),
                max_requests=1,
            )
        )
    )
    thread.start()
    with _connect_unix_socket(socket_path) as client:
        client.sendall(b'{"command":"status"}\n')
        response = json.loads(client.recv(4096).decode("utf-8"))

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert response["jobs_by_state"] == {"queued": 1}
    assert response["socket_path"] == ".sidecar/daemon.sock"
    assert result == {"requests_served": 1, "socket_path": ".sidecar/daemon.sock"}


def test_daemon_serve_cli_can_exit_without_accepting_requests(tmp_path: Path, capsys):
    exit_code = main(
        [
            "daemon",
            "serve",
            "--repo",
            str(tmp_path),
            "--max-requests",
            "0",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"requests_served": 0, "socket_path": ".sidecar/daemon.sock"}


def _connect_unix_socket(path: Path) -> socket.socket:
    for _ in range(100):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(path))
            return client
        except (FileNotFoundError, ConnectionRefusedError):
            client.close()
            time.sleep(0.01)
    raise AssertionError(f"socket was not created: {path}")


def _at(seconds: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, seconds, tzinfo=timezone.utc)
