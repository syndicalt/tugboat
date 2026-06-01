from pathlib import Path

from tugboat.cli import main


def _write_budgeted_instruction_policy(repo: Path) -> None:
    sidecar = repo / ".sidecar"
    docs = repo / "docs"
    sidecar.mkdir()
    docs.mkdir()
    (docs / "one.md").write_text("# One\n\nFirst.\n", encoding="utf-8")
    (docs / "two.md").write_text("# Two\n\nSecond.\n", encoding="utf-8")
    (sidecar / "policy.yaml").write_text(
        """
version: 1
index:
  max_instruction_files: 1
instruction_files:
  - path: docs/**/*.md
    kind: repo_policy
    precedence: 50
    protected: true
""".lstrip(),
        encoding="utf-8",
    )


def test_harness_check_cli_reports_findings(tmp_path: Path, capsys):
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [Missing](docs/MISSING.md).\n",
        encoding="utf-8",
    )

    exit_code = main(["harness", "check", "--repo", str(tmp_path)])

    assert exit_code == 1
    assert "references missing repo-local markdown file docs/MISSING.md" in capsys.readouterr().out


def test_harness_check_blocks_instruction_file_budget_failure_without_traceback(
    tmp_path: Path,
    capsys,
):
    _write_budgeted_instruction_policy(tmp_path)

    exit_code = main(["harness", "check", "--repo", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "harness blocked: instruction file budget exceeded: 2 discovered, limit 1" in output
    assert "Traceback" not in output


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
    assert "duplicate_rule_token_budget:" in output
    assert "- Add ownership metadata to docs/runbook.md." in output


def test_harness_report_blocks_instruction_file_budget_failure_without_traceback(
    tmp_path: Path,
    capsys,
):
    _write_budgeted_instruction_policy(tmp_path)

    exit_code = main(["harness", "report", "--repo", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "harness blocked: instruction file budget exceeded: 2 discovered, limit 1" in output
    assert "Traceback" not in output
    assert not (tmp_path / ".sidecar" / "harness-report.json").exists()


def test_harness_report_cli_prints_duplicate_rule_token_budget_violation(
    tmp_path: Path,
    capsys,
):
    duplicated_rule = "MUST " + " ".join(f"token{i}" for i in range(120)) + "."
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Map\n\n"
        f"{duplicated_rule}\n"
        f"{duplicated_rule}\n",
        encoding="utf-8",
    )

    exit_code = main(["harness", "report", "--repo", str(tmp_path)])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "duplicate_rule_token_budget: 100" in output
    assert (
        "token_budget_violation: duplicate instruction rules estimated at 121 "
        "tokens exceeds duplicate rule budget 100."
    ) in output
