from __future__ import annotations

from dataclasses import dataclass, field


FORBIDDEN_CATEGORIES = frozenset(
    {
        "approvals",
        "approval",
        "deployment",
        "memory_behavior",
        "network",
        "provider_routing",
        "sandboxing",
        "secrets",
        "sidecar_authority",
    }
)
ALLOWED_CHANGE_CATEGORIES = frozenset(
    {
        "broken_internal_link",
        "duplicate_sentence_removal",
        "formatting_normalization",
        "stale_command_reference",
        "typo_fix",
    }
)
REASON_ORDER = (
    "explicit_policy_required",
    "auto_apply_disabled",
    "cli_confirmation_required",
    "cli_confirmation_policy_version_mismatch",
    "burn_in_period_too_short",
    "repository_not_allowlisted",
    "change_class_not_allowed",
    "auto_apply_change_type_not_allowed",
    "held_out_eval_failed",
    "governance_regression_failed",
    "rejection_rate_too_high",
    "rollback_rate_too_high",
    "vcs_backing_required",
    "one_command_rollback_required",
)
VCS_BACKED_MODES = frozenset({"branch", "commit"})


@dataclass(frozen=True)
class AutoApplyPolicy:
    enabled: bool = False
    version: int = 1
    allowed_repositories: tuple[str, ...] = field(default_factory=tuple)
    allowed_change_classes: tuple[str, ...] = ("A",)
    minimum_burn_in_days: int = 14
    maximum_rejection_rate: float = 0.10
    maximum_rollback_rate: float = 0.02

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_repositories", tuple(self.allowed_repositories))
        object.__setattr__(self, "allowed_change_classes", tuple(self.allowed_change_classes))


@dataclass(frozen=True)
class AutoApplyConfirmation:
    confirmed: bool = False
    actor: str = ""
    policy_version: int | None = None


@dataclass(frozen=True)
class VcsProof:
    mode: str
    commit_sha: str | None
    branch_name: str | None
    rollback_commands: tuple[tuple[str, ...], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rollback_commands",
            tuple(tuple(command) for command in self.rollback_commands),
        )

    @property
    def is_backed(self) -> bool:
        if self.mode == "branch":
            return bool(self.branch_name)
        if self.mode == "commit":
            return bool(self.commit_sha)
        return False

    @property
    def has_one_command_rollback(self) -> bool:
        return len(self.rollback_commands) == 1 and bool(self.rollback_commands[0])


@dataclass(frozen=True)
class AutoApplyCandidate:
    candidate_id: str
    repository: str
    change_class: str
    categories: tuple[str, ...]
    held_out_eval_passed: bool
    governance_regression_passed: bool
    rejection_rate: float
    rollback_rate: float
    vcs_proof: VcsProof

    def __post_init__(self) -> None:
        object.__setattr__(self, "categories", tuple(self.categories))


@dataclass(frozen=True)
class AutoApplyReadiness:
    burn_in_days: int
    policy: AutoApplyPolicy | None
    confirmation: AutoApplyConfirmation | None


@dataclass(frozen=True)
class AutoApplyApprovalBundle:
    actor: str
    candidate_id: str
    change_class: str
    policy_version: int
    repository: str
    rollback_command: tuple[str, ...]
    vcs: VcsProof

    def to_json_dict(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "candidate_id": self.candidate_id,
            "change_class": self.change_class,
            "policy_version": self.policy_version,
            "repository": self.repository,
            "rollback_command": list(self.rollback_command),
            "vcs": {
                "branch_name": self.vcs.branch_name,
                "commit_sha": self.vcs.commit_sha,
                "mode": self.vcs.mode,
            },
        }


@dataclass(frozen=True)
class AutoApplyDecision:
    eligible: bool
    reasons: tuple[str, ...]
    approval_bundle: AutoApplyApprovalBundle | None = None


def evaluate_auto_apply(
    *,
    candidate: AutoApplyCandidate,
    readiness: AutoApplyReadiness,
) -> AutoApplyDecision:
    policy = readiness.policy
    confirmation = readiness.confirmation
    found_reasons: set[str] = set()

    if policy is None:
        found_reasons.add("explicit_policy_required")
    else:
        if not policy.enabled:
            found_reasons.add("auto_apply_disabled")
        if candidate.repository not in policy.allowed_repositories:
            found_reasons.add("repository_not_allowlisted")
        if readiness.burn_in_days < policy.minimum_burn_in_days:
            found_reasons.add("burn_in_period_too_short")
        if candidate.rejection_rate > policy.maximum_rejection_rate:
            found_reasons.add("rejection_rate_too_high")
        if candidate.rollback_rate > policy.maximum_rollback_rate:
            found_reasons.add("rollback_rate_too_high")

    if confirmation is None or not confirmation.confirmed or not confirmation.actor:
        found_reasons.add("cli_confirmation_required")
    elif policy is not None and confirmation.policy_version != policy.version:
        found_reasons.add("cli_confirmation_policy_version_mismatch")

    if candidate.change_class != "A" or (
        policy is not None and candidate.change_class not in policy.allowed_change_classes
    ):
        found_reasons.add("change_class_not_allowed")
    if not any(_category_key(category) in ALLOWED_CHANGE_CATEGORIES for category in candidate.categories):
        found_reasons.add("auto_apply_change_type_not_allowed")
    if not candidate.held_out_eval_passed:
        found_reasons.add("held_out_eval_failed")
    if not candidate.governance_regression_passed:
        found_reasons.add("governance_regression_failed")
    if candidate.vcs_proof.mode not in VCS_BACKED_MODES or not candidate.vcs_proof.is_backed:
        found_reasons.add("vcs_backing_required")
    if not candidate.vcs_proof.has_one_command_rollback:
        found_reasons.add("one_command_rollback_required")

    forbidden_reasons = tuple(
        f"forbidden_category:{category}"
        for category in candidate.categories
        if _category_key(category) in FORBIDDEN_CATEGORIES
    )
    ordered_reasons = tuple(reason for reason in REASON_ORDER if reason in found_reasons)
    reasons = ordered_reasons + forbidden_reasons
    if reasons:
        return AutoApplyDecision(eligible=False, reasons=reasons)

    if policy is None or confirmation is None:
        raise AssertionError("eligible auto-apply requires policy and confirmation")
    return AutoApplyDecision(
        eligible=True,
        reasons=(),
        approval_bundle=AutoApplyApprovalBundle(
            actor=confirmation.actor,
            candidate_id=candidate.candidate_id,
            change_class=candidate.change_class,
            policy_version=policy.version,
            repository=candidate.repository,
            rollback_command=candidate.vcs_proof.rollback_commands[0],
            vcs=candidate.vcs_proof,
        ),
    )


def _category_key(category: str) -> str:
    return category.strip().lower().replace("-", "_").replace(" ", "_")


__all__ = [
    "AutoApplyApprovalBundle",
    "AutoApplyCandidate",
    "AutoApplyConfirmation",
    "AutoApplyDecision",
    "AutoApplyPolicy",
    "AutoApplyReadiness",
    "ALLOWED_CHANGE_CATEGORIES",
    "FORBIDDEN_CATEGORIES",
    "VcsProof",
    "evaluate_auto_apply",
]
