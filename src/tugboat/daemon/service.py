from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tugboat.audit.pipeline import AuditPipelineResult, run_audit_pipeline
from tugboat.daemon.queue import DaemonQueue, FileKillSwitch, JobState, KillSwitch
from tugboat.db import Store
from tugboat.paths import sidecar_dir


@dataclass(frozen=True)
class DaemonRunConfig:
    worker_id: str
    lease_duration: timedelta
    kill_switch: KillSwitch | None = None
    now: datetime | None = None
    max_attempts: int = 3


def daemon_status(repo: Path, *, kill_switch: KillSwitch | None = None) -> dict[str, Any]:
    queue = DaemonQueue.open_sidecar(repo)
    try:
        rows = queue.connection.execute(
            """
            SELECT state, COUNT(*) FROM daemon_jobs
            GROUP BY state
            ORDER BY state
            """
        ).fetchall()
        oldest = queue.connection.execute(
            "SELECT id FROM daemon_jobs WHERE state = ? ORDER BY id LIMIT 1",
            (JobState.QUEUED.value,),
        ).fetchone()
        return {
            "queue_path": queue.path.relative_to(repo).as_posix(),
            "kill_switch_enabled": bool(kill_switch and kill_switch.is_enabled()),
            "jobs_by_state": {str(row[0]): int(row[1]) for row in rows},
            "oldest_queued_job_id": int(oldest[0]) if oldest is not None else None,
        }
    finally:
        queue.close()


def run_daemon_once(repo: Path, config: DaemonRunConfig) -> dict[str, Any]:
    queue = DaemonQueue.open_sidecar(repo)
    try:
        recovered = queue.mark_stale_leases(
            now=config.now,
            max_attempts=config.max_attempts,
        )
        job = queue.acquire_next(
            lease_owner=config.worker_id,
            lease_duration=config.lease_duration,
            now=config.now,
            kill_switch=config.kill_switch,
        )
        if job is None:
            return {
                "processed": False,
                "job_id": None,
                "final_state": None,
                "recovered_jobs": list(recovered),
            }
        _record_job_state(repo, job.id, job.state)
        final_job = process_daemon_job(repo, queue, job.id, now=config.now)
        return {
            "processed": True,
            "job_id": final_job.id,
            "final_state": final_job.state.value,
            "recovered_jobs": list(recovered),
        }
    finally:
        queue.close()


def serve_daemon_socket(
    repo: Path,
    *,
    socket_path: Path,
    config: DaemonRunConfig,
    max_requests: int | None = None,
) -> dict[str, Any]:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    requests_served = 0
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        server.listen(1)
        while max_requests is None or requests_served < max_requests:
            connection, _ = server.accept()
            with connection:
                request = _read_socket_request(connection)
                response = _handle_socket_request(repo, config, request)
                response["socket_path"] = socket_path.relative_to(repo).as_posix()
                connection.sendall((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))
            requests_served += 1
    socket_path.unlink(missing_ok=True)
    return {
        "requests_served": requests_served,
        "socket_path": socket_path.relative_to(repo).as_posix(),
    }


def default_kill_switch(repo: Path) -> FileKillSwitch:
    return FileKillSwitch(repo / ".sidecar" / "read-only.kill")


def _read_socket_request(connection: socket.socket) -> dict[str, Any]:
    data = b""
    while b"\n" not in data:
        chunk = connection.recv(4096)
        if not chunk:
            break
        data += chunk
    if not data.strip():
        return {}
    payload = json.loads(data.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("daemon socket request must be a JSON object")
    return payload


def _handle_socket_request(
    repo: Path,
    config: DaemonRunConfig,
    request: dict[str, Any],
) -> dict[str, Any]:
    command = str(request.get("command", "status"))
    if command == "status":
        return daemon_status(repo, kill_switch=config.kill_switch)
    if command == "run_once":
        return run_daemon_once(repo, config)
    return {"error": f"unknown daemon command: {command}"}


def process_daemon_job(repo: Path, queue: DaemonQueue, job_id: int, *, now: datetime | None) -> Any:
    running = queue.transition(job_id, JobState.RUNNING, now=now)
    _record_job_state(repo, running.id, running.state)
    if running.kind == "trace_audit":
        result = _execute_trace_audit(repo, running.payload)
        if result.exit_code != 0:
            failed = queue.transition(running.id, JobState.FAILED, now=now)
            _record_job_state(repo, failed.id, failed.state)
            return failed
    evaluating = queue.transition(running.id, JobState.EVALUATING, now=now)
    _record_job_state(repo, evaluating.id, evaluating.state)
    waiting_review = queue.transition(evaluating.id, JobState.WAITING_REVIEW, now=now)
    _record_job_state(repo, waiting_review.id, waiting_review.state)
    return waiting_review


def _execute_trace_audit(repo: Path, payload: dict[str, Any]) -> AuditPipelineResult:
    trace_path = Path(str(payload["trace_path"]))
    return run_audit_pipeline(repo, trace_path)


def _record_job_state(repo: Path, job_id: int, state: JobState) -> None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.update_daemon_job_state(
            job_id=str(job_id),
            repo_path=repo,
            state=state.value,
        )
