from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tugboat.daemon.queue import (
    DaemonQueue,
    FileKillSwitch,
    JobState,
    QueueStateError,
    validate_local_bind_address,
)


def test_sidecar_queue_initializes_and_enqueues_jobs(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.enqueue(
            kind="patch-proposal",
            payload={"run_id": "run-1", "candidate_id": 7},
            now=_at(0),
        )

        assert queue.path == tmp_path / ".sidecar" / "daemon.sqlite"
        assert queue.path.exists()
        assert job.state is JobState.QUEUED
        assert job.payload == {"run_id": "run-1", "candidate_id": 7}

        loaded = queue.get_job(job.id)
        assert loaded is not None
        assert loaded.kind == "patch-proposal"
        assert loaded.state is JobState.QUEUED


def test_acquire_leases_next_queued_job(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        first = queue.enqueue(kind="inspect", payload={"n": 1}, now=_at(0))
        queue.enqueue(kind="inspect", payload={"n": 2}, now=_at(1))

        acquired = queue.acquire_next(
            lease_owner="worker-a",
            now=_at(10),
            lease_duration=timedelta(seconds=30),
        )

        assert acquired is not None
        assert acquired.id == first.id
        assert acquired.state is JobState.INSPECTING
        assert acquired.lease_owner == "worker-a"
        assert acquired.lease_expires_at == _at(40)
        assert acquired.attempts == 1


def test_acquire_returns_none_and_does_not_mutate_when_kill_switch_enabled(
    tmp_path: Path,
):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.enqueue(kind="inspect", payload={}, now=_at(0))
        kill_switch_path = tmp_path / "kill-switch"
        kill_switch_path.write_text("enabled\n", encoding="utf-8")

        acquired = queue.acquire_next(
            lease_owner="worker-a",
            now=_at(10),
            lease_duration=timedelta(seconds=30),
            kill_switch=FileKillSwitch(kill_switch_path),
        )

        assert acquired is None
        loaded = queue.get_job(job.id)
        assert loaded is not None
        assert loaded.state is JobState.QUEUED
        assert loaded.lease_owner is None
        assert loaded.lease_expires_at is None
        assert loaded.attempts == 0


def test_acquire_can_reclaim_expired_active_lease(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.enqueue(kind="inspect", payload={}, now=_at(0))
        queue.acquire_next(
            lease_owner="worker-a",
            now=_at(10),
            lease_duration=timedelta(seconds=5),
        )

        acquired = queue.acquire_next(
            lease_owner="worker-b",
            now=_at(20),
            lease_duration=timedelta(seconds=10),
        )

        assert acquired is not None
        assert acquired.id == job.id
        assert acquired.state is JobState.INSPECTING
        assert acquired.lease_owner == "worker-b"
        assert acquired.lease_expires_at == _at(30)
        assert acquired.attempts == 2


def test_transition_enforces_allowed_state_machine(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.enqueue(kind="inspect", payload={}, now=_at(0))

        with pytest.raises(QueueStateError, match="queued -> applied"):
            queue.transition(job.id, JobState.APPLIED, now=_at(1))

        queue.transition(job.id, JobState.INSPECTING, now=_at(2))
        queue.transition(job.id, JobState.RUNNING, now=_at(3))
        queue.transition(job.id, JobState.EVALUATING, now=_at(4))
        queue.transition(job.id, JobState.WAITING_REVIEW, now=_at(5))
        updated = queue.transition(job.id, JobState.APPLIED, now=_at(6))

        assert updated.state is JobState.APPLIED
        assert queue.transition(job.id, JobState.ROLLED_BACK, now=_at(7)).state is (
            JobState.ROLLED_BACK
        )


def test_mark_stale_leases_requeues_or_fails_deterministically(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        requeue_job = queue.enqueue(kind="inspect", payload={}, now=_at(0))
        fail_job = queue.enqueue(kind="inspect", payload={}, now=_at(1))

        queue.acquire_next(
            lease_owner="worker-a",
            now=_at(10),
            lease_duration=timedelta(seconds=5),
        )
        queue.acquire_next(
            lease_owner="worker-b",
            now=_at(11),
            lease_duration=timedelta(seconds=5),
        )

        recovered = queue.mark_stale_leases(
            now=_at(20),
            max_attempts=2,
            fail_job_ids=(fail_job.id,),
        )

        assert recovered == (requeue_job.id, fail_job.id)
        assert queue.get_job(requeue_job.id).state is JobState.QUEUED  # type: ignore[union-attr]
        assert queue.get_job(fail_job.id).state is JobState.FAILED  # type: ignore[union-attr]


def test_job_state_machine_declares_phase_8_states():
    assert {state.value for state in JobState} == {
        "queued",
        "inspecting",
        "running",
        "evaluating",
        "waiting_review",
        "rejected",
        "applied",
        "rolled_back",
        "failed",
    }


@pytest.mark.parametrize(
    "address",
    (
        "localhost",
        "127.0.0.1",
        "127.0.0.1:8765",
        "unix:/tmp/tugboat-daemon.sock",
        "/tmp/tugboat-daemon.sock",
    ),
)
def test_bind_address_validation_accepts_local_only_addresses(address: str):
    assert validate_local_bind_address(address) == address


@pytest.mark.parametrize("address", ("0.0.0.0", "0.0.0.0:8765", "8.8.8.8"))
def test_bind_address_validation_rejects_public_listeners(address: str):
    with pytest.raises(ValueError, match="local-only"):
        validate_local_bind_address(address)


def _at(seconds: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, seconds, tzinfo=timezone.utc)
