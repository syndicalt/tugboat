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
