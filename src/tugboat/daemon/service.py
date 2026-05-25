from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tugboat.daemon.queue import DaemonQueue, FileKillSwitch, JobState, KillSwitch


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
        final_job = _process_job(queue, job.id, now=config.now)
        return {
            "processed": True,
            "job_id": final_job.id,
            "final_state": final_job.state.value,
            "recovered_jobs": list(recovered),
        }
    finally:
        queue.close()


def default_kill_switch(repo: Path) -> FileKillSwitch:
    return FileKillSwitch(repo / ".sidecar" / "read-only.kill")


def _process_job(queue: DaemonQueue, job_id: int, *, now: datetime | None) -> Any:
    running = queue.transition(job_id, JobState.RUNNING, now=now)
    evaluating = queue.transition(running.id, JobState.EVALUATING, now=now)
    return queue.transition(evaluating.id, JobState.WAITING_REVIEW, now=now)
