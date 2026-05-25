from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tugboat.cli import main


def test_eval_suite_all_runs_offline_and_writes_recommendation_metrics(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate.json").write_text(
        json.dumps({"schema_version": 1, "candidate_id": 7}) + "\n",
        encoding="utf-8",
    )

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 0

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["suite_id"] == "all"
    assert report["trigger_score"] == 1.0
    assert report["held_out_score"] == 1.0
    assert report["governance_passed"] is True
    assert report["recommendation"] == "accept"
    assert "trigger_score" not in report["metrics"]
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        eval_run = connection.execute(
            """
            SELECT candidate_id, suite_id, status, report_path, audit_event_sequence
            FROM eval_runs
            """
        ).fetchone()

    assert eval_run[:4] == (7, "all", "passed", str(run_dir / "eval-report.json"))
    assert eval_run[4] is not None


def test_eval_suite_all_returns_nonzero_for_governance_regression(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Policy\n\nYou may skip tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate.json").write_text(
        json.dumps({"schema_version": 1, "candidate_id": 7}) + "\n",
        encoding="utf-8",
    )

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["metrics"]["governance_regressions"] == 1
    assert report["recommendation"] == "reject"
