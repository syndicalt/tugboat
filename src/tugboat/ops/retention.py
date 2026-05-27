from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from tugboat.models import Policy
from tugboat.paths import mark_private_file, runs_dir
from tugboat.security.redaction import redact_text
from tugboat.security.secrets import scan_text


RAW_TRACE_FILES = frozenset({"trace-input.jsonl", "trace-redacted.jsonl", "llmff-trace.jsonl"})
CHECKPOINT_FILES = frozenset({"events.jsonl", "llmff-events.jsonl"})
REDACTABLE_RETAINED_ARTIFACTS = frozenset(
    {
        "audit.json",
        "candidate.diff",
        "candidate.raw.json",
        "eval-report.json",
        "eval-report.raw.json",
        "optimization-summary.json",
        "report.md",
    }
)


@dataclass(frozen=True)
class RetentionResult:
    candidates: tuple[str, ...]
    deleted: tuple[str, ...]
    redaction_candidates: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class RedactionExportResult:
    output_dir: Path
    exported: tuple[str, ...]
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
    redaction_candidates = _redaction_candidates(repo, _redaction_scan_artifacts(repo, candidates))
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


def export_redacted_artifacts(repo: Path, output_dir: Path) -> RedactionExportResult:
    repo = repo.resolve()
    output_dir = output_dir.resolve()
    _require_outside_sidecar(repo, output_dir, "redaction output")
    candidates = _redaction_scan_artifacts(repo, ())
    redaction_candidates = _redaction_candidates(repo, candidates)
    exported: list[str] = []
    for source in candidates:
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = _relative(repo, source)
        target = output_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(redact_text(text), encoding="utf-8")
        mark_private_file(target)
        exported.append(relative)
    return RedactionExportResult(
        output_dir=output_dir,
        exported=tuple(sorted(exported)),
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


def _redaction_scan_artifacts(repo: Path, candidates: tuple[Path, ...]) -> tuple[Path, ...]:
    runs_root = runs_dir(repo).resolve()
    paths: dict[str, Path] = {_relative(repo, path): path for path in candidates}
    if runs_root.exists():
        for path in runs_root.rglob("*"):
            if path.is_symlink() or not path.is_file():
                continue
            if (
                path.name in RAW_TRACE_FILES
                or path.name in CHECKPOINT_FILES
                or path.name.startswith("checkpoint")
                or path.name in REDACTABLE_RETAINED_ARTIFACTS
            ):
                paths[_relative(repo, path)] = path
    return tuple(paths[key] for key in sorted(paths))


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


def _require_outside_sidecar(repo: Path, path: Path, label: str) -> None:
    sidecar = (repo / ".sidecar").resolve()
    if path == sidecar or path.is_relative_to(sidecar):
        raise ValueError(f"{label} must resolve outside .sidecar")
