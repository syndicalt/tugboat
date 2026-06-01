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


@dataclass(frozen=True)
class RetentionScan:
    expired_candidates: tuple[Path, ...]
    redaction_scan_paths: tuple[Path, ...]


class RetentionScanBudgetExceeded(ValueError):
    pass


def apply_retention_policy(repo: Path, policy: Policy, *, dry_run: bool = True) -> RetentionResult:
    repo = repo.resolve()
    runs_root = runs_dir(repo).resolve()
    scan = _scan_retention_artifacts(
        repo,
        runs_root=runs_root,
        raw_trace_days=policy.raw_traces_retention_days,
        checkpoint_days=policy.checkpoints_retention_days,
        max_scan_files=policy.retention_scan_file_budget,
    )
    redaction_candidates = _redaction_candidates(repo, scan.redaction_scan_paths)
    deleted: list[str] = []
    if not dry_run:
        for path in scan.expired_candidates:
            path.unlink(missing_ok=True)
            deleted.append(_relative(repo, path))
    return RetentionResult(
        candidates=tuple(_relative(repo, path) for path in scan.expired_candidates),
        deleted=tuple(deleted),
        redaction_candidates=redaction_candidates,
    )


def export_redacted_artifacts(
    repo: Path,
    output_dir: Path,
    *,
    scan_file_budget: int | None = None,
) -> RedactionExportResult:
    repo = repo.resolve()
    output_dir = output_dir.resolve()
    _require_outside_sidecar(repo, output_dir, "redaction output")
    policy = Policy()
    scan = _scan_retention_artifacts(
        repo,
        runs_root=runs_dir(repo).resolve(),
        raw_trace_days=policy.raw_traces_retention_days,
        checkpoint_days=policy.checkpoints_retention_days,
        max_scan_files=(
            policy.retention_scan_file_budget if scan_file_budget is None else scan_file_budget
        ),
    )
    redaction_candidates = _redaction_candidates(repo, scan.redaction_scan_paths)
    exported: list[str] = []
    for source in scan.redaction_scan_paths:
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


def _scan_retention_artifacts(
    repo: Path,
    *,
    runs_root: Path,
    raw_trace_days: int,
    checkpoint_days: int,
    max_scan_files: int,
) -> RetentionScan:
    if not runs_root.exists():
        return RetentionScan(expired_candidates=(), redaction_scan_paths=())
    now = time.time()
    expired_candidates: list[Path] = []
    redaction_paths: dict[str, Path] = {}
    count = 0
    for path in runs_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        count += 1
        if count > max_scan_files:
            raise RetentionScanBudgetExceeded(
                f"scan budget exceeded: found more than {max_scan_files} files under .sidecar/runs"
            )
        try:
            path.relative_to(runs_root)
        except ValueError:
            continue
        relative = _relative(repo, path)
        age_days = (now - path.stat().st_mtime) / (24 * 60 * 60)
        if path.name in RAW_TRACE_FILES and age_days > raw_trace_days:
            expired_candidates.append(path)
        elif (path.name in CHECKPOINT_FILES or path.name.startswith("checkpoint")) and age_days > checkpoint_days:
            expired_candidates.append(path)
        if (
            path.name in RAW_TRACE_FILES
            or path.name in CHECKPOINT_FILES
            or path.name.startswith("checkpoint")
            or path.name in REDACTABLE_RETAINED_ARTIFACTS
        ):
            redaction_paths[relative] = path
    for path in expired_candidates:
        redaction_paths[_relative(repo, path)] = path
    return RetentionScan(
        expired_candidates=tuple(sorted(expired_candidates, key=lambda item: _relative(repo, item))),
        redaction_scan_paths=tuple(redaction_paths[key] for key in sorted(redaction_paths)),
    )


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
