from __future__ import annotations

import json
from pathlib import Path

from tugboat.cli import main
from tugboat.db import Store
from tugboat.paths import sidecar_dir


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
