from __future__ import annotations

import os
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
        "latest_llmff_failure_kind: none",
        "pending_candidates: 0",
        "retention_candidates: 0",
        "manifest_policy: unrestricted",
    ]


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
    assert "latest_llmff_failure_kind: provider_error" in lines
    assert "retention_candidates: 1" in lines


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
    assert "latest_llmff_failure_kind: none" in lines
