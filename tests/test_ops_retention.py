from __future__ import annotations

import os
import time
from pathlib import Path

from tugboat.models import Policy
from tugboat.ops.retention import apply_retention_policy


def _touch_old(path: Path, *, days_old: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name + "\n", encoding="utf-8")
    timestamp = time.time() - days_old * 24 * 60 * 60
    os.utime(path, (timestamp, timestamp))


def test_retention_policy_dry_run_reports_expired_raw_trace_and_checkpoints(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _touch_old(run_dir / "trace-input.jsonl", days_old=15)
    _touch_old(run_dir / "events.jsonl", days_old=8)
    _touch_old(run_dir / "checkpoint-patch-eval.json", days_old=8)
    _touch_old(run_dir / "audit.json", days_old=99)

    result = apply_retention_policy(
        tmp_path,
        Policy(raw_traces_retention_days=14, checkpoints_retention_days=7),
        dry_run=True,
    )

    assert result.deleted == ()
    assert result.candidates == (
        ".sidecar/runs/run-1/checkpoint-patch-eval.json",
        ".sidecar/runs/run-1/events.jsonl",
        ".sidecar/runs/run-1/trace-input.jsonl",
    )
    assert (run_dir / "trace-input.jsonl").exists()
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "checkpoint-patch-eval.json").exists()
    assert (run_dir / "audit.json").exists()


def test_retention_policy_dry_run_reports_expired_per_manifest_lifecycle_trace_and_events(
    tmp_path: Path,
):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _touch_old(run_dir / "episode-audit" / "llmff-trace.jsonl", days_old=15)
    _touch_old(run_dir / "episode-audit" / "llmff-events.jsonl", days_old=8)
    _touch_old(run_dir / "episode-audit" / "checkpoint.json", days_old=8)
    _touch_old(run_dir / "episode-audit" / "llmff-inspect.json", days_old=99)

    result = apply_retention_policy(
        tmp_path,
        Policy(raw_traces_retention_days=14, checkpoints_retention_days=7),
        dry_run=True,
    )

    assert result.deleted == ()
    assert result.candidates == (
        ".sidecar/runs/run-1/episode-audit/checkpoint.json",
        ".sidecar/runs/run-1/episode-audit/llmff-events.jsonl",
        ".sidecar/runs/run-1/episode-audit/llmff-trace.jsonl",
    )
    assert (run_dir / "episode-audit" / "llmff-inspect.json").exists()


def test_retention_policy_delete_mode_removes_only_expired_runtime_artifacts(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _touch_old(run_dir / "trace-input.jsonl", days_old=15)
    _touch_old(run_dir / "events.jsonl", days_old=8)
    _touch_old(run_dir / "checkpoint-patch-eval.json", days_old=8)
    _touch_old(run_dir / "candidate.diff", days_old=99)

    result = apply_retention_policy(
        tmp_path,
        Policy(raw_traces_retention_days=14, checkpoints_retention_days=7),
        dry_run=False,
    )

    assert result.deleted == (
        ".sidecar/runs/run-1/checkpoint-patch-eval.json",
        ".sidecar/runs/run-1/events.jsonl",
        ".sidecar/runs/run-1/trace-input.jsonl",
    )
    assert not (run_dir / "trace-input.jsonl").exists()
    assert not (run_dir / "events.jsonl").exists()
    assert not (run_dir / "checkpoint-patch-eval.json").exists()
    assert (run_dir / "candidate.diff").exists()
