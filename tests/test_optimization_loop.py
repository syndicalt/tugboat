from __future__ import annotations

import json

from tugboat.db import Store
from tugboat.optimization import (
    BoundedEdit,
    EpisodeOutcome,
    LearningRateBudget,
    OptimizationCandidate,
    OptimizationMemory,
    OptimizationRun,
    RejectedClusterRecord,
    ReflectionArtifact,
    ScoreSet,
    SuccessFailureMinibatch,
    ValidationBaselineRecord,
    build_minibatches,
    build_success_failure_minibatch,
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


def test_build_minibatches_rejects_train_held_out_overlap():
    try:
        build_minibatches(
            train_episodes=("ep-1",),
            held_out_episodes=("ep-1",),
            unseen_suites=("governance",),
        )
    except ValueError as error:
        assert str(error) == "train and held-out episodes must be separate"
    else:
        raise AssertionError("overlapping train and held-out episodes should be rejected")


def test_build_success_failure_minibatch_separates_outcomes_for_reflection():
    minibatch = build_success_failure_minibatch(
        (
            EpisodeOutcome("ep-1", "success", "used regression tests"),
            EpisodeOutcome("ep-2", "failure", "skipped regression tests"),
            EpisodeOutcome("ep-1", "success", "used regression tests"),
            EpisodeOutcome("ep-3", "failure", "skipped regression tests"),
            EpisodeOutcome("ep-4", "success", "used TDD"),
        )
    )

    assert minibatch == SuccessFailureMinibatch(
        success_episodes=("ep-1", "ep-4"),
        failure_episodes=("ep-2", "ep-3"),
        success_patterns=("used regression tests", "used TDD"),
        failure_patterns=("skipped regression tests",),
    )


def test_build_success_failure_minibatch_rejects_ambiguous_outcome_labels():
    try:
        build_success_failure_minibatch((EpisodeOutcome("ep-1", "unknown", "ambiguous"),))
    except ValueError as error:
        assert str(error) == "episode outcome must be success or failure: ep-1"
    else:
        raise AssertionError("ambiguous outcome label should be rejected")


def test_build_success_failure_minibatch_rejects_conflicting_episode_outcomes():
    try:
        build_success_failure_minibatch(
            (
                EpisodeOutcome("ep-1", "success", "used regression tests"),
                EpisodeOutcome("ep-1", "failure", "skipped regression tests"),
            )
        )
    except ValueError as error:
        assert str(error) == "episode cannot be both success and failure"
    else:
        raise AssertionError("conflicting episode outcomes should be rejected")


def test_bounded_edit_accepts_all_roadmap_operators():
    operators = ("add", "replace", "delete", "split", "merge", "demote", "promote", "annotate")

    edits = tuple(BoundedEdit(operator, "CODEX.md", "Testing", 1, 0) for operator in operators)

    assert tuple(edit.operator for edit in edits) == operators


def test_bounded_edit_rejects_unknown_operator():
    try:
        BoundedEdit("rewrite_everything", "CODEX.md", "Testing", 1, 0)
    except ValueError as error:
        assert str(error) == (
            "bounded edit operator must be one of: add, annotate, delete, demote, merge, promote, replace, split"
        )
    else:
        raise AssertionError("unknown bounded edit operator should be rejected")


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


def test_old_rejected_edit_memory_rows_load_with_default_suppression_signal(tmp_path):
    repo = tmp_path
    fingerprint = BoundedEdit("delete", "CODEX.md", "Approval", 1, 1).fingerprint

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="rejected_edit",
            key=fingerprint,
            payload={
                "rejection_reason": "held_out_not_improved",
                "semantic_fingerprint": fingerprint,
                "source_refs": "ev_1",
            },
        )
        loaded = OptimizationMemory.load(store, repo=repo)

    assert loaded.rejected_edits[fingerprint].future_proposal_suppression_signal == (
        "suppress_matching_bounded_edit_fingerprint"
    )
    assert loaded.rejected_edits[fingerprint].source_refs == ()


def test_structured_rejected_edit_memory_context_round_trips(tmp_path):
    repo = tmp_path
    fingerprint = BoundedEdit("add", "CODEX.md", "Rules", 1, 0).fingerprint

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="rejected_edit",
            key=fingerprint,
            payload={
                "category": "policy_regression",
                "failure_pattern": "duplicates existing guidance",
                "file": "CODEX.md",
                "future_proposal_suppression_signal": "suppress_matching_bounded_edit_fingerprint",
                "operator": "add",
                "rejection_reason": "redundant_rule",
                "review_actor": "reviewer",
                "review_template": "redundant-rule",
                "section": "Rules",
                "semantic_fingerprint": fingerprint,
                "source_refs": ["candidate:7", "suite:human_review"],
            },
        )
        loaded = OptimizationMemory.load(store, repo=repo)
        loaded.persist(store, repo=repo)
        reloaded = OptimizationMemory.load(store, repo=repo)

    record = reloaded.rejected_edits[fingerprint]
    assert record.operator == "add"
    assert record.file == "CODEX.md"
    assert record.section == "Rules"
    assert record.category == "policy_regression"
    assert record.failure_pattern == "duplicates existing guidance"
    assert record.review_actor == "reviewer"
    assert record.review_template == "redundant-rule"


def test_structured_rejected_cluster_memory_context_round_trips(tmp_path):
    repo = tmp_path

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="rejected_cluster",
            key="drift-1",
            payload={
                "category": "policy_regression",
                "cluster_id": "drift-1",
                "evidence_refs": ["ev-1", "ev-2"],
                "failure_pattern": "duplicates existing guidance",
                "rejection_reason": "redundant_rule",
                "review_actor": "reviewer",
                "review_template": "redundant-rule",
                "source_refs": ["candidate:7", "suite:human_review"],
            },
        )
        loaded = OptimizationMemory.load(store, repo=repo)
        loaded.persist(store, repo=repo)
        reloaded = OptimizationMemory.load(store, repo=repo)

    record = reloaded.rejected_clusters["drift-1"]
    assert record == RejectedClusterRecord(
        cluster_id="drift-1",
        rejection_reason="redundant_rule",
        source_refs=("candidate:7", "suite:human_review"),
        evidence_refs=("ev-1", "ev-2"),
        category="policy_regression",
        failure_pattern="duplicates existing guidance",
        review_actor="reviewer",
        review_template="redundant-rule",
    )


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
    memory.record_slow_update("optimizer_guidance", "Prefer annotate before replace")

    assert memory.slow_update_notes == [
        "successful: Specific regression-test wording improved held-out cases",
        "rejected: Do not weaken approval requirements",
        "optimizer_guidance: Prefer annotate before replace",
    ]
    assert [record.category for record in memory.slow_update_records] == [
        "successful",
        "rejected",
        "optimizer_guidance",
    ]
    assert [record.note for record in memory.slow_update_records] == [
        "Specific regression-test wording improved held-out cases",
        "Do not weaken approval requirements",
        "Prefer annotate before replace",
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


def test_slow_update_memory_persists_structured_records_to_optimizer_memory_table(tmp_path):
    repo = tmp_path

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        memory = OptimizationMemory()
        memory.record_slow_update("optimizer_guidance", "Prefer annotate before replace")
        memory.persist(store, repo=repo)
        loaded = OptimizationMemory.load(store, repo=repo)
        row = store.connection.execute(
            """
            SELECT o.memory_type, o.payload_json, a.event_type
            FROM optimizer_memory o
            JOIN audit_events a ON a.sequence = o.audit_event_sequence
            """
        ).fetchone()

    assert row is not None
    assert row[0] == "slow_update"
    assert json.loads(row[1]) == {
        "category": "optimizer_guidance",
        "legacy_note": "optimizer_guidance: Prefer annotate before replace",
        "note": "Prefer annotate before replace",
    }
    assert row[2] == "optimizer_memory.recorded"
    assert loaded.slow_update_notes == ["optimizer_guidance: Prefer annotate before replace"]
    assert [(record.category, record.note) for record in loaded.slow_update_records] == [
        ("optimizer_guidance", "Prefer annotate before replace")
    ]


def test_legacy_slow_update_memory_rows_load_as_structured_records(tmp_path):
    repo = tmp_path

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="slow_update",
            key="slow_update:legacy-success",
            payload={"note": "successful: Specific regression-test wording improved held-out cases"},
        )
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="slow_update",
            key="slow_update:legacy-freeform",
            payload={"note": "Prefer smaller edits"},
        )
        loaded = OptimizationMemory.load(store, repo=repo)

    assert loaded.slow_update_notes == [
        "successful: Specific regression-test wording improved held-out cases",
        "optimizer_guidance: Prefer smaller edits",
    ]
    assert [(record.category, record.note) for record in loaded.slow_update_records] == [
        ("successful", "Specific regression-test wording improved held-out cases"),
        ("optimizer_guidance", "Prefer smaller edits"),
    ]


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
        "future_proposal_suppression_signal": "suppress_matching_bounded_edit_fingerprint",
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
