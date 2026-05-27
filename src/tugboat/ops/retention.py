from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from tugboat.models import Policy
from tugboat.paths import runs_dir
from tugboat.security.secrets import scan_text


RAW_TRACE_FILES = frozenset({"trace-input.jsonl", "trace-redacted.jsonl", "llmff-trace.jsonl"})
CHECKPOINT_FILES = frozenset({"events.jsonl", "llmff-events.jsonl"})


@dataclass(frozen=True)
class RetentionResult:
    candidates: tuple[str, ...]
    deleted: tuple[str, ...]
    redaction_candidates: tuple[dict[str, object], ...]


def apply_retention_policy(repo: Path, policy: Policy, *, dry_run: bool = True) -> RetentionResult:
    repo = repo.resolve()
    runs_root = runs_dir(repo).resolve()
    candidates = _expired_runtime_artifacts(
        repo,
        runs_root=runs_root,
        raw_trace_days=policy.raw_traces_retention_days,
        checkpoint_days=policy.checkpoints_retention_days,
    )
    redaction_candidates = _redaction_candidates(repo, candidates)
    deleted: list[str] = []
    if not dry_run:
        for path in candidates:
            path.unlink(missing_ok=True)
            deleted.append(_relative(repo, path))
    return RetentionResult(
        candidates=tuple(_relative(repo, path) for path in candidates),
        deleted=tuple(deleted),
        redaction_candidates=redaction_candidates,
    )


def _expired_runtime_artifacts(
    repo: Path,
    *,
    runs_root: Path,
    raw_trace_days: int,
    checkpoint_days: int,
) -> tuple[Path, ...]:
    if not runs_root.exists():
        return ()
    now = time.time()
    candidates: list[Path] = []
    for path in runs_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.relative_to(runs_root)
        except ValueError:
            continue
        age_days = (now - path.stat().st_mtime) / (24 * 60 * 60)
        if path.name in RAW_TRACE_FILES and age_days > raw_trace_days:
            candidates.append(path)
        elif (path.name in CHECKPOINT_FILES or path.name.startswith("checkpoint")) and age_days > checkpoint_days:
            candidates.append(path)
    return tuple(sorted(candidates, key=lambda item: _relative(repo, item)))


def _relative(repo: Path, path: Path) -> str:
    return path.relative_to(repo).as_posix()


def _redaction_candidates(
    repo: Path,
    candidates: tuple[Path, ...],
) -> tuple[dict[str, object], ...]:
    findings: list[dict[str, object]] = []
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative_path = _relative(repo, path)
        for finding in scan_text(relative_path, text):
            findings.append(
                {
                    "path": finding.path,
                    "line_number": finding.line_number,
                    "kind": finding.kind,
                }
            )
    return tuple(findings)
