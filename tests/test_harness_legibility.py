from pathlib import Path

from tugboat.harness.checks import check_harness_legibility


def test_harness_legibility_passes_short_instruction_maps_with_local_refs(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )
    (repo / "CODEX.md").write_text(
        "# Codex Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is True
    assert result.findings == []


def test_harness_legibility_flags_monolithic_instruction_files(tmp_path: Path):
    repo = tmp_path
    (repo / "AGENTS.md").write_text("\n".join(f"line {n}" for n in range(5)), encoding="utf-8")

    result = check_harness_legibility(repo, max_instruction_lines=3)

    assert result.passed is False
    assert result.findings == [
        "AGENTS.md has 5 instruction lines; keep it at or below 3 and move detail into repo-local markdown references."
    ]


def test_harness_legibility_flags_broken_repo_local_markdown_links(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text(
        "# Codex Map\n\nSee [missing runbook](docs/missing.md).\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is False
    assert result.findings == [
        "CODEX.md references missing repo-local markdown file docs/missing.md."
    ]


def test_harness_legibility_flags_instruction_files_without_local_markdown_refs(tmp_path: Path):
    repo = tmp_path
    (repo / "SKILL.md").write_text(
        "# Skill\n\nUse pytest for verification.\n",
        encoding="utf-8",
    )

    result = check_harness_legibility(repo)

    assert result.passed is False
    assert result.findings == [
        "SKILL.md has no repo-local markdown references; keep instruction files as short maps to deeper docs."
    ]
