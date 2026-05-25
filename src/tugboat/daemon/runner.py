from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tugboat.daemon.queue import DaemonQueue, FileKillSwitch, JobState, KillSwitch


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


def discover_trace_jobs(
    repo: Path,
    trace_dirs: list[Path],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    registry = _load_discovered_traces(repo)
    discovered = 0
    skipped = 0
    with DaemonQueue.open_sidecar(repo) as queue:
        for trace_dir in trace_dirs:
            for path in sorted(trace_dir.glob("*.jsonl")):
                trace_key = str(path.resolve())
                if trace_key in registry:
                    skipped += 1
                    continue
                queue.enqueue(kind="trace_audit", payload={"trace_path": str(path)}, now=now)
                registry.add(trace_key)
                discovered += 1
    _write_discovered_traces(repo, registry)
    return {"discovered": discovered, "skipped": skipped}


def run_daemon_cycle(repo: Path, config: DaemonLoopConfig) -> dict[str, Any]:
    processed_jobs: list[int] = []
    failed_jobs: list[dict[str, Any]] = []
    resume_jobs: list[dict[str, Any]] = []
    trace_discovery = discover_trace_jobs(repo, list(config.trace_dirs), now=config.now)
    with DaemonQueue.open_sidecar(repo) as queue:
        recovered = queue.mark_stale_leases(
            now=config.now,
            max_attempts=config.max_attempts,
        )
        slots = max(0, min(config.max_jobs_per_cycle, config.concurrency_limit))
        for _ in range(slots):
            job = queue.acquire_next(
                lease_owner=config.worker_id,
                lease_duration=config.lease_duration,
                now=config.now,
                kill_switch=config.kill_switch,
            )
            if job is None:
                break
            if job.kind == "llmff_resume":
                resume = _resume_metadata(job.id, job.payload)
                if resume is None:
                    queue.transition(job.id, JobState.FAILED, now=config.now)
                    failed_jobs.append(
                        {"job_id": job.id, "reason": "checkpoint_manifest_mismatch"}
                    )
                    continue
                resume_jobs.append(resume)
            queue.transition(job.id, JobState.RUNNING, now=config.now)
            queue.transition(job.id, JobState.EVALUATING, now=config.now)
            queue.transition(job.id, JobState.WAITING_REVIEW, now=config.now)
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


def _resume_metadata(job_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    checkpoint_path = Path(str(payload["checkpoint_path"]))
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    manifest_hash = str(payload["manifest_hash"])
    if str(checkpoint.get("manifest_hash")) != manifest_hash:
        return None
    return {
        "job_id": job_id,
        "run_id": str(payload["run_id"]),
        "checkpoint_path": str(checkpoint_path),
        "manifest_hash": manifest_hash,
    }


def _queued_count(queue: DaemonQueue) -> int:
    return int(
        queue.connection.execute(
            "SELECT COUNT(*) FROM daemon_jobs WHERE state = ?",
            (JobState.QUEUED.value,),
        ).fetchone()[0]
    )


def _load_discovered_traces(repo: Path) -> set[str]:
    path = repo / ".sidecar" / "discovered-traces.json"
    if not path.is_file():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("discovered trace registry must be a JSON list")
    return {str(item) for item in payload}


def _write_discovered_traces(repo: Path, traces: set[str]) -> None:
    path = repo / ".sidecar" / "discovered-traces.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(traces), indent=2) + "\n", encoding="utf-8")
