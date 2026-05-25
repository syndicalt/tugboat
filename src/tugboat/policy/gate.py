from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from tugboat.models import Policy


DENIAL_REASON_ORDER = (
    "base_hash_mismatch",
    "base_file_outside_repo",
    "base_file_not_allowed",
    "max_changed_lines_exceeded",
    "modal_weakening",
    "new_external_endpoint",
    "single_untrusted_source",
    "prohibited_risk_class",
    "auto_apply_not_implemented_in_mvp",
)
PROHIBITED_RISK_CLASSES = frozenset(
    {
        "direct_instruction_mutation",
        "vcs_apply",
        "external_network",
        "credential_exposure",
        "secret_exposure",
    }
)
STRONG_MODALS = re.compile(r"\b(must|never|required|shall)\b", re.IGNORECASE)
WEAK_MODALS = re.compile(r"\b(should|may|can|could|optional|recommend)\b", re.IGNORECASE)
EXTERNAL_ENDPOINT = re.compile(r"https?://[^\s)>\"]+", re.IGNORECASE)


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    trusted: bool = False


@dataclass(frozen=True)
class CandidatePatch:
    audit_id: int
    base_file: str
    base_hash: str
    diff: str
    risk_class: str
    rationale: str
    sources: tuple[SourceRef, ...] = ()

    @staticmethod
    def hash_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def hash_file(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @property
    def diff_hash(self) -> str:
        return self.hash_text(self.diff)

    def to_json_dict(self) -> dict[str, object]:
        return {
            "audit_id": self.audit_id,
            "base_file": self.base_file,
            "base_hash": self.base_hash,
            "diff_hash": self.diff_hash,
            "rationale": self.rationale,
            "risk_class": self.risk_class,
            "sources": [
                {"source_id": source.source_id, "trusted": source.trusted}
                for source in self.sources
            ],
        }


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...]


def evaluate_candidate(repo: Path, policy: Policy, candidate: CandidatePatch) -> PolicyDecision:
    found_reasons: set[str] = set()
    repo_root = repo.resolve()
    base_path = (repo / candidate.base_file).resolve()
    if not _is_relative_to(base_path, repo_root):
        found_reasons.add("base_file_outside_repo")
    if not _is_allowed_base_file(candidate.base_file, policy):
        found_reasons.add("base_file_not_allowed")
    if not base_path.exists() or CandidatePatch.hash_file(base_path) != candidate.base_hash:
        found_reasons.add("base_hash_mismatch")
    if _changed_line_count(candidate.diff) > policy.auto_apply_max_changed_lines:
        found_reasons.add("max_changed_lines_exceeded")
    if _has_modal_weakening(candidate.diff):
        found_reasons.add("modal_weakening")
    if _has_new_external_endpoint(candidate.diff):
        found_reasons.add("new_external_endpoint")
    if len(candidate.sources) == 1 and not candidate.sources[0].trusted:
        found_reasons.add("single_untrusted_source")
    if candidate.risk_class in PROHIBITED_RISK_CLASSES:
        found_reasons.add("prohibited_risk_class")
    if policy.auto_apply_enabled:
        found_reasons.add("auto_apply_not_implemented_in_mvp")

    reasons = tuple(reason for reason in DENIAL_REASON_ORDER if reason in found_reasons)
    return PolicyDecision(allowed=not reasons, reasons=reasons)


def _has_modal_weakening(diff: str) -> bool:
    removed = [_diff_body(line) for line in diff.splitlines() if _is_removed_line(line)]
    added = [_diff_body(line) for line in diff.splitlines() if _is_added_line(line)]
    removed_strong = any(STRONG_MODALS.search(line) for line in removed)
    if not removed_strong:
        return False
    if not added:
        return True
    return any(WEAK_MODALS.search(line) for line in added)


def _has_new_external_endpoint(diff: str) -> bool:
    removed_endpoints = {
        endpoint
        for line in diff.splitlines()
        if _is_removed_line(line)
        for endpoint in EXTERNAL_ENDPOINT.findall(line)
    }
    added_endpoints = {
        endpoint
        for line in diff.splitlines()
        if _is_added_line(line)
        for endpoint in EXTERNAL_ENDPOINT.findall(line)
    }
    return bool(added_endpoints - removed_endpoints)


def _is_removed_line(line: str) -> bool:
    return line.startswith("-") and not line.startswith("---")


def _is_added_line(line: str) -> bool:
    return line.startswith("+") and not line.startswith("+++")


def _diff_body(line: str) -> str:
    return line[1:] if line else line


def _changed_line_count(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if _is_added_line(line) or _is_removed_line(line))


def _is_allowed_base_file(base_file: str, policy: Policy) -> bool:
    entries = policy.instruction_files
    allowed = {entry.path for entry in entries} or {
        "AGENTS.md",
        "CODEX.md",
        "CLAUDE.md",
        "SKILL.md",
    }
    return base_file in allowed


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
