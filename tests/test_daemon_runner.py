from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tugboat.daemon.queue import DaemonQueue, FileKillSwitch, JobState
from tugboat.daemon import run_daemon_loop as exported_run_daemon_loop
from tugboat.daemon.runner import (
    DaemonLoopConfig,
    discover_trace_jobs,
    run_daemon_cycle,
    run_daemon_loop,
    write_worktree_profile,
)


def _write_fake_llmff(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:3] == ["inspect", "--format", "json"]:
    print(json.dumps({"manifest": Path(args[3]).stem, "network_required": False}))
    raise SystemExit(0)

if args[:1] == ["run"]:
    manifest = Path(args[1]).stem
    trace = Path(args[args.index("--trace") + 1])
    events = Path(args[args.index("--events") + 1])
    checkpoint = Path(args[args.index("--checkpoint") + 1])
    outputs = {}
    index = 0
    while index < len(args):
        if args[index] == "--output":
            outputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        index += 1
    trace.write_text('{"event":"step","name":"' + manifest + '"}\\n', encoding="utf-8")
    events.write_text('{"event":"run_completed"}\\n', encoding="utf-8")
    checkpoint.write_text('{"manifest_hash":"fake"}\\n', encoding="utf-8")
    if manifest == "instruction-index":
        outputs["instruction_index"].write_text(json.dumps({
            "documents": [{"path": "CODEX.md", "obligations": ["Use tests."]}]
        }) + "\\n", encoding="utf-8")
    elif manifest == "episode-audit":
        outputs["audit_report"].write_text(json.dumps({
            "edit_warranted": True,
            "failure_class": "cycle_instruction_conflict",
            "severity": "high",
            "confidence": 0.92,
            "evidence_refs": ["ev_cycle"],
        }) + "\\n", encoding="utf-8")
        outputs["evidence_ids"].write_text(json.dumps({
            "evidence_ids": ["ev_cycle"],
        }) + "\\n", encoding="utf-8")
    raise SystemExit(0)

raise SystemExit(64)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_discover_trace_jobs_enqueues_new_jsonl_traces_once(tmp_path: Path):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    (trace_dir / "episode.jsonl").write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    first = discover_trace_jobs(tmp_path, [trace_dir], now=_at(0))
    second = discover_trace_jobs(tmp_path, [trace_dir], now=_at(1))

    assert first == {"discovered": 1, "skipped": 0}
    assert second == {"discovered": 0, "skipped": 1}
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.get_job(1)
        assert job is not None
        assert job.kind == "trace_audit"
        assert job.payload["trace_path"] == str(trace_dir / "episode.jsonl")
    with closing(sqlite3.connect(tmp_path / ".sidecar" / "db.sqlite")) as connection:
        ledger_job = connection.execute(
            """
            SELECT job_id, repo_path, state, payload_json, audit_event_sequence
            FROM daemon_jobs
            """
        ).fetchone()
        event_type = connection.execute(
            "SELECT event_type FROM audit_events WHERE sequence = ?",
            (ledger_job[4],),
        ).fetchone()[0]

    assert ledger_job[:4] == (
        "1",
        str(tmp_path),
        "queued",
        json.dumps({"trace_path": str(trace_dir / "episode.jsonl")}, sort_keys=True),
    )
    assert ledger_job[4] is not None
    assert event_type == "daemon_job.recorded"


def test_discover_trace_jobs_skips_secret_bearing_traces_without_queueing(
    tmp_path: Path,
):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    trace = trace_dir / "episode.jsonl"
    trace.write_text(
        '{"type":"tool","content":"sk-thissecretkeyvalue1234567890"}\n',
        encoding="utf-8",
    )

    result = discover_trace_jobs(tmp_path, [trace_dir], now=_at(0))

    assert result == {"discovered": 0, "skipped": 1}
    assert json.loads(
        (tmp_path / ".sidecar" / "discovered-traces.json").read_text(encoding="utf-8")
    ) == {"schema_version": 1, "traces": []}
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0] == 0


def test_discover_trace_jobs_writes_schema_versioned_registry(tmp_path: Path):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    trace = trace_dir / "episode.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    result = discover_trace_jobs(tmp_path, [trace_dir], now=_at(0))

    assert result == {"discovered": 1, "skipped": 0}
    assert json.loads(
        (tmp_path / ".sidecar" / "discovered-traces.json").read_text(encoding="utf-8")
    ) == {"schema_version": 1, "traces": [str(trace.resolve())]}


def test_discover_trace_jobs_deduplicates_overlapping_trace_dirs_in_one_pass(
    tmp_path: Path,
):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    trace = trace_dir / "episode.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    result = discover_trace_jobs(tmp_path, [trace_dir, trace_dir], now=_at(0))

    assert result == {"discovered": 1, "skipped": 1}
    assert json.loads(
        (tmp_path / ".sidecar" / "discovered-traces.json").read_text(encoding="utf-8")
    ) == {"schema_version": 1, "traces": [str(trace.resolve())]}
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0] == 1


def test_discover_trace_jobs_validates_registry_before_queue_visibility(
    tmp_path: Path,
    monkeypatch,
):
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    (trace_dir / "episode.jsonl").write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    def fail_discovered_traces_artifact(name, payload):
        if name == "daemon-discovered-traces.json":
            raise ValueError("invalid discovered traces registry")

    monkeypatch.setattr(
        "tugboat.daemon.runner.validate_json_artifact",
        fail_discovered_traces_artifact,
    )

    with pytest.raises(ValueError, match="invalid discovered traces registry"):
        discover_trace_jobs(tmp_path, [trace_dir], now=_at(0))

    assert not (tmp_path / ".sidecar" / "discovered-traces.json").exists()
    assert not (tmp_path / ".sidecar" / "daemon.sqlite").exists()


def test_run_daemon_cycle_watches_configured_trace_dirs_without_duplicate_enqueue(
    tmp_path: Path,
):
    trace_dir = tmp_path / "configured-traces"
    trace_dir.mkdir()
    (trace_dir / "episode.jsonl").write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    first = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=0,
            concurrency_limit=0,
            lease_duration=timedelta(seconds=30),
            trace_dirs=(trace_dir,),
            now=_at(0),
        ),
    )
    second = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=0,
            concurrency_limit=0,
            lease_duration=timedelta(seconds=30),
            trace_dirs=(trace_dir,),
            now=_at(1),
        ),
    )

    assert first["trace_discovery"] == {"discovered": 1, "skipped": 0}
    assert second["trace_discovery"] == {"discovered": 0, "skipped": 1}
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        rows = queue.connection.execute(
            "SELECT kind, payload_json FROM daemon_jobs ORDER BY id"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == "trace_audit"
    assert json.loads(rows[0]["payload_json"]) == {
        "trace_path": str(trace_dir / "episode.jsonl")
    }


def test_run_daemon_cycle_read_only_kill_switch_blocks_trace_discovery_writes(
    tmp_path: Path,
):
    trace_dir = tmp_path / "configured-traces"
    trace_dir.mkdir()
    trace = trace_dir / "episode.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    kill_switch_path = tmp_path / ".sidecar" / "read-only.kill"
    kill_switch_path.parent.mkdir()
    kill_switch_path.write_text("enabled\n", encoding="utf-8")

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            trace_dirs=(trace_dir,),
            kill_switch=FileKillSwitch(kill_switch_path),
            now=_at(0),
        ),
    )

    assert result == {
        "processed_jobs": [],
        "failed_jobs": [],
        "resume_jobs": [],
        "recovered_jobs": [],
        "trace_discovery": {"discovered": 0, "skipped": 0},
        "rate_limited": False,
        "concurrency_limited": False,
    }
    assert not (tmp_path / ".sidecar" / "discovered-traces.json").exists()
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0] == 0


def test_run_daemon_cycle_recovers_corrupt_discovered_trace_registry(
    tmp_path: Path,
):
    trace_dir = tmp_path / "configured-traces"
    trace_dir.mkdir()
    trace = trace_dir / "episode.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    registry = tmp_path / ".sidecar" / "discovered-traces.json"
    registry.parent.mkdir()
    registry.write_text('{"not":"a-list"}\n', encoding="utf-8")

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=0,
            concurrency_limit=0,
            lease_duration=timedelta(seconds=30),
            trace_dirs=(trace_dir,),
            now=_at(0),
        ),
    )

    assert result["trace_discovery"] == {"discovered": 1, "skipped": 0}
    assert json.loads(registry.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "traces": [str(trace.resolve())],
    }
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.get_job(1)
        assert job is not None
        assert job.kind == "trace_audit"
        assert job.payload == {"trace_path": str(trace)}


def test_run_daemon_cycle_updates_main_store_job_state_for_discovered_trace(
    tmp_path: Path,
):
    (tmp_path / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff")
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )
    trace_dir = tmp_path / "configured-traces"
    trace_dir.mkdir()
    (trace_dir / "episode.jsonl").write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            trace_dirs=(trace_dir,),
            now=_at(0),
        ),
    )

    assert result["processed_jobs"] == [1]
    with closing(sqlite3.connect(tmp_path / ".sidecar" / "db.sqlite")) as connection:
        ledger_job = connection.execute(
            """
            SELECT job_id, state
            FROM daemon_jobs
            """
        ).fetchone()
        transition_events = connection.execute(
            """
            SELECT event_type, json_extract(payload_json, '$.state')
            FROM audit_events
            WHERE event_type = 'daemon_job.state_changed'
            ORDER BY sequence
            """
        ).fetchall()
        llmff_jobs = connection.execute(
            """
            SELECT manifest_name, status
            FROM llmff_jobs
            ORDER BY id
            """
        ).fetchall()

    assert ledger_job == ("1", "waiting_review")
    run_dir = sorted((tmp_path / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "cycle_instruction_conflict"
    assert audit["evidence_refs"] == ["ev_cycle"]
    assert (run_dir / "audit.raw.json").exists()
    assert llmff_jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
    ]
    assert transition_events == [
        ("daemon_job.state_changed", "inspecting"),
        ("daemon_job.state_changed", "running"),
        ("daemon_job.state_changed", "evaluating"),
        ("daemon_job.state_changed", "waiting_review"),
    ]


def test_run_daemon_cycle_applies_rate_and_concurrency_limits(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        for number in range(3):
            queue.enqueue(kind="audit", payload={"n": number}, now=_at(number))

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=2,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["processed_jobs"] == [1]
    assert result["rate_limited"] is True
    assert result["concurrency_limited"] is True
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.WAITING_REVIEW  # type: ignore[union-attr]
        assert queue.get_job(2).state is JobState.QUEUED  # type: ignore[union-attr]


def test_run_daemon_loop_runs_bounded_cycles_and_sleeps_between_cycles(tmp_path: Path):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(kind="audit", payload={"n": 1}, now=_at(0))
        queue.enqueue(kind="audit", payload={"n": 2}, now=_at(1))
    sleeps: list[float] = []

    result = run_daemon_loop(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
        cycles=2,
        interval_seconds=0.25,
        sleep=sleeps.append,
    )

    assert result["cycle_count"] == 2
    assert [cycle["processed_jobs"] for cycle in result["cycles"]] == [[1], [2]]
    assert sleeps == [0.25]
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.WAITING_REVIEW  # type: ignore[union-attr]
        assert queue.get_job(2).state is JobState.WAITING_REVIEW  # type: ignore[union-attr]


def test_daemon_package_exports_bounded_loop_runner():
    assert exported_run_daemon_loop is run_daemon_loop


def test_run_daemon_loop_rejects_invalid_bounds(tmp_path: Path):
    config = DaemonLoopConfig(
        worker_id="worker-a",
        max_jobs_per_cycle=1,
        concurrency_limit=1,
        lease_duration=timedelta(seconds=30),
        now=_at(10),
    )

    try:
        run_daemon_loop(tmp_path, config, cycles=0)
    except ValueError as error:
        assert str(error) == "cycles must be at least 1"
    else:
        raise AssertionError("cycles=0 should fail")

    try:
        run_daemon_loop(tmp_path, config, cycles=1, interval_seconds=-0.1)
    except ValueError as error:
        assert str(error) == "interval_seconds must be non-negative"
    else:
        raise AssertionError("negative interval should fail")


def test_run_daemon_cycle_fails_corrupt_queue_payload_without_crashing(
    tmp_path: Path,
):
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

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["processed_jobs"] == []
    assert result["failed_jobs"] == [{"job_id": 1, "reason": "queue_payload_invalid"}]
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        row = queue.connection.execute(
            "SELECT state, attempts, lease_owner, lease_expires_at FROM daemon_jobs WHERE id = 1"
        ).fetchone()
    assert tuple(row) == (JobState.FAILED.value, 0, None, None)


def test_run_daemon_cycle_leases_checkpoint_resume_when_manifest_hash_matches(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    checkpoint = run_dir / "checkpoint.json"
    checkpoint.write_text(json.dumps({"manifest_hash": "abc123"}) + "\n", encoding="utf-8")
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(
            kind="llmff_resume",
            payload={
                "run_id": "run-1",
                "manifest_hash": "abc123",
                "checkpoint_path": str(checkpoint),
            },
            now=_at(0),
        )

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["resume_jobs"] == [
        {
            "job_id": 1,
            "run_id": "run-1",
            "checkpoint_path": str(checkpoint),
            "manifest_hash": "abc123",
        }
    ]
    assert result["failed_jobs"] == []
    assert result["processed_jobs"] == []
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        job = queue.get_job(1)
    assert job is not None
    assert job.state is JobState.INSPECTING
    assert job.lease_owner == "worker-a"
    assert job.lease_expires_at == _at(40)
    with closing(sqlite3.connect(tmp_path / ".sidecar" / "db.sqlite")) as connection:
        event = connection.execute(
            """
            SELECT payload_json
            FROM audit_events
            WHERE event_type = 'daemon_job.resume_ready'
            """
        ).fetchone()

    assert event is not None
    assert json.loads(event[0]) == {
        "checkpoint_path": ".sidecar/runs/run-1/checkpoint.json",
        "job_id": "1",
        "manifest_hash": "abc123",
        "repo": str(tmp_path),
        "run_id": "run-1",
    }


def test_run_daemon_cycle_fails_checkpoint_resume_on_manifest_mismatch(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    checkpoint = run_dir / "checkpoint.json"
    checkpoint.write_text(json.dumps({"manifest_hash": "old"}) + "\n", encoding="utf-8")
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(
            kind="llmff_resume",
            payload={
                "run_id": "run-1",
                "manifest_hash": "new",
                "checkpoint_path": str(checkpoint),
            },
            now=_at(0),
        )

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["failed_jobs"] == [{"job_id": 1, "reason": "checkpoint_manifest_mismatch"}]
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.FAILED  # type: ignore[union-attr]


def test_run_daemon_cycle_fails_checkpoint_resume_outside_run_dir_without_reading(
    tmp_path: Path,
):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    outside_checkpoint = tmp_path / ".sidecar" / "checkpoint.json"
    outside_checkpoint.parent.mkdir(parents=True, exist_ok=True)
    outside_checkpoint.write_text(json.dumps({"manifest_hash": "abc123"}) + "\n", encoding="utf-8")
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(
            kind="llmff_resume",
            payload={
                "run_id": "run-1",
                "manifest_hash": "abc123",
                "checkpoint_path": str(outside_checkpoint),
            },
            now=_at(0),
        )

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["failed_jobs"] == [{"job_id": 1, "reason": "checkpoint_path_outside_run"}]
    assert result["resume_jobs"] == []
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.FAILED  # type: ignore[union-attr]


def test_run_daemon_cycle_fails_checkpoint_resume_with_traversal_run_id(tmp_path: Path):
    escaped_dir = tmp_path / ".sidecar" / "escape"
    escaped_dir.mkdir(parents=True)
    escaped_checkpoint = escaped_dir / "checkpoint.json"
    escaped_checkpoint.write_text(json.dumps({"manifest_hash": "abc123"}) + "\n", encoding="utf-8")
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(
            kind="llmff_resume",
            payload={
                "run_id": "../escape",
                "manifest_hash": "abc123",
                "checkpoint_path": str(escaped_checkpoint),
            },
            now=_at(0),
        )

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["failed_jobs"] == [{"job_id": 1, "reason": "invalid_run_id"}]
    assert result["resume_jobs"] == []
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.FAILED  # type: ignore[union-attr]


def test_run_daemon_cycle_fails_malformed_checkpoint_resume_without_crashing(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    checkpoint = run_dir / "checkpoint.json"
    checkpoint.write_text("{not-json\n", encoding="utf-8")
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(
            kind="llmff_resume",
            payload={
                "run_id": "run-1",
                "manifest_hash": "abc123",
                "checkpoint_path": str(checkpoint),
            },
            now=_at(0),
        )

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["failed_jobs"] == [{"job_id": 1, "reason": "checkpoint_unreadable"}]
    assert result["resume_jobs"] == []
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.FAILED  # type: ignore[union-attr]


def test_run_daemon_cycle_fails_checkpoint_resume_when_run_dir_symlink_escapes(
    tmp_path: Path,
):
    runs_root = tmp_path / ".sidecar" / "runs"
    runs_root.mkdir(parents=True)
    escaped_dir = tmp_path / ".sidecar" / "escaped-run"
    escaped_dir.mkdir()
    escaped_checkpoint = escaped_dir / "checkpoint.json"
    escaped_checkpoint.write_text(json.dumps({"manifest_hash": "abc123"}) + "\n", encoding="utf-8")
    (runs_root / "run-1").symlink_to("../escaped-run")
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(
            kind="llmff_resume",
            payload={
                "run_id": "run-1",
                "manifest_hash": "abc123",
                "checkpoint_path": str(escaped_checkpoint),
            },
            now=_at(0),
        )

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["failed_jobs"] == [{"job_id": 1, "reason": "run_dir_outside_runs"}]
    assert result["resume_jobs"] == []
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.FAILED  # type: ignore[union-attr]


def test_run_daemon_cycle_fails_malformed_checkpoint_resume_payload_without_crashing(
    tmp_path: Path,
):
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        queue.enqueue(
            kind="llmff_resume",
            payload={
                "run_id": "run-1",
                "manifest_hash": "abc123",
            },
            now=_at(0),
        )

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["failed_jobs"] == [{"job_id": 1, "reason": "resume_payload_invalid"}]
    assert result["resume_jobs"] == []
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        assert queue.get_job(1).state is JobState.FAILED  # type: ignore[union-attr]


def test_run_daemon_cycle_fails_non_object_checkpoint_resume_payload_without_crashing(
    tmp_path: Path,
):
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
                "llmff_resume",
                "[]",
                JobState.QUEUED.value,
                _at(0).isoformat(timespec="microseconds"),
                _at(0).isoformat(timespec="microseconds"),
            ),
        )
        queue.connection.commit()

    result = run_daemon_cycle(
        tmp_path,
        DaemonLoopConfig(
            worker_id="worker-a",
            max_jobs_per_cycle=1,
            concurrency_limit=1,
            lease_duration=timedelta(seconds=30),
            now=_at(10),
        ),
    )

    assert result["failed_jobs"] == [{"job_id": 1, "reason": "queue_payload_invalid"}]
    assert result["resume_jobs"] == []
    with DaemonQueue.open_sidecar(tmp_path) as queue:
        row = queue.connection.execute(
            "SELECT state, attempts, lease_owner, lease_expires_at FROM daemon_jobs WHERE id = 1"
        ).fetchone()
    assert tuple(row) == (JobState.FAILED.value, 0, None, None)


def test_write_worktree_profile_records_local_observability_refs(tmp_path: Path):
    profile_path = write_worktree_profile(
        tmp_path,
        app_boot={"command": "python -m app"},
        observability_refs=["http://127.0.0.1:3000/health"],
    )

    assert profile_path == tmp_path / ".sidecar" / "worktree-profile.json"
    assert json.loads(profile_path.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "app_boot": {"command": "python -m app"},
        "observability_refs": ["http://127.0.0.1:3000/health"],
        "runs_dir": ".sidecar/runs",
    }


def _at(seconds: int) -> datetime:
    return datetime(2026, 1, 1, 0, 0, seconds, tzinfo=timezone.utc)
