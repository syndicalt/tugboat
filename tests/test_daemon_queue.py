from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tugboat.daemon.queue import (
    DaemonQueue,
    FileKillSwitch,
    JobState,
    QueuePayloadError,
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


def test_sidecar_queue_database_is_private_under_permissive_umask(tmp_path: Path):
    previous_umask = os.umask(0o022)
    try:
        with DaemonQueue.open_sidecar(tmp_path) as queue:
            queue.enqueue(kind="trace_audit", payload={"trace_id": "trace-1"}, now=_at(0))
    finally:
        os.umask(previous_umask)

    sidecar = tmp_path / ".sidecar"
    queue_path = sidecar / "daemon.sqlite"
    assert sidecar.stat().st_mode & 0o777 == 0o700
    assert queue_path.stat().st_mode & 0o777 == 0o600


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


def test_get_job_raises_typed_payload_error_for_corrupt_payload_json(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.connection.execute(
            """
            INSERT INTO daemon_jobs(
              kind, payload_json, state, attempts, lease_owner, lease_expires_at,
              created_at, updated_at
            )
            VALUES (?, ?, ?, 0, NULL, NULL, ?, ?)
            """,
            (
                "trace_audit",
                "{not-json",
                JobState.QUEUED.value,
                _at(0).isoformat(timespec="microseconds"),
                _at(0).isoformat(timespec="microseconds"),
            ),
        )
        queue.connection.commit()

        with pytest.raises(QueuePayloadError) as exc_info:
            queue.get_job(1)

    assert exc_info.value.job_id == 1


def test_enqueue_rejects_non_object_payload_without_persisting_row(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        with pytest.raises(ValueError, match="payload must be a JSON object"):
            queue.enqueue(
                kind="trace_audit",
                payload=[],  # type: ignore[arg-type]
                now=_at(0),
            )

        count = queue.connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0]

    assert count == 0


def test_get_job_raises_typed_payload_error_for_non_object_payload_json(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.connection.execute(
            """
            INSERT INTO daemon_jobs(
              kind, payload_json, state, attempts, lease_owner, lease_expires_at,
              created_at, updated_at
            )
            VALUES (?, ?, ?, 0, NULL, NULL, ?, ?)
            """,
            (
                "trace_audit",
                "[]",
                JobState.QUEUED.value,
                _at(0).isoformat(timespec="microseconds"),
                _at(0).isoformat(timespec="microseconds"),
            ),
        )
        queue.connection.commit()

        with pytest.raises(QueuePayloadError) as exc_info:
            queue.get_job(1)

    assert exc_info.value.job_id == 1


def test_acquire_fails_non_object_payload_before_leasing(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.connection.execute(
            """
            INSERT INTO daemon_jobs(
              kind, payload_json, state, attempts, lease_owner, lease_expires_at,
              created_at, updated_at
            )
            VALUES (?, ?, ?, 0, NULL, NULL, ?, ?)
            """,
            (
                "trace_audit",
                "[]",
                JobState.QUEUED.value,
                _at(0).isoformat(timespec="microseconds"),
                _at(0).isoformat(timespec="microseconds"),
            ),
        )
        queue.connection.commit()

        with pytest.raises(QueuePayloadError) as exc_info:
            queue.acquire_next(
                lease_owner="worker-a",
                now=_at(10),
                lease_duration=timedelta(seconds=30),
            )

        row = queue.connection.execute(
            "SELECT state, attempts, lease_owner, lease_expires_at FROM daemon_jobs WHERE id = 1"
        ).fetchone()

    assert exc_info.value.job_id == 1
    assert tuple(row) == (JobState.FAILED.value, 0, None, None)


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


def test_acquire_next_is_atomic_across_two_worker_connections(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as setup_queue:
        job = setup_queue.enqueue(kind="inspect", payload={}, now=_at(0))

    selected = threading.Event()
    release = threading.Event()
    worker_b_started = threading.Event()
    results: dict[str, Any] = {}

    worker_a_connection = _BlockingSelectConnection(
        sqlite3.connect(tmp_path / ".sidecar" / "daemon.sqlite", check_same_thread=False),
        selected=selected,
        release=release,
    )
    worker_a_connection.row_factory = sqlite3.Row
    worker_b_connection = sqlite3.connect(
        tmp_path / ".sidecar" / "daemon.sqlite",
        check_same_thread=False,
    )
    worker_b_connection.row_factory = sqlite3.Row

    worker_a = DaemonQueue(tmp_path / ".sidecar" / "daemon.sqlite", worker_a_connection)
    worker_b = DaemonQueue(tmp_path / ".sidecar" / "daemon.sqlite", worker_b_connection)

    def acquire_a() -> None:
        try:
            results["a"] = worker_a.acquire_next(
                lease_owner="worker-a",
                now=_at(10),
                lease_duration=timedelta(seconds=30),
            )
        except BaseException as error:  # pragma: no cover - re-raised below
            results["a_error"] = error

    def acquire_b() -> None:
        worker_b_started.set()
        try:
            results["b"] = worker_b.acquire_next(
                lease_owner="worker-b",
                now=_at(10),
                lease_duration=timedelta(seconds=30),
            )
        except BaseException as error:  # pragma: no cover - re-raised below
            results["b_error"] = error

    thread_a = threading.Thread(target=acquire_a)
    thread_a.start()
    assert selected.wait(timeout=2)

    thread_b = threading.Thread(target=acquire_b)
    thread_b.start()
    assert worker_b_started.wait(timeout=2)
    release.set()
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    worker_a.close()
    worker_b.close()

    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    if "a_error" in results:
        raise results["a_error"]
    if "b_error" in results:
        raise results["b_error"]

    acquired = [item for item in (results["a"], results["b"]) if item is not None]
    assert len(acquired) == 1
    assert acquired[0].id == job.id

    with DaemonQueue.open_sidecar(tmp_path) as queue:
        stored = queue.get_job(job.id)

    assert stored is not None
    assert stored.attempts == 1
    assert stored.lease_owner == acquired[0].lease_owner


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


class _BlockingSelectConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        selected: threading.Event,
        release: threading.Event,
    ):
        self._connection = connection
        self._selected = selected
        self._release = release

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
        cursor = self._connection.execute(sql, parameters)
        normalized = " ".join(sql.split()).lower()
        if normalized.startswith("select * from daemon_jobs where state = ?"):
            self._selected.set()
            assert self._release.wait(timeout=2)
        return cursor

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()

    @property
    def row_factory(self) -> object:
        return self._connection.row_factory

    @row_factory.setter
    def row_factory(self, value: object) -> None:
        self._connection.row_factory = value

    def __enter__(self) -> "_BlockingSelectConnection":
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._connection.__exit__(exc_type, exc, traceback)
