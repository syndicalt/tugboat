from __future__ import annotations

from tugboat.auto_apply import (
    AutoApplyCandidate,
    AutoApplyConfirmation,
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
            vcs_proof=VcsProof(
                mode="proposal_only",
                commit_sha=None,
                branch_name=None,
                rollback_commands=(),
            ),
        ),
        readiness=_ready(burn_in_days=12),
    )

    assert decision.eligible is False
    assert decision.approval_bundle is None
    assert decision.reasons == (
        "burn_in_period_too_short",
        "repository_not_allowlisted",
        "change_class_not_allowed",
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
        "vcs": {
            "branch_name": "tugboat/candidate-123",
            "commit_sha": None,
            "mode": "branch",
        },
    }
