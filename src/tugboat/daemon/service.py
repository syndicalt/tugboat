from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tugboat.artifacts import ArtifactValidationError, validate_json_artifact
from tugboat.audit.pipeline import AuditPipelineResult, run_audit_pipeline
from tugboat.config import load_policy
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
from tugboat.security.secrets import scan_text


class DaemonJobPayloadError(ValueError):
    pass


@dataclass(frozen=True)
class DaemonRunConfig:
    worker_id: str
    lease_duration: timedelta
    kill_switch: KillSwitch | None = None
    now: datetime | None = None
    max_attempts: int = 3


def daemon_status(
    repo: Path,
    *,
    kill_switch: KillSwitch | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    queue_path = repo / ".sidecar" / "daemon.sqlite"
    if not queue_path.exists():
        return {
            "queue_path": ".sidecar/daemon.sqlite",
            "kill_switch_enabled": bool(kill_switch and kill_switch.is_enabled()),
            "jobs_by_state": {},
            "oldest_queued_job_id": None,
            "leased_job_count": 0,
            "stuck_job_count": 0,
            "oldest_stuck_job_id": None,
            "oldest_stuck_lease_expires_at": None,
            "recovery_hint": None,
        }
    queue = DaemonQueue.open_sidecar(repo)
    try:
        timestamp = _status_timestamp(now)
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
        leased = queue.connection.execute(
            """
            SELECT COUNT(*) FROM daemon_jobs
            WHERE state IN (?, ?, ?)
            """,
            (
                JobState.INSPECTING.value,
                JobState.RUNNING.value,
                JobState.EVALUATING.value,
            ),
        ).fetchone()
        stuck = queue.connection.execute(
            """
            SELECT id, lease_expires_at FROM daemon_jobs
            WHERE state IN (?, ?, ?)
              AND lease_expires_at IS NOT NULL
              AND lease_expires_at <= ?
            ORDER BY lease_expires_at, id
            """,
            (
                JobState.INSPECTING.value,
                JobState.RUNNING.value,
                JobState.EVALUATING.value,
                timestamp,
            ),
        ).fetchall()
        stuck_count = len(stuck)
        oldest_stuck = stuck[0] if stuck else None
        return {
            "queue_path": queue.path.relative_to(repo).as_posix(),
            "kill_switch_enabled": bool(kill_switch and kill_switch.is_enabled()),
            "jobs_by_state": {str(row[0]): int(row[1]) for row in rows},
            "oldest_queued_job_id": int(oldest[0]) if oldest is not None else None,
            "leased_job_count": int(leased[0]) if leased is not None else 0,
            "stuck_job_count": stuck_count,
            "oldest_stuck_job_id": int(oldest_stuck[0]) if oldest_stuck is not None else None,
            "oldest_stuck_lease_expires_at": (
                str(oldest_stuck[1]) if oldest_stuck is not None else None
            ),
            "recovery_hint": (
                "run tugboat daemon run-once --repo <repo> to recover stale leases"
                if stuck_count
                else None
            ),
        }
    finally:
        queue.close()


def _status_timestamp(now: datetime | None) -> str:
    timestamp = now or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    return timestamp.isoformat(timespec="microseconds")


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
        record_recovered_job_states(repo, queue, recovered)
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
        result: Any = None
        if running.kind == "trace_audit":
            result = _execute_trace_audit(
                repo,
                _authoritative_execution_payload(repo, running.kind, running.payload),
            )
        elif running.kind == "proposal":
            result = _execute_proposal(
                repo,
                _authoritative_execution_payload(repo, running.kind, running.payload),
            )
        elif running.kind == "eval":
            result = _execute_eval(
                repo,
                _authoritative_execution_payload(repo, running.kind, running.payload),
            )
        elif running.kind == "optimization":
            result = _execute_optimization(
                repo,
                _authoritative_execution_payload(repo, running.kind, running.payload),
            )
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
    if "trace_artifact_ref" in payload:
        trace_path = _repo_relative_artifact_path(
            repo,
            _required_payload_text(payload, "trace_artifact_ref"),
        )
    else:
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


def _execute_optimization(repo: Path, payload: dict[str, Any]) -> Any:
    from tugboat.cli import run_optimize_workflow

    trace_path = _trace_path_from_payload(repo, payload)
    train_traces = tuple(
        _repo_relative_artifact_path(repo, artifact_ref)
        for artifact_ref in _payload_text_list(payload, "train_trace_artifact_refs")
    )
    return run_optimize_workflow(
        repo,
        trace_path,
        suite_id=_required_payload_text(payload, "suite"),
        train_traces=train_traces,
        held_out_episodes=tuple(_payload_text_list(payload, "held_out_episode_ids")),
        unseen_suites=tuple(_payload_text_list(payload, "unseen_suites")),
        trace_format=str(payload.get("trace_format", "auto")),
    )


def _trace_path_from_payload(repo: Path, payload: dict[str, Any]) -> Path:
    if "trace_artifact_ref" in payload:
        return _repo_relative_artifact_path(
            repo,
            _required_payload_text(payload, "trace_artifact_ref"),
        )
    trace_path = Path(_required_payload_text(payload, "trace_path")).expanduser().resolve()
    if not trace_path.is_relative_to(repo.resolve()):
        raise DaemonJobPayloadError("trace_path must resolve inside repo")
    return trace_path


def _payload_text_list(payload: dict[str, Any], key: str) -> list[str]:
    raw = payload.get(key, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise DaemonJobPayloadError(f"{key} must be a list of strings")
    return list(raw)


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


def _authoritative_execution_payload(
    repo: Path,
    queue_kind: str,
    queue_payload: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(queue_payload, dict):
        raise DaemonJobPayloadError("daemon job payload must be a JSON object")
    if "artifact_ref" not in queue_payload and "request_id" not in queue_payload:
        return queue_payload
    request = _load_mcp_request_artifact(repo, queue_kind, queue_payload)
    execution = request.get("execution")
    if not isinstance(execution, dict):
        raise DaemonJobPayloadError("mcp request missing execution")
    payload = execution.get("payload")
    if not isinstance(payload, dict):
        raise DaemonJobPayloadError("mcp request execution payload must be a JSON object")
    return dict(payload)


def _load_mcp_request_artifact(
    repo: Path,
    queue_kind: str,
    queue_payload: dict[str, Any],
) -> dict[str, Any]:
    artifact_ref = _required_payload_text(queue_payload, "artifact_ref")
    artifact_path = _request_artifact_path(repo, artifact_ref)
    try:
        text = artifact_path.read_text(encoding="utf-8")
        findings = scan_text(artifact_ref, text)
        if findings:
            raise DaemonJobPayloadError("mcp request artifact contains secrets")
        artifact = json.loads(text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DaemonJobPayloadError("mcp request artifact is unreadable") from exc
    if not isinstance(artifact, dict):
        raise DaemonJobPayloadError("mcp request artifact must be a JSON object")
    try:
        validate_json_artifact("mcp-request.json", artifact)
    except ArtifactValidationError as exc:
        raise DaemonJobPayloadError("mcp request artifact is invalid") from exc

    request_id = _required_payload_text(queue_payload, "request_id")
    if artifact.get("request_id") != request_id:
        raise DaemonJobPayloadError("mcp request_id does not match queue payload")
    expected_request_kind = {
        "trace_audit": "audit",
        "proposal": "proposal",
        "eval": "eval",
        "optimization": "optimization",
    }.get(queue_kind)
    if artifact.get("kind") != expected_request_kind:
        raise DaemonJobPayloadError("mcp request kind does not match queue job")
    execution = artifact.get("execution")
    if not isinstance(execution, dict) or execution.get("kind") != queue_kind:
        raise DaemonJobPayloadError("mcp request execution kind does not match queue job")
    execution_payload = execution.get("payload")
    if not isinstance(execution_payload, dict):
        raise DaemonJobPayloadError("mcp request execution payload must be a JSON object")
    for key, value in execution_payload.items():
        if queue_payload.get(key) != value:
            raise DaemonJobPayloadError(f"mcp request queue payload mismatch: {key}")
    _validate_request_policy_current(repo, artifact)
    return artifact


def _request_artifact_path(repo: Path, artifact_ref: str) -> Path:
    if Path(artifact_ref).is_absolute():
        raise DaemonJobPayloadError("mcp request artifact_ref must be repo-relative")
    requests_root = sidecar_dir(repo) / "mcp" / "requests"
    sidecar_root = sidecar_dir(repo).resolve()
    resolved_requests_root = requests_root.resolve()
    if not resolved_requests_root.is_relative_to(sidecar_root):
        raise DaemonJobPayloadError("mcp request root must resolve inside sidecar")
    artifact_path = (repo / artifact_ref).resolve()
    if not artifact_path.is_relative_to(resolved_requests_root):
        raise DaemonJobPayloadError("mcp request artifact_ref must resolve inside request root")
    if artifact_path.suffix != ".json":
        raise DaemonJobPayloadError("mcp request artifact must be JSON")
    if not artifact_path.is_file():
        raise DaemonJobPayloadError("mcp request artifact does not exist")
    return artifact_path


def _repo_relative_artifact_path(repo: Path, artifact_ref: str) -> Path:
    if Path(artifact_ref).is_absolute():
        raise DaemonJobPayloadError("artifact ref must be repo-relative")
    repo_root = repo.resolve()
    artifact_path = (repo / artifact_ref).resolve()
    if not artifact_path.is_relative_to(repo_root):
        raise DaemonJobPayloadError("artifact ref must resolve inside repo")
    return artifact_path


def _validate_request_policy_current(repo: Path, artifact: dict[str, Any]) -> None:
    expected = artifact.get("repo_policy")
    if not isinstance(expected, dict):
        raise DaemonJobPayloadError("mcp request missing repo policy")
    current = _current_repo_policy_ref(repo)
    if expected != current:
        raise DaemonJobPayloadError("mcp request repo policy is stale")


def _current_repo_policy_ref(repo: Path) -> dict[str, Any]:
    path = sidecar_dir(repo) / "policy.yaml"
    policy = load_policy(repo)
    if not path.exists():
        return {
            "path": ".sidecar/policy.yaml",
            "version": policy.version,
            "hash": None,
        }
    return {
        "path": path.relative_to(repo).as_posix(),
        "version": policy.version,
        "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


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


def record_recovered_job_states(repo: Path, queue: DaemonQueue, job_ids: tuple[int, ...]) -> None:
    for job_id in job_ids:
        job = queue.get_job(job_id)
        if job is not None:
            _record_job_state(repo, job.id, job.state, payload=job.payload)
