from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tugboat.policy.gate import CandidatePatch
from tugboat.vcs import PullRequestMetadata, VcsAdapter, VcsStateError


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


def test_create_pull_request_uses_github_cli_and_parses_result(tmp_path: Path, monkeypatch):
    repo = _init_repo(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        assert kwargs["cwd"] == repo
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="https://github.com/syndicalt/tugboat/pull/42\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = VcsAdapter(repo).create_pull_request(
        PullRequestMetadata(
            title="tugboat: apply candidate 7 for CODEX.md",
            body="Candidate: 7",
            branch_name="tugboat/run/candidate-7/codex-md",
            base_branch="main",
            draft=True,
        ),
        provider="github_cli",
    )

    assert calls == [
        [
            "gh",
            "pr",
            "create",
            "--base",
            "main",
            "--head",
            "tugboat/run/candidate-7/codex-md",
            "--title",
            "tugboat: apply candidate 7 for CODEX.md",
            "--body",
            "Candidate: 7",
            "--draft",
        ]
    ]
    assert result.to_json_dict() == {
        "created": True,
        "number": 42,
        "provider": "github_cli",
        "url": "https://github.com/syndicalt/tugboat/pull/42",
    }


def test_create_pull_request_rejects_unsupported_provider(tmp_path: Path):
    repo = _init_repo(tmp_path)

    with pytest.raises(VcsStateError, match="unsupported pull request provider: webhook"):
        VcsAdapter(repo).create_pull_request(
            PullRequestMetadata(
                title="tugboat",
                body="body",
                branch_name="branch",
                base_branch="main",
            ),
            provider="webhook",
        )


def test_create_pull_request_converts_missing_github_cli_to_vcs_state_error(
    tmp_path: Path,
    monkeypatch,
):
    repo = _init_repo(tmp_path)

    def missing_binary(command, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", missing_binary)

    with pytest.raises(VcsStateError, match="gh pr create failed: gh"):
        VcsAdapter(repo).create_pull_request(
            PullRequestMetadata(
                title="tugboat",
                body="body",
                branch_name="branch",
                base_branch="main",
            ),
            provider="github_cli",
        )


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
        VcsAdapter(repo).apply_diff(diff_path, allowed_paths=("CODEX.md",))

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == "# Codex\n\nKeep tests green.\n"


def test_apply_diff_rejects_unallowlisted_target_before_mutating_repo(tmp_path: Path):
    repo = _init_repo(tmp_path)
    diff_path = tmp_path / "unexpected-target.diff"
    diff_path.write_text(
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1,2 @@\n"
        " # Readme\n"
        "+Unexpected instruction write.\n",
        encoding="utf-8",
    )

    with pytest.raises(VcsStateError, match="diff targets are not allowed: README.md"):
        VcsAdapter(repo).apply_diff(diff_path, allowed_paths=("CODEX.md",))

    assert (repo / "README.md").read_text(encoding="utf-8") == "# Readme\n"


def test_apply_diff_requires_explicit_target_allowlist(tmp_path: Path):
    repo = _init_repo(tmp_path)
    diff_path = tmp_path / "change.diff"
    diff_path.write_text(
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,3 +1,4 @@\n"
        " # Codex\n"
        " \n"
        " Keep tests green.\n"
        "+Record rollback notes.\n",
        encoding="utf-8",
    )

    with pytest.raises(VcsStateError, match="diff target allowlist is empty"):
        VcsAdapter(repo).apply_diff(diff_path, allowed_paths=())


def test_revert_commit_converts_git_conflict_to_vcs_state_error(tmp_path: Path):
    repo = _init_repo(tmp_path)
    adapter = VcsAdapter(repo)
    diff_path = tmp_path / "change.diff"
    diff_path.write_text(
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,3 +1,4 @@\n"
        " # Codex\n"
        " \n"
        " Keep tests green.\n"
        "+Record rollback notes.\n",
        encoding="utf-8",
    )
    adapter.apply_diff(diff_path, allowed_paths=("CODEX.md",))
    applied = adapter.commit_files(("CODEX.md",), "apply tugboat candidate")
    (repo / "CODEX.md").write_text(
        "# Codex\n\nKeep tests green.\nRecord rollback notes and keep them.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "CODEX.md")
    _git(repo, "commit", "-m", "intervening edit")

    with pytest.raises(VcsStateError, match="git revert failed"):
        adapter.revert_commit(branch_name=adapter.current_branch(), commit_sha=applied)

    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert (
        repo / "CODEX.md"
    ).read_text(encoding="utf-8") == (
        "# Codex\n\nKeep tests green.\nRecord rollback notes and keep them.\n"
    )
