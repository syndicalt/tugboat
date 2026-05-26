from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from tugboat.models import Policy
from tugboat.paths import runs_dir


RAW_TRACE_FILES = frozenset({"trace-input.jsonl", "llmff-trace.jsonl"})
CHECKPOINT_FILES = frozenset({"events.jsonl", "llmff-events.jsonl"})


@dataclass(frozen=True)
class RetentionResult:
    candidates: tuple[str, ...]
    deleted: tuple[str, ...]


def apply_retention_policy(repo: Path, policy: Policy, *, dry_run: bool = True) -> RetentionResult:
    repo = repo.resolve()
    candidates = _expired_runtime_artifacts(
        repo,
        raw_trace_days=policy.raw_traces_retention_days,
        checkpoint_days=policy.checkpoints_retention_days,
    )
    deleted: list[str] = []
    if not dry_run:
        for path in candidates:
            path.unlink(missing_ok=True)
            deleted.append(_relative(repo, path))
    return RetentionResult(
        candidates=tuple(_relative(repo, path) for path in candidates),
        deleted=tuple(deleted),
    )


def _expired_runtime_artifacts(
    repo: Path,
    *,
    raw_trace_days: int,
    checkpoint_days: int,
) -> tuple[Path, ...]:
    root = runs_dir(repo)
    if not root.exists():
        return ()
    now = time.time()
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        age_days = (now - path.stat().st_mtime) / (24 * 60 * 60)
        if path.name in RAW_TRACE_FILES and age_days > raw_trace_days:
            candidates.append(path)
        elif (path.name in CHECKPOINT_FILES or path.name.startswith("checkpoint")) and age_days > checkpoint_days:
            candidates.append(path)
    return tuple(sorted(candidates, key=lambda item: _relative(repo, item)))


def _relative(repo: Path, path: Path) -> str:
    return path.resolve().relative_to(repo).as_posix()
