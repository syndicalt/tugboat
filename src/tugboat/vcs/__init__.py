from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class VcsStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeCheck:
    clean: bool
    dirty_paths: tuple[str, ...]


@dataclass(frozen=True)
class RollbackMetadata:
    commit_sha: str
    branch_name: str
    files: tuple[str, ...]
    reason: str
    strategy: str = "git_revert"

    @property
    def commands(self) -> tuple[tuple[str, ...], ...]:
        return (
            ("git", "switch", self.branch_name),
            ("git", "revert", "--no-edit", self.commit_sha),
        )

    def to_json_dict(self) -> dict[str, object]:
        return {
            "branch_name": self.branch_name,
            "commands": [list(command) for command in self.commands],
            "commit_sha": self.commit_sha,
            "files": list(self.files),
            "reason": self.reason,
            "strategy": self.strategy,
        }


@dataclass(frozen=True)
class PullRequestMetadata:
    title: str
    body: str
    branch_name: str
    base_branch: str
    draft: bool = True

    def to_json_dict(self) -> dict[str, object]:
        return {
            "base_branch": self.base_branch,
            "body": self.body,
            "branch_name": self.branch_name,
            "draft": self.draft,
            "title": self.title,
        }


class VcsAdapter:
    def __init__(self, repo: Path):
        self.repo = repo

    def check_clean_worktree(self) -> WorktreeCheck:
        dirty_paths = self._dirty_paths()
        return WorktreeCheck(clean=not dirty_paths, dirty_paths=dirty_paths)

    def assert_clean_worktree(self) -> None:
        check = self.check_clean_worktree()
        if not check.clean:
            raise VcsStateError(f"worktree is dirty: {', '.join(check.dirty_paths)}")

    def assert_target_files_clean(self, target_files: tuple[str, ...]) -> None:
        targets = set(target_files)
        dirty_targets = tuple(path for path in self._dirty_paths() if path in targets)
        if dirty_targets:
            raise VcsStateError(f"target files are dirty: {', '.join(dirty_targets)}")

    def assert_base_hashes(self, expected_hashes: dict[str, str]) -> None:
        for relative_path, expected_hash in sorted(expected_hashes.items()):
            path = (self.repo / relative_path).resolve()
            if not _is_relative_to(path, self.repo.resolve()) or not path.is_file():
                raise VcsStateError(f"base file missing: {relative_path}")
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                raise VcsStateError(f"base hash mismatch for {relative_path}")

    def branch_name(self, *, run_id: str, candidate_id: int, base_file: str) -> str:
        return "/".join(
            (
                "tugboat",
                _slug(run_id),
                f"candidate-{candidate_id}",
                _slug(base_file),
            )
        )

    def commit_message(
        self, *, run_id: str, candidate_id: int, base_file: str, rationale: str
    ) -> str:
        lines = [
            f"tugboat: apply candidate {candidate_id} for {base_file}",
            "",
            f"Run: {run_id}",
            f"Candidate: {candidate_id}",
            f"Base file: {base_file}",
            "",
            "Rationale:",
        ]
        lines.extend(rationale.rstrip().splitlines())
        lines.append("")
        return "\n".join(lines)

    def rollback_metadata(
        self,
        *,
        commit_sha: str,
        branch_name: str,
        files: tuple[str, ...],
        reason: str,
    ) -> RollbackMetadata:
        return RollbackMetadata(
            commit_sha=commit_sha,
            branch_name=branch_name,
            files=tuple(files),
            reason=reason,
        )

    def current_branch(self) -> str:
        return self._git("branch", "--show-current")

    def pull_request_metadata(
        self,
        *,
        candidate_id: int,
        base_file: str,
        branch_name: str,
        base_branch: str,
        rationale: str,
    ) -> PullRequestMetadata:
        title = f"tugboat: apply candidate {candidate_id} for {base_file}"
        body = "\n".join(
            [
                f"Candidate: {candidate_id}",
                f"Base file: {base_file}",
                f"Branch: {branch_name}",
                "",
                "Rationale:",
                rationale.rstrip(),
                "",
                "This pull request was generated from Tugboat review artifacts.",
            ]
        )
        return PullRequestMetadata(
            title=title,
            body=body,
            branch_name=branch_name,
            base_branch=base_branch,
        )

    def create_branch(self, branch_name: str) -> None:
        self._git("switch", "-c", branch_name)

    def switch_branch(self, branch_name: str) -> None:
        self._git("switch", branch_name)

    def delete_branch(self, branch_name: str) -> None:
        self._git("branch", "-D", branch_name)

    def apply_diff(self, diff_path: Path) -> None:
        try:
            self._git("apply", str(diff_path))
        except subprocess.CalledProcessError as error:
            message = (error.stderr or error.stdout or "").strip()
            detail = f": {message}" if message else ""
            raise VcsStateError(f"git apply failed{detail}") from error

    def commit_files(self, files: tuple[str, ...], message: str) -> str:
        self._git("add", "--", *files)
        self._git("commit", "-m", message)
        return self._git("rev-parse", "HEAD")

    def revert_commit(self, *, branch_name: str, commit_sha: str) -> str:
        self._git("switch", branch_name)
        self._git("revert", "--no-edit", commit_sha)
        return self._git("rev-parse", "HEAD")

    def _dirty_paths(self) -> tuple[str, ...]:
        output = self._git("status", "--porcelain=v1", "--untracked-files=all")
        paths: set[str] = set()
        for line in output.splitlines():
            if not line:
                continue
            status_path = line[3:]
            if " -> " in status_path:
                old_path, new_path = status_path.split(" -> ", 1)
                paths.add(_unquote_status_path(old_path))
                paths.add(_unquote_status_path(new_path))
            else:
                paths.add(_unquote_status_path(status_path))
        return tuple(sorted(paths))

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return result.stdout.rstrip("\n")


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def _unquote_status_path(value: str) -> str:
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
