from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from tugboat.cli import main
from tugboat.db import Store
from tugboat.llmff.contracts import RunResult
from tugboat.paths import sidecar_dir


def test_status_reports_empty_sidecar_state(tmp_path: Path, capsys):
    assert main(["status", "--repo", str(tmp_path)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "mode: proposal_only",
        "auto_apply: disabled",
        "indexed_documents: 0",
        "latest_run: none",
        "latest_llmff_job: none",
        "latest_llmff_exit_code: none",
        "latest_llmff_failure_kind: none",
        "pending_candidates: 0",
        "retention_candidates: 0",
        "retention_redaction_candidates: 0",
        "manifest_policy: unrestricted",
        f"status_report: {tmp_path / '.sidecar' / 'status-report.json'}",
    ]
    assert json.loads((tmp_path / ".sidecar" / "status-report.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "mode": "proposal_only",
        "auto_apply": "disabled",
        "indexed_documents": 0,
        "latest_run": None,
        "latest_llmff_job": None,
        "latest_llmff_exit_code": None,
        "latest_llmff_failure_kind": None,
        "pending_candidates": 0,
        "retention_candidates": 0,
        "retention_redaction_candidates": 0,
        "manifest_policy": "unrestricted",
    }


def test_status_reports_indexed_documents_and_latest_run(tmp_path: Path, capsys):
    (tmp_path / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")

    assert main(["index", "--repo", str(tmp_path)]) == 0
    assert main(["audit", "--repo", str(tmp_path), "--trace", str(trace), "--mock-llmff-inspect"]) == 0
    capsys.readouterr()

    assert main(["status", "--repo", str(tmp_path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "indexed_documents: 1" in lines
    assert "latest_run: audit completed" in lines


def test_status_reports_failed_llmff_job_and_retention_candidates(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    lifecycle_dir = run_dir / "patch-propose"
    lifecycle_dir.mkdir(parents=True)
    manifest = tmp_path / "patch-propose.yaml"
    manifest.write_text("name: patch-propose\n", encoding="utf-8")
    events = lifecycle_dir / "llmff-events.jsonl"
    events.write_text(
        '{"event":"run_failed","run_failed":{"failure_kind":"provider_error","failure_message":"backend unavailable"}}\n',
        encoding="utf-8",
    )
    trace = lifecycle_dir / "llmff-trace.jsonl"
    trace.write_text('{"event":"step"}\n', encoding="utf-8")
    checkpoint = lifecycle_dir / "checkpoint.json"
    checkpoint.write_text('{"manifest_hash":"abc"}\n', encoding="utf-8")
    old = 0
    os.utime(events, (old, old))

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="propose",
            manifest_hash="abc",
            status="failed",
            run_dir=run_dir,
        )
        store.record_llmff_run(
            run_id="run-1",
            manifest_hash="abc",
            result=RunResult(
                manifest_path=manifest,
                exit_code=1,
                trace_path=trace,
                events_path=events,
                checkpoint_path=checkpoint,
                output_paths={},
                failure_kind="provider_error",
                failure_message="backend unavailable",
            ),
        )

    assert main(["status", "--repo", str(repo)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "latest_run: propose failed" in lines
    assert "latest_llmff_job: patch-propose.yaml failed" in lines
    assert "latest_llmff_exit_code: 1" in lines
    assert "latest_llmff_failure_kind: provider_error" in lines
    assert "retention_candidates: 1" in lines


def test_status_reports_retention_redaction_candidates(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    trace = run_dir / "trace-input.jsonl"
    trace.write_text('{"output":"OPENAI_API_KEY=sk-1234567890abcdefghijkl"}\n', encoding="utf-8")
    os.utime(trace, (0, 0))

    assert main(["status", "--repo", str(repo)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "retention_candidates: 1" in lines
    assert "retention_redaction_candidates: 1" in lines


def test_status_reports_llmff_failure_kind_from_job_record_without_events(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    lifecycle_dir = run_dir / "patch-eval"
    lifecycle_dir.mkdir(parents=True)
    manifest = tmp_path / "patch-eval.yaml"
    manifest.write_text("name: patch-eval\n", encoding="utf-8")

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="eval",
            manifest_hash="abc",
            status="failed",
            run_dir=run_dir,
        )
        store.record_llmff_run(
            run_id="run-1",
            manifest_hash="abc",
            result=RunResult(
                manifest_path=manifest,
                exit_code=124,
                trace_path=lifecycle_dir / "llmff-trace.jsonl",
                events_path=lifecycle_dir / "missing-events.jsonl",
                checkpoint_path=lifecycle_dir / "checkpoint.json",
                output_paths={},
                failure_kind="timeout",
                failure_message="Timed out after 12000 ms",
            ),
        )

    assert main(["status", "--repo", str(repo)]) == 0

    assert "latest_llmff_failure_kind: timeout" in capsys.readouterr().out.splitlines()


def test_status_reports_llmff_job_for_latest_run_only(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    old_run_dir = repo / ".sidecar" / "runs" / "20260101T000000000000Z"
    old_lifecycle_dir = old_run_dir / "patch-propose"
    old_lifecycle_dir.mkdir(parents=True)
    new_run_dir = repo / ".sidecar" / "runs" / "20260102T000000000000Z"
    new_run_dir.mkdir(parents=True)
    manifest = tmp_path / "patch-propose.yaml"
    manifest.write_text("name: patch-propose\n", encoding="utf-8")
    events = old_lifecycle_dir / "llmff-events.jsonl"
    events.write_text(
        '{"event":"run_failed","run_failed":{"failure_kind":"provider_error"}}\n',
        encoding="utf-8",
    )
    trace = old_lifecycle_dir / "llmff-trace.jsonl"
    trace.write_text('{"event":"step"}\n', encoding="utf-8")
    checkpoint = old_lifecycle_dir / "checkpoint.json"
    checkpoint.write_text('{"manifest_hash":"abc"}\n', encoding="utf-8")

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id=old_run_dir.name,
            stage="propose",
            manifest_hash="abc",
            status="failed",
            run_dir=old_run_dir,
        )
        store.record_llmff_run(
            run_id=old_run_dir.name,
            manifest_hash="abc",
            result=RunResult(
                manifest_path=manifest,
                exit_code=1,
                trace_path=trace,
                events_path=events,
                checkpoint_path=checkpoint,
                output_paths={},
                failure_kind="provider_error",
            ),
        )
        store.insert_run(
            run_id=new_run_dir.name,
            stage="audit",
            manifest_hash="def",
            status="completed",
            run_dir=new_run_dir,
        )

    assert main(["status", "--repo", str(repo)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "latest_run: audit completed" in lines
    assert "latest_llmff_job: none" in lines
    assert "latest_llmff_exit_code: none" in lines
    assert "latest_llmff_failure_kind: none" in lines


def test_status_reports_unknown_exit_code_for_legacy_llmff_jobs(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    db_path = sidecar_dir(repo) / "db.sqlite"
    db_path.parent.mkdir(parents=True)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            """
            CREATE TABLE runs (
              id TEXT PRIMARY KEY,
              episode_id INTEGER,
              stage TEXT NOT NULL,
              manifest_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              run_dir TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE llmff_jobs (
              id INTEGER PRIMARY KEY,
              run_id TEXT NOT NULL,
              manifest_name TEXT NOT NULL,
              manifest_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              audit_event_sequence INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE audit_events (
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              previous_hash TEXT NOT NULL,
              event_hash TEXT NOT NULL
            )
            """
        )
        cursor = connection.execute(
            """
            INSERT INTO audit_events(event_type, payload_json, previous_hash, event_hash)
            VALUES ('llmff_job.recorded', '{"run_id":"run-1"}', '', 'legacy-hash')
            """
        )
        connection.execute(
            """
            INSERT INTO runs(id, episode_id, stage, manifest_hash, status, run_dir, created_at, updated_at)
            VALUES ('run-1', NULL, 'propose', 'abc', 'failed', '.sidecar/runs/run-1', 'run-1', 'run-1')
            """
        )
        connection.execute(
            """
            INSERT INTO llmff_jobs(run_id, manifest_name, manifest_hash, status, audit_event_sequence)
            VALUES ('run-1', 'patch-propose.yaml', 'abc', 'failed', ?)
            """,
            (cursor.lastrowid,),
        )
        connection.commit()

    assert main(["status", "--repo", str(repo)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "latest_llmff_job: patch-propose.yaml failed" in lines
    assert "latest_llmff_exit_code: none" in lines
