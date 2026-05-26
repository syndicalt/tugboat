from __future__ import annotations

import json

from tugboat.db import Store
from tugboat.optimization import (
    BoundedEdit,
    LearningRateBudget,
    OptimizationCandidate,
    OptimizationMemory,
    OptimizationRun,
    ReflectionArtifact,
    ScoreSet,
    ValidationBaselineRecord,
    build_minibatches,
    budget_reasons_for_bounded_edit_metadata,
    evaluate_candidate,
    reflect_on_minibatch,
    rank_candidates,
)
from tugboat.paths import sidecar_dir


def test_build_minibatches_keeps_triggering_and_held_out_episodes_separate():
    batches = build_minibatches(
        train_episodes=("train-1", "train-2"),
        held_out_episodes=("held-1",),
        unseen_suites=("governance",),
    )

    assert batches.train_episodes == ("train-1", "train-2")
    assert batches.held_out_episodes == ("held-1",)
    assert batches.unseen_suites == ("governance",)


def test_candidate_is_accepted_only_when_held_out_improves_and_governance_passes():
    candidate = OptimizationCandidate(
        candidate_id="cand-1",
        edits=(BoundedEdit("add", "CODEX.md", "Testing", changed_lines=2, normative_changes=0),),
        trigger_score=ScoreSet(behavior=0.4, regression=0.0, governance_passed=True),
        held_out_score=ScoreSet(behavior=0.7, regression=0.0, governance_passed=True),
    )

    decision = evaluate_candidate(candidate, baseline=ScoreSet(0.5, 0.0, True))

    assert decision.accepted is True
    assert decision.reasons == ("held_out_improved",)
    assert decision.operator_metadata == ({"operator": "add", "file": "CODEX.md", "section": "Testing"},)


def test_candidate_is_rejected_when_regression_degrades_or_governance_fails():
    candidate = OptimizationCandidate(
        candidate_id="cand-1",
        edits=(BoundedEdit("replace", "CODEX.md", "Safety", changed_lines=3, normative_changes=1),),
        trigger_score=ScoreSet(behavior=0.8, regression=0.0, governance_passed=True),
        held_out_score=ScoreSet(behavior=0.9, regression=0.3, governance_passed=False),
    )

    decision = evaluate_candidate(candidate, baseline=ScoreSet(0.5, 0.0, True), regression_tolerance=0.1)

    assert decision.accepted is False
    assert decision.reasons == ("regression_degraded", "governance_failed")


def test_learning_rate_budget_rejects_oversized_candidate():
    candidate = OptimizationCandidate(
        candidate_id="cand-1",
        edits=(
            BoundedEdit("add", "CODEX.md", "One", changed_lines=3, normative_changes=1),
            BoundedEdit("delete", "AGENTS.md", "Two", changed_lines=3, normative_changes=1),
        ),
        trigger_score=ScoreSet(0.8, 0.0, True),
        held_out_score=ScoreSet(0.9, 0.0, True),
    )

    decision = evaluate_candidate(
        candidate,
        baseline=ScoreSet(0.5, 0.0, True),
        budget=LearningRateBudget(max_files_touched=1, max_changed_lines=4, max_normative_changes=1),
    )

    assert decision.accepted is False
    assert decision.reasons == (
        "max_files_touched_exceeded",
        "max_changed_lines_exceeded",
        "max_normative_changes_exceeded",
    )


def test_learning_rate_budget_evaluates_bounded_edit_metadata_directly():
    reasons = budget_reasons_for_bounded_edit_metadata(
        (
            {
                "operator": "add",
                "file": "CODEX.md",
                "section": "Testing",
                "changed_lines": 3,
                "normative_changes": 1,
            },
            {
                "operator": "delete",
                "file": "AGENTS.md",
                "section": "Approval",
                "changed_lines": 3,
                "normative_changes": 1,
            },
        ),
        budget=LearningRateBudget(
            max_files_touched=1,
            max_changed_lines=4,
            max_normative_changes=1,
        ),
    )

    assert reasons == (
        "max_files_touched_exceeded",
        "max_changed_lines_exceeded",
        "max_normative_changes_exceeded",
    )


def test_learning_rate_budget_rejects_too_many_sections_touched():
    candidate = OptimizationCandidate(
        candidate_id="cand-1",
        edits=(
            BoundedEdit("add", "CODEX.md", "Testing", changed_lines=1, normative_changes=0),
            BoundedEdit("annotate", "CODEX.md", "Review", changed_lines=1, normative_changes=0),
        ),
        trigger_score=ScoreSet(0.8, 0.0, True),
        held_out_score=ScoreSet(0.9, 0.0, True),
    )

    decision = evaluate_candidate(
        candidate,
        baseline=ScoreSet(0.5, 0.0, True),
        budget=LearningRateBudget(max_sections_touched=1),
    )

    assert decision.accepted is False
    assert decision.reasons == ("max_sections_touched_exceeded",)


def test_learning_rate_budget_enforces_operator_specific_risk_limits():
    candidate = OptimizationCandidate(
        candidate_id="cand-1",
        edits=(
            BoundedEdit("delete", "CODEX.md", "One", changed_lines=1, normative_changes=1),
            BoundedEdit("delete", "CODEX.md", "Two", changed_lines=1, normative_changes=1),
        ),
        trigger_score=ScoreSet(0.8, 0.0, True),
        held_out_score=ScoreSet(0.9, 0.0, True),
    )

    decision = evaluate_candidate(
        candidate,
        baseline=ScoreSet(0.5, 0.0, True),
        budget=LearningRateBudget(operator_risk_limits={"delete": 1}),
    )

    assert decision.accepted is False
    assert decision.reasons == ("operator_risk_limit_exceeded:delete",)


def test_rank_candidates_merges_compatible_accepted_edits_within_budget():
    run = OptimizationRun(
        baseline=ScoreSet(0.5, 0.0, True),
        budget=LearningRateBudget(max_files_touched=1, max_changed_lines=5, max_normative_changes=2),
        candidates=(
            OptimizationCandidate(
                candidate_id="testing",
                edits=(BoundedEdit("annotate", "CODEX.md", "Testing", 1, 0),),
                trigger_score=ScoreSet(0.6, 0.0, True),
                held_out_score=ScoreSet(0.8, 0.0, True),
            ),
            OptimizationCandidate(
                candidate_id="review",
                edits=(BoundedEdit("add", "CODEX.md", "Review", 2, 1),),
                trigger_score=ScoreSet(0.6, 0.0, True),
                held_out_score=ScoreSet(0.7, 0.0, True),
            ),
            OptimizationCandidate(
                candidate_id="harmful",
                edits=(BoundedEdit("demote", "CODEX.md", "Approval", 1, 1),),
                trigger_score=ScoreSet(0.8, 0.0, True),
                held_out_score=ScoreSet(0.9, 0.0, False),
            ),
        ),
    )

    ranked = rank_candidates(run)

    assert ranked[0].candidate_id == "merged:testing+review"
    assert ranked[0].accepted is True
    assert ranked[0].reasons == ("held_out_improved", "compatible_edits_merged")
    assert ranked[0].operator_metadata == (
        {"operator": "annotate", "file": "CODEX.md", "section": "Testing"},
        {"operator": "add", "file": "CODEX.md", "section": "Review"},
    )
    assert [decision.candidate_id for decision in ranked[1:]] == ["harmful"]


def test_rejected_edit_memory_suppresses_later_matching_candidates():
    memory = OptimizationMemory()
    rejected = OptimizationCandidate(
        candidate_id="bad",
        edits=(BoundedEdit("delete", "CODEX.md", "Approval", changed_lines=1, normative_changes=1),),
        trigger_score=ScoreSet(0.5, 0.0, True),
        held_out_score=ScoreSet(0.4, 0.0, True),
    )
    memory.record_rejection(rejected, reason="held_out_not_improved", source_refs=("ev_1",))
    later = OptimizationCandidate(
        candidate_id="later",
        edits=(BoundedEdit("delete", "CODEX.md", "Approval", changed_lines=1, normative_changes=1),),
        trigger_score=ScoreSet(0.7, 0.0, True),
        held_out_score=ScoreSet(0.8, 0.0, True),
    )

    decision = evaluate_candidate(later, baseline=ScoreSet(0.5, 0.0, True), memory=memory)

    assert decision.accepted is False
    assert decision.reasons == ("suppressed_by_rejected_edit_memory",)


def test_fixture_benchmark_accepts_one_improvement_and_rejects_one_harmful_edit():
    run = OptimizationRun(
        baseline=ScoreSet(0.5, 0.0, True),
        candidates=(
            OptimizationCandidate(
                candidate_id="good",
                edits=(BoundedEdit("annotate", "CODEX.md", "Testing", 1, 0),),
                trigger_score=ScoreSet(0.6, 0.0, True),
                held_out_score=ScoreSet(0.7, 0.0, True),
            ),
            OptimizationCandidate(
                candidate_id="bad",
                edits=(BoundedEdit("demote", "CODEX.md", "Approval", 1, 1),),
                trigger_score=ScoreSet(0.8, 0.0, True),
                held_out_score=ScoreSet(0.9, 0.0, False),
            ),
        ),
    )

    ranked = rank_candidates(run)

    assert [decision.candidate_id for decision in ranked if decision.accepted] == ["good"]
    assert [decision.candidate_id for decision in ranked if not decision.accepted] == ["bad"]


def test_reflection_artifact_summarizes_successes_and_failures_with_root_cause():
    artifact = reflect_on_minibatch(
        failure_patterns=("skipped regression test", "skipped regression test"),
        success_patterns=("used TDD",),
        affected_instruction_chunks=("CODEX.md#testing",),
        proposed_root_cause="Testing guidance is too implicit.",
    )

    assert artifact == ReflectionArtifact(
        recurring_failure_patterns=("skipped regression test",),
        preserved_success_patterns=("used TDD",),
        affected_instruction_chunks=("CODEX.md#testing",),
        proposed_root_cause="Testing guidance is too implicit.",
    )


def test_slow_update_memory_records_successful_and_rejected_directions():
    memory = OptimizationMemory()
    memory.record_slow_update("successful", "Specific regression-test wording improved held-out cases")
    memory.record_slow_update("rejected", "Do not weaken approval requirements")

    assert memory.slow_update_notes == [
        "successful: Specific regression-test wording improved held-out cases",
        "rejected: Do not weaken approval requirements",
    ]


def test_validation_baseline_memory_persists_to_optimizer_memory_table(tmp_path):
    repo = tmp_path

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        memory = OptimizationMemory()
        memory.validation_baselines["held-out"] = ValidationBaselineRecord(
            suite_id="held-out",
            held_out_score=0.82,
            candidate_id=7,
        )
        memory.persist(store, repo=repo)
        loaded = OptimizationMemory.load(store, repo=repo)
        row = store.connection.execute(
            """
            SELECT o.memory_type, o.key, o.payload_json, a.event_type
            FROM optimizer_memory o
            JOIN audit_events a ON a.sequence = o.audit_event_sequence
            """
        ).fetchone()

    assert row is not None
    assert row[0] == "validation_baseline"
    assert row[1] == "validation_baseline:held-out"
    assert json.loads(row[2]) == {
        "candidate_id": 7,
        "held_out_score": 0.82,
        "suite_id": "held-out",
    }
    assert row[3] == "optimizer_memory.recorded"
    assert loaded.validation_baselines["held-out"] == ValidationBaselineRecord(
        suite_id="held-out",
        held_out_score=0.82,
        candidate_id=7,
    )


def test_rejected_edit_memory_persists_to_optimizer_memory_table_with_audit_link(tmp_path):
    repo = tmp_path
    candidate = OptimizationCandidate(
        candidate_id="bad",
        edits=(BoundedEdit("delete", "CODEX.md", "Approval", 1, 1),),
        trigger_score=ScoreSet(0.5, 0.0, True),
        held_out_score=ScoreSet(0.4, 0.0, True),
    )

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        memory = OptimizationMemory.load(store, repo=repo)
        memory.record_rejection(candidate, reason="held_out_not_improved", source_refs=("ev_1",))
        memory.persist(store, repo=repo)
        row = store.connection.execute(
            """
            SELECT o.memory_type, o.key, o.payload_json, a.event_type
            FROM optimizer_memory o
            JOIN audit_events a ON a.sequence = o.audit_event_sequence
            """
        ).fetchone()

    assert row is not None
    assert row[0] == "rejected_edit"
    assert row[1] == candidate.edits[0].fingerprint
    assert json.loads(row[2]) == {
        "rejection_reason": "held_out_not_improved",
        "semantic_fingerprint": candidate.edits[0].fingerprint,
        "source_refs": ["ev_1"],
    }
    assert row[3] == "optimizer_memory.recorded"


def test_persisted_rejected_edit_memory_suppresses_later_matching_candidate(tmp_path):
    repo = tmp_path
    rejected = OptimizationCandidate(
        candidate_id="bad",
        edits=(BoundedEdit("delete", "CODEX.md", "Approval", 1, 1),),
        trigger_score=ScoreSet(0.5, 0.0, True),
        held_out_score=ScoreSet(0.4, 0.0, True),
    )
    later = OptimizationCandidate(
        candidate_id="later",
        edits=(BoundedEdit("delete", "CODEX.md", "Approval", 1, 1),),
        trigger_score=ScoreSet(0.7, 0.0, True),
        held_out_score=ScoreSet(0.8, 0.0, True),
    )

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        memory = OptimizationMemory()
        memory.record_rejection(rejected, reason="held_out_not_improved", source_refs=("ev_1",))
        memory.persist(store, repo=repo)
        loaded = OptimizationMemory.load(store, repo=repo)

    decision = evaluate_candidate(later, baseline=ScoreSet(0.5, 0.0, True), memory=loaded)

    assert decision.accepted is False
    assert decision.reasons == ("suppressed_by_rejected_edit_memory",)
