from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tugboat.audit.pipeline import AuditPipelineResult, run_audit_pipeline
from tugboat.daemon.queue import (
    DaemonQueue,
    FileKillSwitch,
    JobState,
    KillSwitch,
    QueuePayloadError,
)
from tugboat.db import Store
from tugboat.eval.pipeline import EvalPipelineResult, run_eval_pipeline
from tugboat.paths import ensure_private_dir, mark_private_file, sidecar_dir
from tugboat.propose.pipeline import ProposePipelineResult, run_propose_pipeline


class DaemonJobPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class DaemonRunConfig:
    worker_id: str
    lease_duration: timedelta
    kill_switch: KillSwitch | None = None
    now: datetime | None = None
    max_attempts: int = 3


def daemon_status(repo: Path, *, kill_switch: KillSwitch | None = None) -> dict[str, Any]:
    queue_path = repo / ".sidecar" / "daemon.sqlite"
    if not queue_path.exists():
        return {
            "queue_path": ".sidecar/daemon.sqlite",
            "kill_switch_enabled": bool(kill_switch and kill_switch.is_enabled()),
            "jobs_by_state": {},
            "oldest_queued_job_id": None,
        }
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
    if config.kill_switch is not None and config.kill_switch.is_enabled():
        return {
            "processed": False,
            "job_id": None,
            "final_state": None,
            "recovered_jobs": [],
        }
    queue = DaemonQueue.open_sidecar(repo)
    try:
        recovered = queue.mark_stale_leases(
            now=config.now,
            max_attempts=config.max_attempts,
        )
        try:
            job = queue.acquire_next(
                lease_owner=config.worker_id,
                lease_duration=config.lease_duration,
                now=config.now,
                kill_switch=config.kill_switch,
            )
        except QueuePayloadError as error:
            _record_job_state(
                repo,
                error.job_id,
                JobState.FAILED,
                payload={"queue_payload_invalid": True},
            )
            return {
                "processed": True,
                "job_id": error.job_id,
                "final_state": "failed",
                "recovered_jobs": list(recovered),
            }
        if job is None:
            return {
                "processed": False,
                "job_id": None,
                "final_state": None,
                "recovered_jobs": list(recovered),
            }
        _record_job_state(repo, job.id, job.state, payload=job.payload)
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
    repo = repo.resolve()
    socket_path = socket_path.expanduser().resolve()
    socket_ref = _sidecar_relative_socket_path(repo, socket_path)
    ensure_private_dir(socket_path.parent)
    socket_path.unlink(missing_ok=True)
    requests_served = 0
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            mark_private_file(socket_path)
            server.listen(1)
            while max_requests is None or requests_served < max_requests:
                connection, _ = server.accept()
                with connection:
                    try:
                        request = _read_socket_request(connection)
                    except (json.JSONDecodeError, ValueError):
                        response = {"error": "invalid daemon socket request"}
                    else:
                        response = _handle_socket_request(repo, config, request)
                    response["socket_path"] = socket_ref
                    connection.sendall(
                        (json.dumps(response, sort_keys=True) + "\n").encode("utf-8")
                    )
                requests_served += 1
    finally:
        socket_path.unlink(missing_ok=True)
    return {
        "requests_served": requests_served,
        "socket_path": socket_ref,
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


def _sidecar_relative_socket_path(repo: Path, socket_path: Path) -> str:
    sidecar_root = repo / ".sidecar"
    if sidecar_root.is_symlink():
        raise ValueError("socket_path must resolve inside repo sidecar")
    sidecar_root = sidecar_root.resolve()
    if not socket_path.is_relative_to(sidecar_root):
        raise ValueError("socket_path must resolve inside repo sidecar")
    return socket_path.relative_to(repo).as_posix()


def process_daemon_job(repo: Path, queue: DaemonQueue, job_id: int, *, now: datetime | None) -> Any:
    running = queue.transition(job_id, JobState.RUNNING, now=now)
    _record_job_state(repo, running.id, running.state, payload=running.payload)
    try:
        result: AuditPipelineResult | ProposePipelineResult | EvalPipelineResult | None = None
        if running.kind == "trace_audit":
            result = _execute_trace_audit(repo, running.payload)
        elif running.kind == "proposal":
            result = _execute_proposal(repo, running.payload)
        elif running.kind == "eval":
            result = _execute_eval(repo, running.payload)
        elif running.kind == "audit":
            result = None
        else:
            return _fail_daemon_job(repo, queue, running.id, now=now)
    except DaemonJobPayloadError:
        return _fail_daemon_job(repo, queue, running.id, now=now)

    if running.kind == "eval":
        evaluating = queue.transition(running.id, JobState.EVALUATING, now=now)
        _record_job_state(repo, evaluating.id, evaluating.state, payload=evaluating.payload)
        final_state = JobState.WAITING_REVIEW if result is not None and result.exit_code == 0 else JobState.REJECTED
        waiting_review = queue.transition(evaluating.id, final_state, now=now)
    else:
        if result is not None and result.exit_code != 0:
            return _fail_daemon_job(repo, queue, running.id, now=now)
        waiting_review = queue.transition(running.id, JobState.WAITING_REVIEW, now=now)
    _record_job_state(repo, waiting_review.id, waiting_review.state, payload=waiting_review.payload)
    return waiting_review


def _execute_trace_audit(repo: Path, payload: dict[str, Any]) -> AuditPipelineResult:
    repo_root = repo.resolve()
    trace_path = Path(_required_payload_text(payload, "trace_path")).expanduser().resolve()
    if not trace_path.is_relative_to(repo_root):
        raise DaemonJobPayloadError("trace_path must resolve inside repo")
    trace_format = str(payload.get("trace_format", "auto"))
    return run_audit_pipeline(repo, trace_path, trace_format=trace_format)


def _execute_proposal(repo: Path, payload: dict[str, Any]) -> ProposePipelineResult:
    return run_propose_pipeline(repo, _required_payload_text(payload, "audit_id"))


def _execute_eval(repo: Path, payload: dict[str, Any]) -> EvalPipelineResult:
    return run_eval_pipeline(
        repo,
        _required_payload_text(payload, "candidate_id"),
        _required_payload_text(payload, "suite"),
    )


def _fail_daemon_job(
    repo: Path,
    queue: DaemonQueue,
    job_id: int,
    *,
    now: datetime | None,
) -> Any:
    failed = queue.transition(job_id, JobState.FAILED, now=now)
    _record_job_state(repo, failed.id, failed.state, payload=failed.payload)
    return failed


def _required_payload_text(payload: Any, key: str) -> str:
    if not isinstance(payload, dict):
        raise DaemonJobPayloadError("daemon job payload must be a JSON object")
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DaemonJobPayloadError(f"daemon job payload missing {key}")
    return value


def _record_job_state(
    repo: Path,
    job_id: int,
    state: JobState,
    *,
    payload: dict[str, Any] | None = None,
) -> None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.update_daemon_job_state(
            job_id=str(job_id),
            repo_path=repo,
            state=state.value,
            payload=payload,
        )
