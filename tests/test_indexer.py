from pathlib import Path

import pytest

from tugboat.corpus.indexer import InstructionIndexBudgetExceeded, index_repo, instruction_paths
from tugboat.models import InstructionFilePolicy, Policy


def test_index_repo_uses_policy_paths_and_globs_in_deterministic_order(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("# Agents\n\nTop priority.\n", encoding="utf-8")
    (tmp_path / "CODEX.md").write_text("# Codex\n\nAgent rules.\n", encoding="utf-8")
    skill_dir = tmp_path / ".codex" / "skills" / "python"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Python\n\nUse pytest.\n", encoding="utf-8")
    policy = Policy(
        instruction_files=(
            InstructionFilePolicy("AGENTS.md", "repo_policy", 80, True),
            InstructionFilePolicy("CODEX.md", "agent_policy", 70, True),
            InstructionFilePolicy(".codex/skills/**/SKILL.md", "skill", 60, False),
        )
    )

    result = index_repo(tmp_path, policy)

    assert [document.path for document in result.documents] == [
        "AGENTS.md",
        "CODEX.md",
        ".codex/skills/python/SKILL.md",
    ]
    assert [document.kind for document in result.documents] == [
        "repo_policy",
        "agent_policy",
        "skill",
    ]
    assert [document.precedence for document in result.documents] == [80, 70, 60]
    assert result.indexed_count == 3


def test_index_repo_preserves_instruction_metadata(tmp_path: Path):
    (tmp_path / "CODEX.md").write_text("# Codex\n\nAgent rules.\n", encoding="utf-8")
    policy = Policy(
        instruction_files=(
            InstructionFilePolicy("CODEX.md", "agent_policy", 70, True),
        )
    )

    result = index_repo(tmp_path, policy)

    document = result.documents[0]
    assert document.path == "CODEX.md"
    assert document.protected is True
    assert document.chunks[0].heading_path == ("Codex",)


def test_instruction_paths_accepts_legacy_instruction_file_entries(tmp_path: Path):
    (tmp_path / "CODEX.md").write_text("# Codex\n\nAgent rules.\n", encoding="utf-8")
    entries = (InstructionFilePolicy("CODEX.md", "agent_policy", 70, True),)

    paths = instruction_paths(tmp_path, entries)

    assert paths == [(tmp_path / "CODEX.md", entries[0])]


def test_index_repo_expands_instruction_file_scope_roots_to_repo_relative_paths(
    tmp_path: Path,
):
    api = tmp_path / "services" / "api"
    web = tmp_path / "services" / "web"
    api.mkdir(parents=True)
    web.mkdir(parents=True)
    (api / "CODEX.md").write_text("# API\n\nUse API fixtures.\n", encoding="utf-8")
    (web / "CODEX.md").write_text("# Web\n\nUse browser fixtures.\n", encoding="utf-8")
    policy = Policy(
        instruction_files=(
            InstructionFilePolicy("CODEX.md", "agent_policy", 70, True, "services/api"),
            InstructionFilePolicy("CODEX.md", "agent_policy", 70, True, "services/web"),
        ),
    )

    result = index_repo(tmp_path, policy)

    assert [document.path for document in result.documents] == [
        "services/api/CODEX.md",
        "services/web/CODEX.md",
    ]


def test_index_repo_blocks_when_deduped_instruction_file_budget_is_exceeded(
    tmp_path: Path,
):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "one.md").write_text("# One\n\nFirst.\n", encoding="utf-8")
    (docs / "two.md").write_text("# Two\n\nSecond.\n", encoding="utf-8")
    policy = Policy(
        instruction_files=(
            InstructionFilePolicy("docs/**/*.md", "repo_policy", 50, True),
        ),
        index_max_instruction_files=1,
    )

    with pytest.raises(
        InstructionIndexBudgetExceeded,
        match="instruction file budget exceeded: 2 discovered, limit 1",
    ):
        index_repo(tmp_path, policy)
