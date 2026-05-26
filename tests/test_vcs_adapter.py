from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tugboat.policy.gate import CandidatePatch
from tugboat.vcs import VcsAdapter, VcsStateError


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tugboat@example.test")
    _git(repo, "config", "user.name", "Tugboat Tests")
    (repo / "CODEX.md").write_text("# Codex\n\nKeep tests green.\n", encoding="utf-8")
    (repo / "README.md").write_text("# Readme\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def test_clean_worktree_reports_clean_repo(tmp_path: Path):
    repo = _init_repo(tmp_path)

    check = VcsAdapter(repo).check_clean_worktree()

    assert check.clean is True
    assert check.dirty_paths == ()


def test_clean_worktree_reports_dirty_repo(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# Readme\n\nchanged\n", encoding="utf-8")

    check = VcsAdapter(repo).check_clean_worktree()

    assert check.clean is False
    assert check.dirty_paths == ("README.md",)


def test_target_dirty_check_rejects_dirty_target_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "CODEX.md").write_text("# Codex\n\nchanged\n", encoding="utf-8")

    with pytest.raises(VcsStateError, match="target files are dirty: CODEX.md"):
        VcsAdapter(repo).assert_target_files_clean(("CODEX.md",))


def test_target_dirty_check_allows_unrelated_dirty_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# Readme\n\nchanged\n", encoding="utf-8")

    VcsAdapter(repo).assert_target_files_clean(("CODEX.md",))


def test_base_hash_check_rejects_stale_file_content(tmp_path: Path):
    repo = _init_repo(tmp_path)
    stale_hash = CandidatePatch.hash_file(repo / "CODEX.md")
    (repo / "CODEX.md").write_text("# Codex\n\nnew base\n", encoding="utf-8")

    with pytest.raises(VcsStateError, match="base hash mismatch for CODEX.md"):
        VcsAdapter(repo).assert_base_hashes({"CODEX.md": stale_hash})


def test_branch_name_generation_is_deterministic_and_sanitized(tmp_path: Path):
    adapter = VcsAdapter(tmp_path)

    branch = adapter.branch_name(run_id="Run 42", candidate_id=7, base_file="docs/My File.md")

    assert branch == "tugboat/run-42/candidate-7/docs-my-file-md"


def test_commit_message_generation_is_deterministic(tmp_path: Path):
    adapter = VcsAdapter(tmp_path)

    message = adapter.commit_message(
        run_id="run-42",
        candidate_id=7,
        base_file="CODEX.md",
        rationale="Clarify test expectations.\nKeep provenance visible.",
    )

    assert message == "\n".join(
        [
            "tugboat: apply candidate 7 for CODEX.md",
            "",
            "Run: run-42",
            "Candidate: 7",
            "Base file: CODEX.md",
            "",
            "Rationale:",
            "Clarify test expectations.",
            "Keep provenance visible.",
            "",
        ]
    )


def test_rollback_metadata_contains_revert_commands_without_mutating_repo(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")

    metadata = VcsAdapter(repo).rollback_metadata(
        commit_sha=head,
        branch_name="tugboat/run-42/candidate-7/codex-md",
        files=("CODEX.md",),
        reason="eval regression",
    )

    assert metadata.to_json_dict() == {
        "branch_name": "tugboat/run-42/candidate-7/codex-md",
        "commands": [
            ["git", "switch", "tugboat/run-42/candidate-7/codex-md"],
            ["git", "revert", "--no-edit", head],
        ],
        "commit_sha": head,
        "files": ["CODEX.md"],
        "reason": "eval regression",
        "strategy": "git_revert",
    }
    assert _git(repo, "rev-parse", "HEAD") == head


def test_apply_diff_converts_git_conflict_to_vcs_state_error(tmp_path: Path):
    repo = _init_repo(tmp_path)
    diff_path = tmp_path / "conflicting.diff"
    diff_path.write_text(
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,3 +1,4 @@\n"
        " # Codex\n"
        " \n"
        " Different existing text.\n"
        "+Record rollback notes.\n",
        encoding="utf-8",
    )

    with pytest.raises(VcsStateError, match="git apply failed"):
        VcsAdapter(repo).apply_diff(diff_path)

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == "# Codex\n\nKeep tests green.\n"
