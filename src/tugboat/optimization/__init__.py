from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


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
    return tuple(
        evaluate_candidate(
            candidate,
            baseline=run.baseline,
            budget=run.budget,
            memory=run.memory,
        )
        for candidate in candidates
    )


def _budget_reasons(candidate: OptimizationCandidate, budget: LearningRateBudget) -> tuple[str, ...]:
    reasons: list[str] = []
    files = {edit.file for edit in candidate.edits}
    changed_lines = sum(edit.changed_lines for edit in candidate.edits)
    normative_changes = sum(edit.normative_changes for edit in candidate.edits)
    if len(files) > budget.max_files_touched:
        reasons.append("max_files_touched_exceeded")
    if changed_lines > budget.max_changed_lines:
        reasons.append("max_changed_lines_exceeded")
    if normative_changes > budget.max_normative_changes:
        reasons.append("max_normative_changes_exceeded")
    return tuple(reasons)


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
