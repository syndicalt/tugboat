from __future__ import annotations

import os
import json
import time
from pathlib import Path

import pytest

from tugboat.models import Policy
from tugboat.cli import _write_retention_report, main
from tugboat.ops.retention import apply_retention_policy


def _touch_old(path: Path, *, days_old: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name + "\n", encoding="utf-8")
    timestamp = time.time() - days_old * 24 * 60 * 60
    os.utime(path, (timestamp, timestamp))


def test_retention_policy_dry_run_reports_expired_raw_trace_and_checkpoints(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _touch_old(run_dir / "trace-input.jsonl", days_old=15)
    _touch_old(run_dir / "trace-redacted.jsonl", days_old=15)
    _touch_old(run_dir / "events.jsonl", days_old=8)
    _touch_old(run_dir / "checkpoint-patch-eval.json", days_old=8)
    _touch_old(run_dir / "audit.json", days_old=99)

    result = apply_retention_policy(
        tmp_path,
        Policy(raw_traces_retention_days=14, checkpoints_retention_days=7),
        dry_run=True,
    )

    assert result.deleted == ()
    assert result.redaction_candidates == ()
    assert result.candidates == (
        ".sidecar/runs/run-1/checkpoint-patch-eval.json",
        ".sidecar/runs/run-1/events.jsonl",
        ".sidecar/runs/run-1/trace-input.jsonl",
        ".sidecar/runs/run-1/trace-redacted.jsonl",
    )
    assert (run_dir / "trace-input.jsonl").exists()
    assert (run_dir / "trace-redacted.jsonl").exists()
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "checkpoint-patch-eval.json").exists()
    assert (run_dir / "audit.json").exists()


def test_retention_policy_dry_run_reports_secret_bearing_runtime_artifacts(
    tmp_path: Path,
):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    secret_trace = run_dir / "trace-input.jsonl"
    _touch_old(secret_trace, days_old=15)
    secret_trace.write_text('{"output":"OPENAI_API_KEY=sk-1234567890abcdefghijkl"}\n', encoding="utf-8")
    timestamp = time.time() - 15 * 24 * 60 * 60
    os.utime(secret_trace, (timestamp, timestamp))

    result = apply_retention_policy(
        tmp_path,
        Policy(raw_traces_retention_days=14, checkpoints_retention_days=7),
        dry_run=True,
    )

    assert result.deleted == ()
    assert result.redaction_candidates == (
        {
            "path": ".sidecar/runs/run-1/trace-input.jsonl",
            "line_number": 1,
            "kind": "openai_api_key",
        },
    )
    assert secret_trace.exists()
    assert "sk-1234567890abcdefghijkl" in secret_trace.read_text(encoding="utf-8")


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
    assert result.redaction_candidates == ()
    assert result.candidates == (
        ".sidecar/runs/run-1/episode-audit/checkpoint.json",
        ".sidecar/runs/run-1/episode-audit/llmff-events.jsonl",
        ".sidecar/runs/run-1/episode-audit/llmff-trace.jsonl",
    )
    assert (run_dir / "episode-audit" / "llmff-inspect.json").exists()


def test_retention_policy_delete_mode_removes_only_expired_runtime_artifacts(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _touch_old(run_dir / "trace-input.jsonl", days_old=15)
    _touch_old(run_dir / "trace-redacted.jsonl", days_old=15)
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
        ".sidecar/runs/run-1/trace-redacted.jsonl",
    )
    assert not (run_dir / "trace-input.jsonl").exists()
    assert not (run_dir / "trace-redacted.jsonl").exists()
    assert not (run_dir / "events.jsonl").exists()
    assert not (run_dir / "checkpoint-patch-eval.json").exists()
    assert (run_dir / "candidate.diff").exists()


def test_retention_cli_dry_run_reports_expired_runtime_artifacts(
    tmp_path: Path, capsys
):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
retention:
  raw_traces_days: 14
  checkpoints_days: 7
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _touch_old(run_dir / "trace-input.jsonl", days_old=15)
    _touch_old(run_dir / "episode-audit" / "checkpoint.json", days_old=8)

    assert main(["retention", "--repo", str(tmp_path)]) == 0

    output = capsys.readouterr().out.splitlines()
    assert output == [
        "retention_mode: dry-run",
        "candidates: 2",
        "deleted: 0",
        "redaction_candidates: 0",
        f"retention_report: {tmp_path / '.sidecar' / 'ops' / 'retention' / 'retention-report.json'}",
        "candidate: .sidecar/runs/run-1/episode-audit/checkpoint.json",
        "candidate: .sidecar/runs/run-1/trace-input.jsonl",
    ]
    assert json.loads(
        (tmp_path / ".sidecar" / "ops" / "retention" / "retention-report.json").read_text(
            encoding="utf-8"
        )
    ) == {
        "schema_version": 1,
        "mode": "dry-run",
        "status": "complete",
        "candidates": [
            ".sidecar/runs/run-1/episode-audit/checkpoint.json",
            ".sidecar/runs/run-1/trace-input.jsonl",
        ],
        "deleted": [],
        "redaction_candidates": [],
    }
    assert (run_dir / "trace-input.jsonl").exists()
    assert (run_dir / "episode-audit" / "checkpoint.json").exists()


def test_retention_cli_apply_deletes_expired_runtime_artifacts(
    tmp_path: Path, capsys
):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
retention:
  raw_traces_days: 14
  checkpoints_days: 7
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _touch_old(run_dir / "trace-redacted.jsonl", days_old=15)
    _touch_old(run_dir / "episode-audit" / "llmff-events.jsonl", days_old=8)

    assert main(["retention", "--repo", str(tmp_path), "--apply"]) == 0

    output = capsys.readouterr().out.splitlines()
    assert output == [
        "retention_mode: apply",
        "candidates: 2",
        "deleted: 2",
        "redaction_candidates: 0",
        f"retention_report: {tmp_path / '.sidecar' / 'ops' / 'retention' / 'retention-report.json'}",
        "candidate: .sidecar/runs/run-1/episode-audit/llmff-events.jsonl",
        "candidate: .sidecar/runs/run-1/trace-redacted.jsonl",
        "deleted: .sidecar/runs/run-1/episode-audit/llmff-events.jsonl",
        "deleted: .sidecar/runs/run-1/trace-redacted.jsonl",
    ]
    assert json.loads(
        (tmp_path / ".sidecar" / "ops" / "retention" / "retention-report.json").read_text(
            encoding="utf-8"
        )
    ) == {
        "schema_version": 1,
        "mode": "apply",
        "status": "complete",
        "candidates": [
            ".sidecar/runs/run-1/episode-audit/llmff-events.jsonl",
            ".sidecar/runs/run-1/trace-redacted.jsonl",
        ],
        "deleted": [
            ".sidecar/runs/run-1/episode-audit/llmff-events.jsonl",
            ".sidecar/runs/run-1/trace-redacted.jsonl",
        ],
        "redaction_candidates": [],
    }
    assert not (run_dir / "trace-redacted.jsonl").exists()
    assert not (run_dir / "episode-audit" / "llmff-events.jsonl").exists()


def test_retention_cli_apply_is_blocked_by_read_only_kill_switch(
    tmp_path: Path,
    capsys,
) -> None:
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
retention:
  raw_traces_days: 14
  checkpoints_days: 7
""".lstrip(),
        encoding="utf-8",
    )
    (policy_dir / "read-only.kill").write_text("enabled\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    trace = run_dir / "trace-redacted.jsonl"
    _touch_old(trace, days_old=15)

    assert main(["retention", "--repo", str(tmp_path), "--apply"]) == 1

    assert "retention blocked: read-only kill switch is enabled" in capsys.readouterr().out
    assert trace.exists()
    assert not (tmp_path / ".sidecar" / "ops" / "retention" / "retention-report.json").exists()


def test_retention_cli_apply_preflights_report_before_deleting(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
retention:
  raw_traces_days: 14
  checkpoints_days: 7
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    trace = run_dir / "trace-input.jsonl"
    _touch_old(trace, days_old=15)

    def fail_report(*args, **kwargs):
        raise PermissionError("report destination unavailable")

    monkeypatch.setattr("tugboat.cli._write_retention_report", fail_report)

    with pytest.raises(PermissionError, match="report destination unavailable"):
        main(["retention", "--repo", str(tmp_path), "--apply"])

    assert trace.exists()


def test_retention_cli_apply_leaves_planned_report_if_final_write_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
retention:
  raw_traces_days: 14
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    trace = run_dir / "trace-input.jsonl"
    _touch_old(trace, days_old=15)

    real_replace = Path.replace
    replace_count = 0

    def fail_final_retention_report_replace(self, target):
        nonlocal replace_count
        if Path(target).name == "retention-report.json":
            replace_count += 1
            if replace_count == 2:
                raise PermissionError("final report unavailable")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_final_retention_report_replace)

    with pytest.raises(PermissionError, match="final report unavailable"):
        main(["retention", "--repo", str(tmp_path), "--apply"])

    assert not trace.exists()
    assert json.loads(
        (tmp_path / ".sidecar" / "ops" / "retention" / "retention-report.json").read_text(
            encoding="utf-8"
        )
    ) == {
        "schema_version": 1,
        "mode": "apply",
        "status": "planned",
        "candidates": [".sidecar/runs/run-1/trace-input.jsonl"],
        "deleted": [],
        "redaction_candidates": [],
    }


def test_retention_report_writes_use_unique_temp_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    real_replace = Path.replace
    temp_names: list[str] = []

    def record_replace(self, target):
        if Path(target).name == "retention-report.json":
            temp_names.append(self.name)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", record_replace)

    _write_retention_report(
        tmp_path,
        mode="dry-run",
        status="complete",
        candidates=(),
        deleted=(),
        redaction_candidates=(),
    )
    _write_retention_report(
        tmp_path,
        mode="dry-run",
        status="complete",
        candidates=(".sidecar/runs/run-1/trace-input.jsonl",),
        deleted=(),
        redaction_candidates=(),
    )

    assert len(temp_names) == 2
    assert len(set(temp_names)) == 2
    assert ".retention-report.json.tmp" not in temp_names


def test_retention_cli_reports_redaction_candidates_without_mutating_file(
    tmp_path: Path,
    capsys,
):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
retention:
  raw_traces_days: 14
  checkpoints_days: 7
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    trace = run_dir / "trace-input.jsonl"
    _touch_old(trace, days_old=15)
    trace.write_text('{"output":"OPENAI_API_KEY=sk-1234567890abcdefghijkl"}\n', encoding="utf-8")
    timestamp = time.time() - 15 * 24 * 60 * 60
    os.utime(trace, (timestamp, timestamp))

    assert main(["retention", "--repo", str(tmp_path)]) == 0

    output = capsys.readouterr().out.splitlines()
    assert output == [
        "retention_mode: dry-run",
        "candidates: 1",
        "deleted: 0",
        "redaction_candidates: 1",
        f"retention_report: {tmp_path / '.sidecar' / 'ops' / 'retention' / 'retention-report.json'}",
        "candidate: .sidecar/runs/run-1/trace-input.jsonl",
        "redaction_candidate: .sidecar/runs/run-1/trace-input.jsonl:1:openai_api_key",
    ]
    assert json.loads(
        (tmp_path / ".sidecar" / "ops" / "retention" / "retention-report.json").read_text(
            encoding="utf-8"
        )
    ) == {
        "schema_version": 1,
        "mode": "dry-run",
        "status": "complete",
        "candidates": [".sidecar/runs/run-1/trace-input.jsonl"],
        "deleted": [],
        "redaction_candidates": [
            {
                "path": ".sidecar/runs/run-1/trace-input.jsonl",
                "line_number": 1,
                "kind": "openai_api_key",
            }
        ],
    }
    assert "sk-1234567890abcdefghijkl" in trace.read_text(encoding="utf-8")
