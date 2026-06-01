from pathlib import Path

from tugboat.cli import main


def test_harness_check_cli_reports_findings(tmp_path: Path, capsys):
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [Missing](docs/MISSING.md).\n",
        encoding="utf-8",
    )

    exit_code = main(["harness", "check", "--repo", str(tmp_path)])

    assert exit_code == 1
    assert "references missing repo-local markdown file docs/MISSING.md" in capsys.readouterr().out


def test_harness_report_cli_writes_knowledge_map_and_tasks(tmp_path: Path, capsys):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    exit_code = main(["harness", "report", "--repo", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "# Tugboat Harness Report" in output
    assert "AGENTS.md -> docs/runbook.md" in output
    assert "## Token Efficiency" in output
    assert "instruction_corpus_estimated_tokens:" in output
    assert "active_context_estimated_tokens:" in output
    assert "retrieval_pack_file_count:" in output
    assert "retrieval_pack_estimated_tokens:" in output
    assert "duplicate_rule_estimated_tokens:" in output
    assert "- Add ownership metadata to docs/runbook.md." in output
