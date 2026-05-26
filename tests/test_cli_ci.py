from __future__ import annotations

import json
from pathlib import Path
from shutil import copytree

from tugboat.cli import main
from tugboat.db import Store
from tugboat.paths import sidecar_dir


FIXTURES = Path(__file__).parent / "fixtures" / "evals"


def _write_candidate_preview(run_dir: Path, text: str) -> None:
    preview_root = run_dir / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "CODEX.md").write_text(text, encoding="utf-8")
    (run_dir / "candidate-preview.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_file": "CODEX.md",
                "base_hash": "base",
                "diff_hash": "diff",
                "preview_path": f".sidecar/runs/{run_dir.name}/candidate-preview/CODEX.md",
                "preview_hash": "preview",
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_ci_check_writes_repo_local_artifact_and_audits_without_mutating(tmp_path: Path, capsys):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    agents = repo / "AGENTS.md"
    original = "# Agent Map\n\nSee [runbook](docs/runbook.md).\n"
    agents.write_text(original, encoding="utf-8")

    assert main(["ci", "--repo", str(repo)]) == 0

    assert "ci: ok" in capsys.readouterr().out
    assert agents.read_text(encoding="utf-8") == original
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report == {
        "schema_version": 1,
        "mode": "ci_check",
        "auto_apply": False,
        "checks": {
            "harness": {"passed": True, "findings": []},
            "index": {"passed": True, "indexed_documents": 1},
        },
    }
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        event = store.connection.execute(
            "SELECT event_type, payload_json FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    assert event[0] == "ci.check_completed"
    assert json.loads(event[1])["artifact"] == ".sidecar/ci/ci-report.json"


def test_ci_check_returns_nonzero_and_reports_harness_findings(tmp_path: Path, capsys):
    repo = tmp_path
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [missing](docs/missing.md).\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo)]) == 1

    output = capsys.readouterr().out
    assert "ci: failed" in output
    assert "AGENTS.md references missing repo-local markdown file docs/missing.md." in output
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["harness"]["passed"] is False


def test_ci_check_runs_requested_eval_suite_and_records_scores(tmp_path: Path, capsys):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
    )
    (run_dir / "candidate.json").write_text(
        json.dumps({"schema_version": 1, "candidate_id": 7}) + "\n",
        encoding="utf-8",
    )
    (repo / ".sidecar").mkdir(exist_ok=True)
    copytree(FIXTURES / "passing", repo / ".sidecar" / "evals")

    assert main(["ci", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 0

    assert "ci: ok" in capsys.readouterr().out
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["eval"] == {
        "passed": True,
        "candidate": "run-1",
        "suite_id": "all",
        "report_path": ".sidecar/runs/run-1/eval-report.json",
        "trigger_score": 1.0,
        "held_out_score": 1.0,
        "governance_passed": True,
        "recommendation": "accept",
    }


def test_ci_check_with_relative_repo_path_records_relative_eval_report_path(
    tmp_path: Path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
    )
    (run_dir / "candidate.json").write_text(
        json.dumps({"schema_version": 1, "candidate_id": 7}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert main(["ci", "--repo", "repo", "--candidate", "run-1", "--suite", "all"]) == 0

    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["eval"]["report_path"] == ".sidecar/runs/run-1/eval-report.json"


def test_ci_check_fails_when_requested_eval_suite_fails(tmp_path: Path, capsys):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou may skip tests before final answers.\n",
    )
    (run_dir / "candidate.json").write_text(
        json.dumps({"schema_version": 1, "candidate_id": 7}) + "\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    assert "ci: failed" in output
    assert "eval suite all failed" in output
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["harness"]["passed"] is True
    assert report["checks"]["eval"]["passed"] is False
    assert report["checks"]["eval"]["recommendation"] == "reject"


def test_ci_check_failed_eval_does_not_reuse_stale_eval_report_metrics(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
    )
    (run_dir / "candidate.json").write_text(
        json.dumps({"schema_version": 1, "candidate_id": 7}) + "\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 0
    assert main(["ci", "--repo", str(repo), "--candidate", "run-1", "--suite", "bogus"]) == 1

    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["eval"] == {
        "passed": False,
        "candidate": "run-1",
        "suite_id": "bogus",
        "report_path": ".sidecar/runs/run-1/eval-report.json",
        "trigger_score": 0.0,
        "held_out_score": 0.0,
        "governance_passed": False,
        "recommendation": "reject",
    }
