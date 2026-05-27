from pathlib import Path

from tugboat.corpus.indexer import index_repo
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
