from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tugboat.db import Store


@dataclass(frozen=True)
class ScoreSet:
    behavior: float
    regression: float
    governance_passed: bool


@dataclass(frozen=True)
class BoundedEdit:
    operator: str
    file: str
    section: str
    changed_lines: int
    normative_changes: int

    @property
    def fingerprint(self) -> str:
        value = f"{self.operator}\n{self.file}\n{self.section}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def metadata(self) -> dict[str, str]:
        return {"operator": self.operator, "file": self.file, "section": self.section}


@dataclass(frozen=True)
class LearningRateBudget:
    max_files_touched: int = 2
    max_changed_lines: int = 20
    max_normative_changes: int = 2
    operator_risk_limits: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class OptimizationCandidate:
    candidate_id: str
    edits: tuple[BoundedEdit, ...]
    trigger_score: ScoreSet
    held_out_score: ScoreSet


@dataclass(frozen=True)
class OptimizationDecision:
    candidate_id: str
    accepted: bool
    reasons: tuple[str, ...]
    operator_metadata: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class EpisodeBatches:
    train_episodes: tuple[str, ...]
    held_out_episodes: tuple[str, ...]
    unseen_suites: tuple[str, ...]


@dataclass(frozen=True)
class RejectedEditRecord:
    semantic_fingerprint: str
    rejection_reason: str
    source_refs: tuple[str, ...]


@dataclass(frozen=True)
class ReflectionArtifact:
    recurring_failure_patterns: tuple[str, ...]
    preserved_success_patterns: tuple[str, ...]
    affected_instruction_chunks: tuple[str, ...]
    proposed_root_cause: str


@dataclass
class OptimizationMemory:
    rejected_edits: dict[str, RejectedEditRecord] = field(default_factory=dict)
    slow_update_notes: list[str] = field(default_factory=list)

    def record_rejection(
        self,
        candidate: OptimizationCandidate,
        *,
        reason: str,
        source_refs: tuple[str, ...],
    ) -> None:
        for edit in candidate.edits:
            self.rejected_edits[edit.fingerprint] = RejectedEditRecord(
                semantic_fingerprint=edit.fingerprint,
                rejection_reason=reason,
                source_refs=source_refs,
            )

    def suppresses(self, candidate: OptimizationCandidate) -> bool:
        return any(edit.fingerprint in self.rejected_edits for edit in candidate.edits)

    def record_slow_update(self, category: str, note: str) -> None:
        self.slow_update_notes.append(f"{category}: {note}")

    def persist(self, store: "Store", *, repo: Path) -> None:
        repo_path = str(repo)
        for fingerprint, record in sorted(self.rejected_edits.items()):
            payload = {
                "rejection_reason": record.rejection_reason,
                "semantic_fingerprint": record.semantic_fingerprint,
                "source_refs": list(record.source_refs),
            }
            store.record_optimizer_memory(
                repo_path=repo_path,
                memory_type="rejected_edit",
                key=fingerprint,
                payload=payload,
            )
        for index, note in enumerate(self.slow_update_notes):
            store.record_optimizer_memory(
                repo_path=repo_path,
                memory_type="slow_update",
                key=f"slow_update:{index}:{hashlib.sha256(note.encode('utf-8')).hexdigest()}",
                payload={"note": note},
            )

    @classmethod
    def load(cls, store: "Store", *, repo: Path) -> "OptimizationMemory":
        memory = cls()
        rows = store.connection.execute(
            """
            SELECT memory_type, key, payload_json
            FROM optimizer_memory
            WHERE repo_path = ?
            ORDER BY id
            """,
            (str(repo),),
        ).fetchall()
        for memory_type, key, payload_json in rows:
            payload = json.loads(str(payload_json))
            if memory_type == "rejected_edit":
                source_refs = payload.get("source_refs", [])
                if not isinstance(source_refs, list):
                    source_refs = []
                memory.rejected_edits[str(key)] = RejectedEditRecord(
                    semantic_fingerprint=str(payload["semantic_fingerprint"]),
                    rejection_reason=str(payload["rejection_reason"]),
                    source_refs=tuple(str(ref) for ref in source_refs),
                )
            elif memory_type == "slow_update":
                memory.slow_update_notes.append(str(payload["note"]))
        return memory


@dataclass(frozen=True)
class OptimizationRun:
    baseline: ScoreSet
    candidates: tuple[OptimizationCandidate, ...]
    memory: OptimizationMemory | None = None
    budget: LearningRateBudget = field(default_factory=LearningRateBudget)


def build_minibatches(
    *,
    train_episodes: tuple[str, ...],
    held_out_episodes: tuple[str, ...],
    unseen_suites: tuple[str, ...],
) -> EpisodeBatches:
    overlap = set(train_episodes) & set(held_out_episodes)
    if overlap:
        raise ValueError("train and held-out episodes must be separate")
    return EpisodeBatches(train_episodes, held_out_episodes, unseen_suites)


def reflect_on_minibatch(
    *,
    failure_patterns: tuple[str, ...],
    success_patterns: tuple[str, ...],
    affected_instruction_chunks: tuple[str, ...],
    proposed_root_cause: str,
) -> ReflectionArtifact:
    return ReflectionArtifact(
        recurring_failure_patterns=_unique(failure_patterns),
        preserved_success_patterns=_unique(success_patterns),
        affected_instruction_chunks=_unique(affected_instruction_chunks),
        proposed_root_cause=proposed_root_cause,
    )


def evaluate_candidate(
    candidate: OptimizationCandidate,
    *,
    baseline: ScoreSet,
    budget: LearningRateBudget | None = None,
    regression_tolerance: float = 0.0,
    memory: OptimizationMemory | None = None,
) -> OptimizationDecision:
    if memory is not None and memory.suppresses(candidate):
        return OptimizationDecision(candidate.candidate_id, False, ("suppressed_by_rejected_edit_memory",))

    reasons = [*_budget_reasons(candidate, budget or LearningRateBudget())]
    if candidate.held_out_score.behavior <= baseline.behavior:
        reasons.append("held_out_not_improved")
    if candidate.held_out_score.regression > baseline.regression + regression_tolerance:
        reasons.append("regression_degraded")
    if not candidate.held_out_score.governance_passed:
        reasons.append("governance_failed")

    if reasons:
        return OptimizationDecision(candidate.candidate_id, False, tuple(reasons))
    return OptimizationDecision(
        candidate.candidate_id,
        True,
        ("held_out_improved",),
        tuple(edit.metadata() for edit in candidate.edits),
    )


def rank_candidates(run: OptimizationRun) -> tuple[OptimizationDecision, ...]:
    candidates = sorted(
        run.candidates,
        key=lambda candidate: candidate.held_out_score.behavior,
        reverse=True,
    )
    evaluated = tuple(
        (
            candidate,
            evaluate_candidate(
                candidate,
                baseline=run.baseline,
                budget=run.budget,
                memory=run.memory,
            ),
        )
        for candidate in candidates
    )
    accepted = tuple(candidate for candidate, decision in evaluated if decision.accepted)
    rejected = tuple(decision for _, decision in evaluated if not decision.accepted)
    return (*_merge_accepted_candidates(accepted, run.budget), *rejected)


def _budget_reasons(candidate: OptimizationCandidate, budget: LearningRateBudget) -> tuple[str, ...]:
    reasons: list[str] = []
    files = {edit.file for edit in candidate.edits}
    changed_lines = sum(edit.changed_lines for edit in candidate.edits)
    normative_changes = sum(edit.normative_changes for edit in candidate.edits)
    operators = Counter(edit.operator for edit in candidate.edits)
    if len(files) > budget.max_files_touched:
        reasons.append("max_files_touched_exceeded")
    if changed_lines > budget.max_changed_lines:
        reasons.append("max_changed_lines_exceeded")
    if normative_changes > budget.max_normative_changes:
        reasons.append("max_normative_changes_exceeded")
    for operator, count in sorted(operators.items()):
        limit = budget.operator_risk_limits.get(operator)
        if limit is not None and count > limit:
            reasons.append(f"operator_risk_limit_exceeded:{operator}")
    return tuple(reasons)


def _merge_accepted_candidates(
    candidates: tuple[OptimizationCandidate, ...],
    budget: LearningRateBudget,
) -> tuple[OptimizationDecision, ...]:
    groups: list[list[OptimizationCandidate]] = []
    for candidate in candidates:
        for group in groups:
            merged = [*group, candidate]
            if _compatible(merged) and not _budget_reasons(_merged_candidate(merged), budget):
                group.append(candidate)
                break
        else:
            groups.append([candidate])
    return tuple(_decision_from_group(group) for group in groups)


def _compatible(candidates: list[OptimizationCandidate]) -> bool:
    edits = [edit for candidate in candidates for edit in candidate.edits]
    fingerprints = {edit.fingerprint for edit in edits}
    touched_sections = {(edit.file, edit.section) for edit in edits}
    return len(fingerprints) == len(edits) and len(touched_sections) == len(edits)


def _merged_candidate(candidates: list[OptimizationCandidate]) -> OptimizationCandidate:
    edits = tuple(edit for candidate in candidates for edit in candidate.edits)
    return OptimizationCandidate(
        candidate_id="+".join(candidate.candidate_id for candidate in candidates),
        edits=edits,
        trigger_score=candidates[0].trigger_score,
        held_out_score=candidates[0].held_out_score,
    )


def _decision_from_group(candidates: list[OptimizationCandidate]) -> OptimizationDecision:
    edits = tuple(edit for candidate in candidates for edit in candidate.edits)
    if len(candidates) == 1:
        candidate_id = candidates[0].candidate_id
        reasons = ("held_out_improved",)
    else:
        candidate_id = "merged:" + "+".join(candidate.candidate_id for candidate in candidates)
        reasons = ("held_out_improved", "compatible_edits_merged")
    return OptimizationDecision(
        candidate_id,
        True,
        reasons,
        tuple(edit.metadata() for edit in edits),
    )


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
