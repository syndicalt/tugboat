from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tugboat.cli import main
from tugboat.daemon.queue import DaemonQueue, FileKillSwitch, JobState
from tugboat.daemon.service import (
    DaemonRunConfig,
    daemon_status,
    run_daemon_once,
    serve_daemon_socket,
)
from tugboat.mcp import tugboat_daemon_status


def _write_fake_llmff(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:3] == ["inspect", "--format", "json"]:
    print(json.dumps({"manifest": Path(args[3]).stem, "network_required": False}))
    raise SystemExit(0)

if args[:1] == ["run"]:
    manifest = Path(args[1]).stem
    trace = Path(args[args.index("--trace") + 1])
    events = Path(args[args.index("--events") + 1])
    checkpoint = Path(args[args.index("--checkpoint") + 1])
    outputs = {}
    index = 0
    while index < len(args):
        if args[index] == "--output":
            outputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        index += 1
    trace.write_text('{"event":"step","name":"' + manifest + '"}\\n', encoding="utf-8")
    events.write_text('{"event":"run_completed"}\\n', encoding="utf-8")
    checkpoint.write_text('{"manifest_hash":"fake"}\\n', encoding="utf-8")
    if manifest == "instruction-index":
        outputs["instruction_index"].write_text(json.dumps({
            "documents": [{"path": "CODEX.md", "obligations": ["Use tests."]}]
        }) + "\\n", encoding="utf-8")
    elif manifest == "episode-audit":
        outputs["audit_report"].write_text(json.dumps({
            "edit_warranted": True,
            "failure_class": "instruction_conflict",
            "severity": "high",
            "confidence": 0.91,
            "evidence_refs": ["ev_daemon"],
        }) + "\\n", encoding="utf-8")
    raise SystemExit(0)

raise SystemExit(64)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


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


def test_run_daemon_once_executes_trace_audit_job_through_storage_layer(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    fake_llmff = _write_fake_llmff(repo / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )
    trace = repo / "episode.jsonl"
    trace.write_text(
        '{"type":"user_request","content":"Fix bug"}\n'
        '{"type":"user_correction","content":"Use regression tests"}\n',
        encoding="utf-8",
    )
    with DaemonQueue.open_sidecar(repo) as queue:
        job = queue.enqueue(kind="trace_audit", payload={"trace_path": str(trace)}, now=_at(0))

    result = run_daemon_once(
        repo,
        DaemonRunConfig(
            worker_id="worker-a",
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["processed"] is True
    assert result["job_id"] == job.id
    assert result["final_state"] == "waiting_review"
    run_dirs = sorted((repo / ".sidecar" / "runs").iterdir())
    assert len(run_dirs) == 1
    audit = json.loads((run_dirs[0] / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "instruction_conflict"
    assert audit["severity"] == "high"
    assert audit["confidence"] == 0.91
    assert audit["evidence_refs"] == ["ev_daemon"]
    assert (run_dirs[0] / "audit.raw.json").exists()
    assert (run_dirs[0] / "instruction-index" / "llmff-trace.jsonl").exists()
    assert (run_dirs[0] / "episode-audit" / "llmff-trace.jsonl").exists()
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM runs WHERE stage = 'audit'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM audits").fetchone()[0] == 1
        jobs = connection.execute(
            """
            SELECT manifest_name, status
            FROM llmff_jobs
            ORDER BY id
            """
        ).fetchall()
    assert jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
    ]


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


def test_daemon_unix_socket_returns_error_for_malformed_request_and_cleans_up(
    tmp_path: Path,
):
    socket_path = tmp_path / ".sidecar" / "daemon.sock"
    result: dict[str, object] = {}
    errors: list[BaseException] = []

    def run_server() -> None:
        try:
            result.update(
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
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_server)
    thread.start()
    with _connect_unix_socket(socket_path) as client:
        client.sendall(b"{not-json\n")
        response = json.loads(client.recv(4096).decode("utf-8"))

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert errors == []
    assert response == {
        "error": "invalid daemon socket request",
        "socket_path": ".sidecar/daemon.sock",
    }
    assert result == {"requests_served": 1, "socket_path": ".sidecar/daemon.sock"}
    assert not socket_path.exists()


def test_daemon_unix_socket_rejects_path_outside_repo_before_bind(tmp_path: Path):
    outside_socket = tmp_path.parent / f"{tmp_path.name}-outside.sock"

    with pytest.raises(ValueError, match="socket_path must resolve inside repo sidecar"):
        serve_daemon_socket(
            tmp_path,
            socket_path=outside_socket,
            config=DaemonRunConfig(
                worker_id="socket-worker",
                lease_duration=timedelta(seconds=30),
                now=_at(10),
            ),
            max_requests=0,
        )

    assert not outside_socket.exists()


def test_daemon_unix_socket_rejects_repo_file_path_before_unlink(tmp_path: Path):
    instruction_file = tmp_path / "CODEX.md"
    instruction_file.write_text("# Rules\n\nUse tests.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="socket_path must resolve inside repo sidecar"):
        serve_daemon_socket(
            tmp_path,
            socket_path=instruction_file,
            config=DaemonRunConfig(
                worker_id="socket-worker",
                lease_duration=timedelta(seconds=30),
                now=_at(10),
            ),
            max_requests=0,
        )

    assert instruction_file.read_text(encoding="utf-8") == "# Rules\n\nUse tests.\n"


def test_daemon_unix_socket_rejects_symlinked_sidecar_before_unlink(
    tmp_path: Path,
):
    instruction_file = tmp_path / "CODEX.md"
    instruction_file.write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    (tmp_path / ".sidecar").symlink_to(".")

    with pytest.raises(ValueError, match="socket_path must resolve inside repo sidecar"):
        serve_daemon_socket(
            tmp_path,
            socket_path=tmp_path / ".sidecar" / "CODEX.md",
            config=DaemonRunConfig(
                worker_id="socket-worker",
                lease_duration=timedelta(seconds=30),
                now=_at(10),
            ),
            max_requests=0,
        )

    assert instruction_file.read_text(encoding="utf-8") == "# Rules\n\nUse tests.\n"


def test_daemon_unix_socket_rejects_sidecar_symlink_outside_repo(
    tmp_path: Path,
):
    external_sidecar = tmp_path.parent / f"{tmp_path.name}-external-sidecar"
    external_sidecar.mkdir()
    (tmp_path / ".sidecar").symlink_to(external_sidecar)
    external_socket = external_sidecar / "daemon.sock"

    with pytest.raises(ValueError, match="socket_path must resolve inside repo sidecar"):
        serve_daemon_socket(
            tmp_path,
            socket_path=tmp_path / ".sidecar" / "daemon.sock",
            config=DaemonRunConfig(
                worker_id="socket-worker",
                lease_duration=timedelta(seconds=30),
                now=_at(10),
            ),
            max_requests=0,
        )

    assert not external_socket.exists()


def test_daemon_serve_cli_rejects_path_outside_repo_without_binding(
    tmp_path: Path,
    capsys,
):
    outside_socket = tmp_path.parent / f"{tmp_path.name}-outside.sock"

    exit_code = main(
        [
            "daemon",
            "serve",
            "--repo",
            str(tmp_path),
            "--socket",
            str(outside_socket),
            "--max-requests",
            "0",
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().out == (
        "daemon serve blocked: socket_path must resolve inside repo sidecar\n"
    )
    assert not outside_socket.exists()


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
