from __future__ import annotations

from tugboat.auto_apply import (
    AutoApplyCandidate,
    AutoApplyConfirmation,
    AutoApplyLanePolicy,
    AutoApplyPolicy,
    AutoApplyReadiness,
    VcsProof,
    evaluate_auto_apply,
)


def _passing_candidate(**overrides: object) -> AutoApplyCandidate:
    values = {
        "candidate_id": "candidate-123",
        "repository": "allowed/repo",
        "change_class": "A",
        "categories": ("typo_fix",),
        "held_out_eval_passed": True,
        "governance_regression_passed": True,
        "rejection_rate": 0.02,
        "rollback_rate": 0.001,
        "changed_lines": 1,
        "instruction_token_delta": 0,
        "vcs_proof": VcsProof(
            mode="branch",
            commit_sha=None,
            branch_name="tugboat/candidate-123",
            rollback_commands=(("git", "switch", "main"),),
        ),
    }
    values.update(overrides)
    return AutoApplyCandidate(**values)


def _enabled_policy(**overrides: object) -> AutoApplyPolicy:
    values = {
        "enabled": True,
        "version": 9,
        "allowed_repositories": ("allowed/repo",),
    }
    values.update(overrides)
    return AutoApplyPolicy(**values)


def _confirmed(**overrides: object) -> AutoApplyConfirmation:
    values = {
        "confirmed": True,
        "actor": "operator@example.com",
        "policy_version": 9,
    }
    values.update(overrides)
    return AutoApplyConfirmation(**values)


def _ready(**overrides: object) -> AutoApplyReadiness:
    values = {
        "burn_in_days": 14,
        "policy": _enabled_policy(),
        "confirmation": _confirmed(),
    }
    values.update(overrides)
    return AutoApplyReadiness(**values)


def test_auto_apply_policy_defaults_off_and_requires_explicit_policy():
    assert AutoApplyPolicy().enabled is False
    assert AutoApplyPolicy().minimum_burn_in_days == 14
    assert AutoApplyPolicy().maximum_rejection_rate == 0.10
    assert AutoApplyPolicy().maximum_rollback_rate == 0.02
    assert AutoApplyPolicy().max_changed_lines == 50
    assert AutoApplyPolicy().max_instruction_token_delta == 50
    assert [lane.name for lane in AutoApplyPolicy().lanes] == [
        "docs_hygiene",
        "skill_improvement",
    ]

    without_policy = evaluate_auto_apply(
        candidate=_passing_candidate(),
        readiness=AutoApplyReadiness(
            burn_in_days=14,
            policy=None,
            confirmation=_confirmed(),
        ),
    )
    default_policy = evaluate_auto_apply(
        candidate=_passing_candidate(),
        readiness=AutoApplyReadiness(
            burn_in_days=14,
            policy=AutoApplyPolicy(),
            confirmation=_confirmed(policy_version=1),
        ),
    )

    assert without_policy.eligible is False
    assert without_policy.approval_bundle is None
    assert "explicit_policy_required" in without_policy.reasons
    assert default_policy.eligible is False
    assert "auto_apply_disabled" in default_policy.reasons


def test_auto_apply_requires_cli_confirmation_for_enabled_policy():
    missing_confirmation = evaluate_auto_apply(
        candidate=_passing_candidate(),
        readiness=_ready(confirmation=None),
    )
    version_mismatch = evaluate_auto_apply(
        candidate=_passing_candidate(),
        readiness=_ready(confirmation=_confirmed(policy_version=8)),
    )

    assert missing_confirmation.eligible is False
    assert "cli_confirmation_required" in missing_confirmation.reasons
    assert version_mismatch.eligible is False
    assert "cli_confirmation_policy_version_mismatch" in version_mismatch.reasons


def test_auto_apply_reports_all_auditable_ineligibility_reasons():
    decision = evaluate_auto_apply(
        candidate=_passing_candidate(
            repository="blocked/repo",
            change_class="B",
            held_out_eval_passed=False,
            governance_regression_passed=False,
            rejection_rate=0.25,
            rollback_rate=0.08,
            changed_lines=51,
            instruction_token_delta=51,
            vcs_proof=VcsProof(
                mode="proposal_only",
                commit_sha=None,
                branch_name=None,
                rollback_commands=(),
            ),
        ),
        readiness=_ready(burn_in_days=2),
    )

    assert decision.eligible is False
    assert decision.approval_bundle is None
    assert decision.reasons == (
        "burn_in_period_too_short",
        "repository_not_allowlisted",
        "change_class_not_allowed",
        "auto_apply_change_type_not_allowed",
        "max_changed_lines_exceeded",
        "max_instruction_token_delta_exceeded",
        "held_out_eval_failed",
        "governance_regression_failed",
        "rejection_rate_too_high",
        "rollback_rate_too_high",
        "vcs_backing_required",
        "one_command_rollback_required",
    )


def test_forbidden_categories_and_non_class_a_changes_are_never_eligible():
    forbidden_categories = (
        "memory_behavior",
        "approvals",
        "sandboxing",
        "network",
        "deployment",
        "secrets",
        "provider_routing",
        "sidecar_authority",
    )

    decision = evaluate_auto_apply(
        candidate=_passing_candidate(
            change_class="D",
            categories=forbidden_categories,
        ),
        readiness=_ready(
            policy=_enabled_policy(allowed_change_classes=("A", "B", "C", "D")),
        ),
    )

    assert decision.eligible is False
    assert "change_class_not_allowed" in decision.reasons
    assert tuple(
        reason
        for reason in decision.reasons
        if reason.startswith("forbidden_category:")
    ) == tuple(f"forbidden_category:{category}" for category in forbidden_categories)


def test_forbidden_category_blocks_even_when_docs_hygiene_lane_matches():
    decision = evaluate_auto_apply(
        candidate=_passing_candidate(categories=("typo_fix", "secrets")),
        readiness=_ready(burn_in_days=3),
    )

    assert decision.eligible is False
    assert decision.lane == "docs_hygiene"
    assert decision.reasons == ("forbidden_category:secrets",)


def test_auto_apply_requires_narrow_allowed_change_type():
    unsupported = evaluate_auto_apply(
        candidate=_passing_candidate(categories=("instruction_clarification",)),
        readiness=_ready(),
    )
    allowed = evaluate_auto_apply(
        candidate=_passing_candidate(categories=("broken_internal_link",)),
        readiness=_ready(),
    )

    assert unsupported.eligible is False
    assert unsupported.approval_bundle is None
    assert unsupported.reasons == ("auto_apply_change_type_not_allowed",)
    assert allowed.eligible is True
    assert allowed.lane == "docs_hygiene"


def test_docs_hygiene_lane_relaxes_small_safe_changes_without_cli_thresholds():
    decision = evaluate_auto_apply(
        candidate=_passing_candidate(changed_lines=50, rejection_rate=0.20, rollback_rate=0.05),
        readiness=_ready(burn_in_days=3),
    )

    assert decision.eligible is True
    assert decision.lane == "docs_hygiene"


def test_skill_improvement_lane_has_separate_thresholds():
    accepted = evaluate_auto_apply(
        candidate=_passing_candidate(
            categories=("skill_improvement",),
            changed_lines=30,
            rejection_rate=0.15,
            rollback_rate=0.03,
        ),
        readiness=_ready(burn_in_days=7),
    )
    too_large = evaluate_auto_apply(
        candidate=_passing_candidate(categories=("skill_improvement",), changed_lines=31),
        readiness=_ready(burn_in_days=7),
    )

    assert accepted.eligible is True
    assert accepted.lane == "skill_improvement"
    assert too_large.eligible is False
    assert too_large.reasons == ("max_changed_lines_exceeded",)


def test_auto_apply_blocks_candidates_that_exceed_token_growth_limit():
    at_limit = evaluate_auto_apply(
        candidate=_passing_candidate(instruction_token_delta=50),
        readiness=_ready(),
    )
    over_limit = evaluate_auto_apply(
        candidate=_passing_candidate(instruction_token_delta=51),
        readiness=_ready(),
    )
    reduction = evaluate_auto_apply(
        candidate=_passing_candidate(instruction_token_delta=-200),
        readiness=_ready(),
    )

    assert at_limit.eligible is True
    assert reduction.eligible is True
    assert over_limit.eligible is False
    assert over_limit.reasons == ("max_instruction_token_delta_exceeded",)


def test_auto_apply_blocks_when_token_growth_metrics_are_missing():
    decision = evaluate_auto_apply(
        candidate=_passing_candidate(instruction_token_delta=None),
        readiness=_ready(),
    )

    assert decision.eligible is False
    assert decision.reasons == ("instruction_token_delta_missing",)


def test_auto_apply_policy_pause_controls_block_by_repo_lane_category_and_incident():
    policy = _enabled_policy(
        paused_repositories=("allowed/repo",),
        paused_lanes=("docs_hygiene",),
        paused_categories=("typo_fix",),
        pause_for_incident=True,
    )

    decision = evaluate_auto_apply(
        candidate=_passing_candidate(),
        readiness=_ready(policy=policy, burn_in_days=3),
    )

    assert decision.eligible is False
    assert decision.lane == "docs_hygiene"
    assert decision.reasons == (
        "auto_apply_repository_paused",
        "auto_apply_lane_paused",
        "auto_apply_category_paused",
        "auto_apply_incident_pause_active",
    )


def test_auto_apply_lane_can_set_stricter_token_growth_limit():
    policy = _enabled_policy(
        lanes=(
            AutoApplyLanePolicy(
                name="docs_hygiene",
                enabled=True,
                allowed_categories=("typo_fix",),
                max_changed_lines=50,
                max_instruction_token_delta=5,
                minimum_burn_in_days=3,
                maximum_rejection_rate=0.20,
                maximum_rollback_rate=0.05,
            ),
        )
    )

    decision = evaluate_auto_apply(
        candidate=_passing_candidate(instruction_token_delta=6),
        readiness=_ready(policy=policy, burn_in_days=3),
    )

    assert decision.eligible is False
    assert decision.lane == "docs_hygiene"
    assert decision.reasons == ("max_instruction_token_delta_exceeded",)


def test_eligible_candidate_returns_approval_bundle_without_apply_execution():
    decision = evaluate_auto_apply(
        candidate=_passing_candidate(),
        readiness=_ready(),
    )

    assert decision.eligible is True
    assert decision.reasons == ()
    assert decision.approval_bundle is not None
    assert decision.approval_bundle.to_json_dict() == {
        "actor": "operator@example.com",
        "candidate_id": "candidate-123",
        "change_class": "A",
        "policy_version": 9,
        "repository": "allowed/repo",
        "rollback_command": ["git", "switch", "main"],
        "lane": "docs_hygiene",
        "vcs": {
            "branch_name": "tugboat/candidate-123",
            "commit_sha": None,
            "mode": "branch",
        },
    }
