from __future__ import annotations

import ipaddress
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


SCHEMA = """
CREATE TABLE IF NOT EXISTS daemon_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_daemon_jobs_acquire
ON daemon_jobs(state, lease_expires_at, id);
"""


class JobState(str, Enum):
    QUEUED = "queued"
    INSPECTING = "inspecting"
    RUNNING = "running"
    EVALUATING = "evaluating"
    WAITING_REVIEW = "waiting_review"
    REJECTED = "rejected"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


LEASED_STATES = frozenset(
    {
        JobState.INSPECTING,
        JobState.RUNNING,
        JobState.EVALUATING,
    }
)

FINAL_STATES = frozenset(
    {
        JobState.REJECTED,
        JobState.APPLIED,
        JobState.ROLLED_BACK,
        JobState.FAILED,
    }
)

ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.INSPECTING, JobState.FAILED}),
    JobState.INSPECTING: frozenset(
        {JobState.RUNNING, JobState.QUEUED, JobState.REJECTED, JobState.FAILED}
    ),
    JobState.RUNNING: frozenset({JobState.EVALUATING, JobState.FAILED}),
    JobState.EVALUATING: frozenset(
        {
            JobState.WAITING_REVIEW,
            JobState.REJECTED,
            JobState.APPLIED,
            JobState.FAILED,
        }
    ),
    JobState.WAITING_REVIEW: frozenset({JobState.REJECTED, JobState.APPLIED}),
    JobState.APPLIED: frozenset({JobState.ROLLED_BACK}),
    JobState.REJECTED: frozenset(),
    JobState.ROLLED_BACK: frozenset(),
    JobState.FAILED: frozenset(),
}


@dataclass(frozen=True)
class DaemonJob:
    id: int
    kind: str
    payload: dict[str, Any]
    state: JobState
    attempts: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class QueueStateError(RuntimeError):
    pass


class QueuePayloadError(RuntimeError):
    def __init__(self, job_id: int):
        super().__init__(f"invalid daemon job payload: {job_id}")
        self.job_id = job_id


class KillSwitch(Protocol):
    def is_enabled(self) -> bool:
        pass


@dataclass(frozen=True)
class FileKillSwitch:
    path: Path

    def is_enabled(self) -> bool:
        if not self.path.exists():
            return False
        value = self.path.read_text(encoding="utf-8").strip().lower()
        return value in {"1", "enabled", "on", "true", "yes"}


class DaemonQueue:
    def __init__(self, path: Path, connection: sqlite3.Connection):
        self.path = path
        self.connection = connection

    def __enter__(self) -> "DaemonQueue":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @classmethod
    def open_sidecar(cls, root: Path) -> "DaemonQueue":
        return cls.open(root / ".sidecar" / "daemon.sqlite")

    @classmethod
    def open(cls, path: Path) -> "DaemonQueue":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        connection.commit()
        return cls(path, connection)

    def close(self) -> None:
        self.connection.close()

    def enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> DaemonJob:
        return self._enqueue(kind=kind, payload=payload, now=now, commit=True)

    def enqueue_uncommitted(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        now: datetime | None = None,
    ) -> DaemonJob:
        return self._enqueue(kind=kind, payload=payload, now=now, commit=False)

    def _enqueue(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        now: datetime | None,
        commit: bool,
    ) -> DaemonJob:
        if not isinstance(payload, dict):
            raise ValueError("daemon job payload must be a JSON object")
        timestamp = _serialize_datetime(_coerce_datetime(now))
        cursor = self.connection.execute(
            """
            INSERT INTO daemon_jobs(
              kind, payload_json, state, attempts, lease_owner, lease_expires_at,
              created_at, updated_at
            )
            VALUES (?, ?, ?, 0, NULL, NULL, ?, ?)
            """,
            (
                kind,
                json.dumps(payload, sort_keys=True),
                JobState.QUEUED.value,
                timestamp,
                timestamp,
            ),
        )
        if commit:
            self.connection.commit()
        return self._require_job(int(cursor.lastrowid))

    def get_job(self, job_id: int) -> DaemonJob | None:
        row = self.connection.execute(
            "SELECT * FROM daemon_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return _job_from_row(row) if row is not None else None

    def acquire_next(
        self,
        *,
        lease_owner: str,
        lease_duration: timedelta,
        now: datetime | None = None,
        kill_switch: KillSwitch | None = None,
    ) -> DaemonJob | None:
        if kill_switch is not None and kill_switch.is_enabled():
            return None

        timestamp = _coerce_datetime(now)
        lease_expires_at = timestamp + lease_duration
        timestamp_text = _serialize_datetime(timestamp)
        lease_expires_at_text = _serialize_datetime(lease_expires_at)

        with self.connection:
            row = self.connection.execute(
                """
                SELECT * FROM daemon_jobs
                WHERE
                  state = ?
                  OR (
                    state IN (?, ?, ?)
                    AND lease_expires_at IS NOT NULL
                    AND lease_expires_at <= ?
                  )
                ORDER BY id
                LIMIT 1
                """,
                (
                    JobState.QUEUED.value,
                    JobState.INSPECTING.value,
                    JobState.RUNNING.value,
                    JobState.EVALUATING.value,
                    timestamp_text,
                ),
            ).fetchone()
            if row is None:
                return None
            job_id = int(row["id"])
            if not _payload_is_valid_object(str(row["payload_json"])):
                self.connection.execute(
                    """
                    UPDATE daemon_jobs
                    SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (JobState.FAILED.value, timestamp_text, job_id),
                )
                self.connection.commit()
                raise QueuePayloadError(job_id)

            self.connection.execute(
                """
                UPDATE daemon_jobs
                SET state = ?, attempts = attempts + 1, lease_owner = ?,
                    lease_expires_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    JobState.INSPECTING.value,
                    lease_owner,
                    lease_expires_at_text,
                    timestamp_text,
                    job_id,
                ),
            )

        return self._require_job(job_id)

    def transition(
        self,
        job_id: int,
        target_state: JobState,
        *,
        now: datetime | None = None,
    ) -> DaemonJob:
        current = self._require_job(job_id)
        target_state = JobState(target_state)
        if target_state not in ALLOWED_TRANSITIONS[current.state]:
            raise QueueStateError(
                f"invalid daemon job transition: "
                f"{current.state.value} -> {target_state.value}"
            )

        lease_owner = current.lease_owner
        lease_expires_at = (
            _serialize_datetime(current.lease_expires_at)
            if current.lease_expires_at is not None
            else None
        )
        if target_state in FINAL_STATES or target_state in {
            JobState.QUEUED,
            JobState.WAITING_REVIEW,
        }:
            lease_owner = None
            lease_expires_at = None

        self.connection.execute(
            """
            UPDATE daemon_jobs
            SET state = ?, lease_owner = ?, lease_expires_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                target_state.value,
                lease_owner,
                lease_expires_at,
                _serialize_datetime(_coerce_datetime(now)),
                job_id,
            ),
        )
        self.connection.commit()
        return self._require_job(job_id)

    def mark_stale_leases(
        self,
        *,
        now: datetime | None = None,
        max_attempts: int,
        fail_job_ids: tuple[int, ...] = (),
    ) -> tuple[int, ...]:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        timestamp = _serialize_datetime(_coerce_datetime(now))
        fail_ids = set(fail_job_ids)
        recovered: list[int] = []
        with self.connection:
            rows = self.connection.execute(
                """
                SELECT * FROM daemon_jobs
                WHERE state IN (?, ?, ?)
                  AND lease_expires_at IS NOT NULL
                  AND lease_expires_at <= ?
                ORDER BY id
                """,
                (
                    JobState.INSPECTING.value,
                    JobState.RUNNING.value,
                    JobState.EVALUATING.value,
                    timestamp,
                ),
            ).fetchall()
            for row in rows:
                job_id = int(row["id"])
                attempts = int(row["attempts"])
                state = (
                    JobState.FAILED
                    if attempts >= max_attempts or job_id in fail_ids
                    else JobState.QUEUED
                )
                self.connection.execute(
                    """
                    UPDATE daemon_jobs
                    SET state = ?, lease_owner = NULL, lease_expires_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (state.value, timestamp, job_id),
                )
                recovered.append(job_id)
        return tuple(recovered)

    def _require_job(self, job_id: int) -> DaemonJob:
        job = self.get_job(job_id)
        if job is None:
            raise KeyError(f"unknown daemon job: {job_id}")
        return job


def validate_local_bind_address(address: str) -> str:
    if not address:
        raise ValueError("daemon bind address must be local-only")
    if address.startswith("unix:"):
        if address.removeprefix("unix:"):
            return address
        raise ValueError("daemon bind address must be local-only")
    if address.startswith("/"):
        return address

    host = _host_from_bind_address(address)
    if host == "localhost":
        return address

    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("daemon bind address must be local-only") from exc

    if ip.is_loopback:
        return address
    raise ValueError("daemon bind address must be local-only")


def _host_from_bind_address(address: str) -> str:
    if address.startswith("["):
        host, separator, _port = address[1:].partition("]")
        if not separator:
            raise ValueError("daemon bind address must be local-only")
        return host

    host, separator, port = address.partition(":")
    if separator and (not host or not port):
        raise ValueError("daemon bind address must be local-only")
    return host


def _job_from_row(row: sqlite3.Row) -> DaemonJob:
    job_id = int(row["id"])
    return DaemonJob(
        id=job_id,
        kind=str(row["kind"]),
        payload=_decode_payload_json(job_id, str(row["payload_json"])),
        state=JobState(str(row["state"])),
        attempts=int(row["attempts"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=_parse_optional_datetime(row["lease_expires_at"]),
        created_at=_parse_datetime(str(row["created_at"])),
        updated_at=_parse_datetime(str(row["updated_at"])),
    )


def _decode_payload_json(job_id: int, payload_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as exc:
        raise QueuePayloadError(job_id) from exc
    if not isinstance(payload, dict):
        raise QueuePayloadError(job_id)
    return payload


def _payload_is_valid_object(payload_json: str) -> bool:
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict)


def _coerce_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _serialize_datetime(value: datetime) -> str:
    return _coerce_datetime(value).isoformat(timespec="microseconds")


def _parse_optional_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)
