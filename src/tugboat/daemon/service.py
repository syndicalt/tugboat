from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tugboat.audit.service import write_audit
from tugboat.daemon.queue import DaemonQueue, FileKillSwitch, JobState, KillSwitch
from tugboat.db import Store
from tugboat.paths import new_run_dir, sidecar_dir
from tugboat.traces.ingest import ingest_jsonl_trace


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
        final_job = _process_job(repo, queue, job.id, now=config.now)
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


def _process_job(repo: Path, queue: DaemonQueue, job_id: int, *, now: datetime | None) -> Any:
    running = queue.transition(job_id, JobState.RUNNING, now=now)
    if running.kind == "trace_audit":
        _execute_trace_audit(repo, running.payload)
    evaluating = queue.transition(running.id, JobState.EVALUATING, now=now)
    return queue.transition(evaluating.id, JobState.WAITING_REVIEW, now=now)


def _execute_trace_audit(repo: Path, payload: dict[str, Any]) -> None:
    trace_path = Path(str(payload["trace_path"]))
    bundle = ingest_jsonl_trace(trace_path)
    run_dir = new_run_dir(repo)
    evidence_refs = [event.evidence_id for event in bundle.events]
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        episode_id = store.record_trace_episode(repo=repo, bundle=bundle)
        store.insert_run(
            run_id=run_dir.name,
            stage="audit",
            manifest_hash="daemon-trace-audit",
            status="completed",
            run_dir=run_dir,
            episode_id=episode_id,
        )
        audit_id = store.insert_audit(
            run_id=run_dir.name,
            failure_class="daemon_trace_audit",
            severity="medium",
            confidence=0.75,
            evidence_refs=evidence_refs,
            instruction_refs=[],
        )
    write_audit(
        run_dir,
        {
            "audit_id": audit_id,
            "edit_warranted": True,
            "evidence_refs": evidence_refs,
            "failure_class": "daemon_trace_audit",
            "severity": "medium",
            "confidence": 0.75,
            "instruction_refs": [],
        },
    )
