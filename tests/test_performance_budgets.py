from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from tugboat.cli import main
from tugboat.traces.ingest import ingest_jsonl_trace


def _touch_old(path: Path, *, days_old: int, text: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(path.name + "\n" if text is None else text, encoding="utf-8")
    timestamp = time.time() - days_old * 24 * 60 * 60
    os.utime(path, (timestamp, timestamp))


def test_large_instruction_index_within_budget_writes_expected_rows(
    tmp_path: Path,
    capsys,
) -> None:
    sidecar = tmp_path / ".sidecar"
    docs = tmp_path / "docs"
    sidecar.mkdir()
    docs.mkdir()
    instruction_count = 128
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
index:
  max_instruction_files: {instruction_count}
instruction_files:
  - path: docs/**/*.md
    kind: repo_policy
    precedence: 50
    protected: true
""".lstrip(),
        encoding="utf-8",
    )
    for index in range(instruction_count):
        (docs / f"instruction-{index:03d}.md").write_text(
            f"# Instruction {index:03d}\n\nKeep bounded edit {index:03d} evidence-backed.\n",
            encoding="utf-8",
        )

    assert main(["index", "--repo", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert f"indexed documents: {instruction_count}" in output
    assert "Traceback" not in output
    with closing(sqlite3.connect(sidecar / "db.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == (
            instruction_count
        )
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == (
            instruction_count
        )


def test_big_jsonl_trace_at_event_budget_ingests_all_events(
    tmp_path: Path,
) -> None:
    max_events = 512
    trace = tmp_path / "episode.jsonl"
    trace.write_text(
        "".join(
            json.dumps(
                {
                    "type": "tool_result",
                    "tool": "pytest",
                    "exit_code": 0,
                    "sequence": index,
                },
                sort_keys=True,
            )
            + "\n"
            for index in range(max_events)
        ),
        encoding="utf-8",
    )

    first = ingest_jsonl_trace(trace, max_events=max_events)
    second = ingest_jsonl_trace(trace, max_events=max_events)

    assert len(first.events) == max_events
    assert first.events[0].line_number == 1
    assert first.events[-1].line_number == max_events
    assert len({event.evidence_id for event in first.events}) == max_events
    assert [event.evidence_id for event in first.events] == [
        event.evidence_id for event in second.events
    ]


def test_retention_cleanup_at_scan_budget_deletes_only_expired_runtime_artifacts(
    tmp_path: Path,
    capsys,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    run_count = 12
    files_per_run = 4
    scan_budget = run_count * files_per_run
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
retention:
  raw_traces_days: 14
  checkpoints_days: 7
  max_scan_files: {scan_budget}
""".lstrip(),
        encoding="utf-8",
    )
    for index in range(run_count):
        run_dir = sidecar / "runs" / f"run-{index:03d}"
        _touch_old(run_dir / "trace-input.jsonl", days_old=15)
        _touch_old(run_dir / "events.jsonl", days_old=8)
        _touch_old(run_dir / "candidate.diff", days_old=99)
        _touch_old(run_dir / "trace-redacted.jsonl", days_old=1)

    assert main(["retention", "--repo", str(tmp_path), "--apply"]) == 0

    output = capsys.readouterr().out
    assert "retention_mode: apply" in output
    assert "Traceback" not in output
    report_path = sidecar / "ops" / "retention" / "retention-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "complete"
    assert len(report["deleted"]) == run_count * 2
    for index in range(run_count):
        run_dir = sidecar / "runs" / f"run-{index:03d}"
        assert not (run_dir / "trace-input.jsonl").exists()
        assert not (run_dir / "events.jsonl").exists()
        assert (run_dir / "candidate.diff").exists()
        assert (run_dir / "trace-redacted.jsonl").exists()
