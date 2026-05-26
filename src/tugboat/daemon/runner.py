from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
from tugboat.daemon.queue import (
    DaemonQueue,
    FileKillSwitch,
    JobState,
    KillSwitch,
    QueuePayloadError,
)
from tugboat.daemon.service import process_daemon_job
from tugboat.db import Store
from tugboat.paths import sidecar_dir
from tugboat.security.secrets import SecretScanError, scan_path


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
    eligible_traces: list[tuple[Path, str]] = []
    planned_registry = set(registry)
    discovered = 0
    skipped = 0
    for trace_dir in trace_dirs:
        for path in sorted(trace_dir.glob("*.jsonl")):
            trace_key = str(path.resolve())
            if trace_key in planned_registry:
                skipped += 1
                continue
            try:
                scan_path(path)
            except SecretScanError:
                skipped += 1
                continue
            eligible_traces.append((path, trace_key))
            planned_registry.add(trace_key)

    validate_json_artifact("daemon-discovered-traces.json", _discovered_traces_payload(planned_registry))

    with DaemonQueue.open_sidecar(repo) as queue:
        for path, trace_key in eligible_traces:
            payload = {"trace_path": str(path)}
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
                _record_job_state(repo, error.job_id, JobState.FAILED)
                failed_jobs.append({"job_id": error.job_id, "reason": "queue_payload_invalid"})
                continue
            if job is None:
                break
            _record_job_state(repo, job.id, job.state)
            if job.kind == "llmff_resume":
                validation = _resume_metadata(repo, job.id, job.payload)
                if validation.failure_reason is not None:
                    failed = queue.transition(job.id, JobState.FAILED, now=config.now)
                    _record_job_state(repo, failed.id, failed.state)
                    failed_jobs.append({"job_id": job.id, "reason": validation.failure_reason})
                    continue
                if validation.resume is not None:
                    resume_jobs.append(validation.resume)
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "app_boot": app_boot,
                "observability_refs": observability_refs,
                "runs_dir": ".sidecar/runs",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
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
    return ResumeValidation(
        resume={
            "job_id": job_id,
            "run_id": run_id,
            "checkpoint_path": str(checkpoint_path),
            "manifest_hash": manifest_hash,
        }
    )


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


def _record_job_state(repo: Path, job_id: int, state: JobState) -> None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.update_daemon_job_state(
            job_id=str(job_id),
            repo_path=repo,
            state=state.value,
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
    validate_json_artifact("daemon-discovered-traces.json", payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _discovered_traces_payload(traces: set[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "traces": sorted(traces),
    }
