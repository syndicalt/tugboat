from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
from tugboat.audit.pipeline import detect_trace_format
from tugboat.config import load_policy
from tugboat.daemon.queue import (
    DaemonQueue,
    FileKillSwitch,
    JobState,
    KillSwitch,
    QueuePayloadError,
)
from tugboat.daemon.service import process_daemon_job
from tugboat.db import Store
from tugboat.llmff.runner import run_manifest as run_llmff_manifest
from tugboat.paths import ensure_private_dir, mark_private_file, sidecar_dir
from tugboat.security.secrets import SecretScanError, scan_path, scan_text


_SAFE_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class DaemonLoopConfig:
    worker_id: str
    max_jobs_per_cycle: int
    concurrency_limit: int
    lease_duration: timedelta
    trace_dirs: tuple[Path, ...] = ()
    kill_switch: KillSwitch | None = None
    now: datetime | None = None
    max_attempts: int = 3


@dataclass(frozen=True)
class ResumeValidation:
    resume: dict[str, Any] | None
    failure_reason: str | None = None


def discover_trace_jobs(
    repo: Path,
    trace_dirs: list[Path],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    registry = _load_discovered_traces(repo)
    eligible_traces: list[tuple[Path, str, str]] = []
    planned_registry = set(registry)
    discovered = 0
    skipped = 0
    repo_root = repo.resolve()
    for trace_dir in trace_dirs:
        trace_dir_path = trace_dir.resolve()
        trace_paths = sorted(
            path
            for pattern in ("*.jsonl", "*.json")
            for path in trace_dir_path.glob(pattern)
        )
        if not trace_dir_path.is_relative_to(repo_root):
            skipped += len(trace_paths)
            continue
        for path in trace_paths:
            trace_target = path.resolve()
            if not trace_target.is_relative_to(repo_root):
                skipped += 1
                continue
            trace_key = str(trace_target)
            if trace_key in planned_registry:
                skipped += 1
                continue
            try:
                scan_path(path)
                trace_format = detect_trace_format(path)
            except (OSError, ValueError, SecretScanError, json.JSONDecodeError):
                skipped += 1
                continue
            eligible_traces.append((path, trace_key, trace_format))
            planned_registry.add(trace_key)

    validate_json_artifact("daemon-discovered-traces.json", _discovered_traces_payload(planned_registry))

    with DaemonQueue.open_sidecar(repo) as queue:
        for path, trace_key, trace_format in eligible_traces:
            payload = {"trace_format": trace_format, "trace_path": str(path)}
            job = queue.enqueue(kind="trace_audit", payload=payload, now=now)
            with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
                store.record_daemon_job(
                    job_id=str(job.id),
                    repo_path=repo,
                    state=job.state.value,
                    payload=payload,
                )
            registry.add(trace_key)
            discovered += 1
    _write_discovered_traces(repo, registry)
    return {"discovered": discovered, "skipped": skipped}


def run_daemon_cycle(repo: Path, config: DaemonLoopConfig) -> dict[str, Any]:
    processed_jobs: list[int] = []
    failed_jobs: list[dict[str, Any]] = []
    resume_jobs: list[dict[str, Any]] = []
    if config.kill_switch is not None and config.kill_switch.is_enabled():
        return {
            "processed_jobs": processed_jobs,
            "failed_jobs": failed_jobs,
            "resume_jobs": resume_jobs,
            "recovered_jobs": [],
            "trace_discovery": {"discovered": 0, "skipped": 0},
            "rate_limited": False,
            "concurrency_limited": False,
        }
    trace_discovery = discover_trace_jobs(repo, list(config.trace_dirs), now=config.now)
    with DaemonQueue.open_sidecar(repo) as queue:
        recovered = queue.mark_stale_leases(
            now=config.now,
            max_attempts=config.max_attempts,
        )
        slots = max(0, min(config.max_jobs_per_cycle, config.concurrency_limit))
        for _ in range(slots):
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
                failed_jobs.append({"job_id": error.job_id, "reason": "queue_payload_invalid"})
                continue
            if job is None:
                break
            _record_job_state(repo, job.id, job.state, payload=job.payload)
            if job.kind == "llmff_resume":
                validation = _resume_metadata(repo, job.id, job.payload)
                if validation.failure_reason is not None:
                    failed = queue.transition(job.id, JobState.FAILED, now=config.now)
                    _record_job_state(repo, failed.id, failed.state, payload=failed.payload)
                    failed_jobs.append({"job_id": job.id, "reason": validation.failure_reason})
                    continue
                if validation.resume is not None:
                    resume_jobs.append(validation.resume)
                    _record_resume_ready(repo, validation.resume)
                    if _resume_can_execute(validation.resume):
                        final_job = _execute_resume(repo, queue, job.id, validation.resume, now=config.now)
                        if final_job.state is JobState.FAILED:
                            failed_jobs.append({"job_id": job.id, "reason": "resume_failed"})
                            continue
                        processed_jobs.append(job.id)
                    continue
            final_job = process_daemon_job(repo, queue, job.id, now=config.now)
            if final_job.state is JobState.FAILED:
                failed_jobs.append({"job_id": job.id, "reason": "job_failed"})
                continue
            processed_jobs.append(job.id)
        remaining_queued = _queued_count(queue)

    return {
        "processed_jobs": processed_jobs,
        "failed_jobs": failed_jobs,
        "resume_jobs": resume_jobs,
        "recovered_jobs": list(recovered),
        "trace_discovery": trace_discovery,
        "rate_limited": remaining_queued > 0,
        "concurrency_limited": config.concurrency_limit < config.max_jobs_per_cycle,
    }


def run_daemon_loop(
    repo: Path,
    config: DaemonLoopConfig,
    *,
    cycles: int,
    interval_seconds: float = 0.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    if cycles < 1:
        raise ValueError("cycles must be at least 1")
    if interval_seconds < 0:
        raise ValueError("interval_seconds must be non-negative")

    cycle_results = []
    for index in range(cycles):
        cycle_results.append(run_daemon_cycle(repo, config))
        if index < cycles - 1 and interval_seconds > 0:
            sleep(interval_seconds)
    return {
        "cycle_count": cycles,
        "cycles": cycle_results,
    }


def write_worktree_profile(
    repo: Path,
    *,
    app_boot: dict[str, Any],
    observability_refs: list[str],
) -> Path:
    path = repo / ".sidecar" / "worktree-profile.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "app_boot": app_boot,
        "observability_refs": observability_refs,
        "runs_dir": ".sidecar/runs",
    }
    _write_private_json_artifact(path, "worktree-profile.json", payload)
    return path


def default_trace_dirs(repo: Path) -> list[Path]:
    return [repo / ".sidecar" / "traces"]


def default_runner_kill_switch(repo: Path) -> FileKillSwitch:
    return FileKillSwitch(repo / ".sidecar" / "read-only.kill")


def _resume_metadata(repo: Path, job_id: int, payload: Any) -> ResumeValidation:
    if not isinstance(payload, dict):
        return ResumeValidation(resume=None, failure_reason="resume_payload_invalid")
    run_id = _resume_payload_text(payload, "run_id")
    checkpoint_ref = _resume_payload_text(payload, "checkpoint_path")
    manifest_hash = _resume_payload_text(payload, "manifest_hash")
    if run_id is None or checkpoint_ref is None or manifest_hash is None:
        return ResumeValidation(resume=None, failure_reason="resume_payload_invalid")
    if not run_id or run_id in {".", ".."} or _SAFE_RUN_ID_PATTERN.fullmatch(run_id) is None:
        return ResumeValidation(resume=None, failure_reason="invalid_run_id")
    runs_root = (repo / ".sidecar" / "runs").resolve()
    run_dir = (runs_root / run_id).resolve()
    if not run_dir.is_relative_to(runs_root):
        return ResumeValidation(resume=None, failure_reason="run_dir_outside_runs")
    checkpoint_path = Path(checkpoint_ref).expanduser().resolve()
    if not checkpoint_path.is_relative_to(run_dir):
        return ResumeValidation(resume=None, failure_reason="checkpoint_path_outside_run")
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ResumeValidation(resume=None, failure_reason="checkpoint_unreadable")
    if not isinstance(checkpoint, dict):
        return ResumeValidation(resume=None, failure_reason="checkpoint_unreadable")
    if str(checkpoint.get("manifest_hash")) != manifest_hash:
        return ResumeValidation(resume=None, failure_reason="checkpoint_manifest_mismatch")
    resume: dict[str, Any] = {
        "job_id": job_id,
        "run_id": run_id,
        "checkpoint_path": str(checkpoint_path),
        "manifest_hash": manifest_hash,
    }
    for key in (
        "manifest_path",
        "input_paths",
        "output_paths",
        "timeout_ms",
        "retry_attempts",
        "retry_backoff_ms",
    ):
        if key in payload:
            resume[key] = payload[key]
        elif key in checkpoint:
            resume[key] = checkpoint[key]
    return ResumeValidation(resume=resume)


def _resume_payload_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if not isinstance(value, str):
        return None
    return value


def _queued_count(queue: DaemonQueue) -> int:
    return int(
        queue.connection.execute(
            "SELECT COUNT(*) FROM daemon_jobs WHERE state = ?",
            (JobState.QUEUED.value,),
        ).fetchone()[0]
    )


def _resume_can_execute(resume: dict[str, Any]) -> bool:
    return isinstance(resume.get("manifest_path"), str) and isinstance(resume.get("output_paths"), dict)


def _execute_resume(
    repo: Path,
    queue: DaemonQueue,
    job_id: int,
    resume: dict[str, Any],
    *,
    now: datetime | None,
) -> Any:
    running = queue.transition(job_id, JobState.RUNNING, now=now)
    _record_job_state(repo, running.id, running.state, payload=running.payload)
    policy = load_policy(repo)
    try:
        result = run_llmff_manifest(
            _resume_path(repo, resume, "manifest_path"),
            run_dir=(repo / ".sidecar" / "runs" / str(resume["run_id"])).resolve(),
            policy=policy,
            timeout_ms=_resume_positive_int(resume.get("timeout_ms"), 60_000),
            retry_attempts=_resume_non_negative_int(resume.get("retry_attempts"), 0),
            retry_backoff_ms=_resume_non_negative_int(resume.get("retry_backoff_ms"), 0),
            checkpoint_path=Path(str(resume["checkpoint_path"])).resolve(),
            input_paths=_resume_paths(repo, resume.get("input_paths")),
            output_paths=_resume_paths(repo, resume.get("output_paths")),
        )
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.record_llmff_run(
                run_id=str(resume["run_id"]),
                manifest_hash=str(resume["manifest_hash"]),
                result=result,
            )
    except Exception:
        failed = queue.transition(running.id, JobState.FAILED, now=now)
        _record_job_state(repo, failed.id, failed.state, payload=failed.payload)
        return failed
    evaluating = queue.transition(running.id, JobState.EVALUATING, now=now)
    _record_job_state(repo, evaluating.id, evaluating.state, payload=evaluating.payload)
    final_state = JobState.WAITING_REVIEW if result.exit_code == 0 else JobState.FAILED
    final_job = queue.transition(evaluating.id, final_state, now=now)
    _record_job_state(repo, final_job.id, final_job.state, payload=final_job.payload)
    return final_job


def _resume_path(repo: Path, resume: dict[str, Any], key: str) -> Path:
    value = resume.get(key)
    if not isinstance(value, str):
        raise ValueError(f"resume {key} must be a path")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"resume {key} must exist")
    if not path.is_relative_to(repo.resolve()):
        raise ValueError(f"resume {key} must resolve inside repo")
    return path


def _resume_paths(repo: Path, raw_paths: object) -> dict[str, Path]:
    if raw_paths is None:
        return {}
    if not isinstance(raw_paths, dict):
        raise ValueError("resume paths must be a JSON object")
    paths: dict[str, Path] = {}
    repo_root = repo.resolve()
    for name, raw_path in raw_paths.items():
        if not isinstance(name, str) or not name:
            raise ValueError("resume path names must be non-empty strings")
        if not isinstance(raw_path, str):
            raise ValueError(f"resume path for {name} must be a string")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_relative_to(repo_root):
            raise ValueError(f"resume path for {name} must resolve inside repo")
        paths[name] = path
    return paths


def _resume_positive_int(value: object, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("resume timeout_ms must be a positive integer")
    return value


def _resume_non_negative_int(value: object, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("resume retry values must be non-negative integers")
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


def _record_resume_ready(repo: Path, resume: dict[str, Any]) -> None:
    checkpoint_path = Path(str(resume["checkpoint_path"]))
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "daemon_job.resume_ready",
            {
                "job_id": str(resume["job_id"]),
                "repo": str(repo),
                "run_id": str(resume["run_id"]),
                "checkpoint_path": checkpoint_path.resolve().relative_to(repo.resolve()).as_posix(),
                "manifest_hash": str(resume["manifest_hash"]),
            },
        )


def _load_discovered_traces(repo: Path) -> set[str]:
    path = repo / ".sidecar" / "discovered-traces.json"
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(payload, dict):
        traces = payload.get("traces")
        if not isinstance(traces, list):
            return set()
        return {str(item) for item in traces}
    if not isinstance(payload, list):
        return set()
    return {str(item) for item in payload}


def _write_discovered_traces(repo: Path, traces: set[str]) -> None:
    path = repo / ".sidecar" / "discovered-traces.json"
    payload = _discovered_traces_payload(traces)
    _write_private_json_artifact(path, "daemon-discovered-traces.json", payload)


def _discovered_traces_payload(traces: set[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "traces": sorted(traces),
    }


def _write_private_json_artifact(path: Path, artifact_name: str, payload: dict[str, Any]) -> None:
    validate_json_artifact(artifact_name, payload)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    findings = scan_text(path.as_posix(), text)
    if findings:
        raise SecretScanError(findings)
    ensure_private_dir(path.parent)
    path.write_text(text, encoding="utf-8")
    mark_private_file(path)
