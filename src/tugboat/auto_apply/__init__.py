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
SKILL_IMPROVEMENT_CATEGORIES = frozenset({"skill_improvement"})
REASON_ORDER = (
    "explicit_policy_required",
    "auto_apply_disabled",
    "auto_apply_repository_paused",
    "auto_apply_lane_disabled",
    "auto_apply_lane_paused",
    "auto_apply_category_paused",
    "auto_apply_incident_pause_active",
    "cli_confirmation_required",
    "cli_confirmation_policy_version_mismatch",
    "production_observation_period_too_short",
    "narrower_observation_risk_decision_required",
    "burn_in_period_too_short",
    "repository_not_allowlisted",
    "change_class_not_allowed",
    "auto_apply_change_type_not_allowed",
    "max_changed_lines_exceeded",
    "instruction_token_delta_missing",
    "max_instruction_token_delta_exceeded",
    "held_out_eval_failed",
    "governance_regression_failed",
    "skill_report_failed",
    "skill_held_out_behavior_failed",
    "rejection_rate_too_high",
    "rollback_rate_too_high",
    "vcs_backing_required",
    "one_command_rollback_required",
)
VCS_BACKED_MODES = frozenset({"branch", "commit"})


def _category_key(category: str) -> str:
    return category.strip().lower().replace("-", "_").replace(" ", "_")


@dataclass(frozen=True)
class AutoApplyLanePolicy:
    name: str
    enabled: bool
    allowed_categories: tuple[str, ...]
    allowed_change_classes: tuple[str, ...] = ("A",)
    max_changed_lines: int = 30
    max_instruction_token_delta: int = 50
    minimum_burn_in_days: int = 14
    maximum_rejection_rate: float = 0.10
    maximum_rollback_rate: float = 0.02

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _category_key(self.name))
        object.__setattr__(
            self,
            "allowed_categories",
            tuple(_category_key(category) for category in self.allowed_categories),
        )
        object.__setattr__(self, "allowed_change_classes", tuple(self.allowed_change_classes))


DEFAULT_AUTO_APPLY_LANES = (
    AutoApplyLanePolicy(
        name="docs_hygiene",
        enabled=True,
        allowed_categories=tuple(sorted(ALLOWED_CHANGE_CATEGORIES)),
        max_changed_lines=50,
        max_instruction_token_delta=50,
        minimum_burn_in_days=3,
        maximum_rejection_rate=0.20,
        maximum_rollback_rate=0.05,
    ),
    AutoApplyLanePolicy(
        name="skill_improvement",
        enabled=True,
        allowed_categories=tuple(sorted(SKILL_IMPROVEMENT_CATEGORIES)),
        max_changed_lines=30,
        max_instruction_token_delta=30,
        minimum_burn_in_days=7,
        maximum_rejection_rate=0.15,
        maximum_rollback_rate=0.03,
    ),
)


@dataclass(frozen=True)
class AutoApplyPolicy:
    enabled: bool = False
    version: int = 1
    allowed_repositories: tuple[str, ...] = field(default_factory=tuple)
    allowed_change_classes: tuple[str, ...] = ("A",)
    minimum_burn_in_days: int = 14
    production_observation_days: int = 30
    narrower_observation_risk_decision: str = ""
    observation_rollback_owner: str = ""
    maximum_rejection_rate: float = 0.10
    maximum_rollback_rate: float = 0.02
    max_changed_lines: int = 50
    max_instruction_token_delta: int = 50
    paused_repositories: tuple[str, ...] = field(default_factory=tuple)
    paused_lanes: tuple[str, ...] = field(default_factory=tuple)
    paused_categories: tuple[str, ...] = field(default_factory=tuple)
    pause_for_incident: bool = False
    lanes: tuple[AutoApplyLanePolicy, ...] = DEFAULT_AUTO_APPLY_LANES

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_repositories", tuple(self.allowed_repositories))
        object.__setattr__(self, "allowed_change_classes", tuple(self.allowed_change_classes))
        object.__setattr__(self, "paused_repositories", tuple(self.paused_repositories))
        object.__setattr__(
            self,
            "paused_lanes",
            tuple(_category_key(lane) for lane in self.paused_lanes),
        )
        object.__setattr__(
            self,
            "paused_categories",
            tuple(_category_key(category) for category in self.paused_categories),
        )
        object.__setattr__(self, "lanes", tuple(self.lanes))


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
    changed_lines: int
    instruction_token_delta: int | None
    vcs_proof: VcsProof
    skill_report_passed: bool = True
    skill_held_out_behavior_passed: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "categories", tuple(self.categories))


@dataclass(frozen=True)
class AutoApplyReadiness:
    burn_in_days: int
    policy: AutoApplyPolicy | None
    confirmation: AutoApplyConfirmation | None
    active_incidents: tuple["AutoApplyIncidentState", ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "active_incidents", tuple(self.active_incidents))


@dataclass(frozen=True)
class AutoApplyIncidentState:
    candidate_id: int
    event_type: str
    failure_kind: str
    incident: str
    artifact_valid: bool
    artifact_status: str

    def to_json_dict(self) -> dict[str, object]:
        return {
            "artifact_status": self.artifact_status,
            "artifact_valid": self.artifact_valid,
            "candidate_id": self.candidate_id,
            "event_type": self.event_type,
            "failure_kind": self.failure_kind,
            "incident": self.incident,
        }


@dataclass(frozen=True)
class AutoApplyApprovalBundle:
    actor: str
    candidate_id: str
    change_class: str
    policy_version: int
    repository: str
    rollback_command: tuple[str, ...]
    vcs: VcsProof
    lane: str

    def to_json_dict(self) -> dict[str, object]:
        return {
            "actor": self.actor,
            "candidate_id": self.candidate_id,
            "change_class": self.change_class,
            "policy_version": self.policy_version,
            "repository": self.repository,
            "rollback_command": list(self.rollback_command),
            "lane": self.lane,
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
    lane: str | None = None


def evaluate_auto_apply(
    *,
    candidate: AutoApplyCandidate,
    readiness: AutoApplyReadiness,
) -> AutoApplyDecision:
    policy = readiness.policy
    confirmation = readiness.confirmation
    found_reasons: set[str] = set()
    lane: AutoApplyLanePolicy | None = None

    if policy is None:
        found_reasons.add("explicit_policy_required")
    else:
        lane = _matching_lane(policy, candidate, include_disabled=True)
        if not policy.enabled:
            found_reasons.add("auto_apply_disabled")
        if lane is not None and not lane.enabled:
            found_reasons.add("auto_apply_lane_disabled")
        if candidate.repository not in policy.allowed_repositories:
            found_reasons.add("repository_not_allowlisted")
        if candidate.repository in policy.paused_repositories:
            found_reasons.add("auto_apply_repository_paused")
        if lane is not None and lane.enabled and lane.name in policy.paused_lanes:
            found_reasons.add("auto_apply_lane_paused")
        candidate_category_keys = {_category_key(category) for category in candidate.categories}
        if candidate_category_keys.intersection(policy.paused_categories):
            found_reasons.add("auto_apply_category_paused")
        if readiness.active_incidents:
            found_reasons.add("auto_apply_incident_pause_active")
        threshold_policy = lane if lane is not None else policy
        has_narrower_observation_approval = bool(
            policy.narrower_observation_risk_decision.strip()
            and policy.observation_rollback_owner.strip()
        )
        has_partial_narrower_observation_approval = bool(
            policy.narrower_observation_risk_decision.strip()
            or policy.observation_rollback_owner.strip()
        )
        if readiness.burn_in_days < policy.production_observation_days:
            if has_narrower_observation_approval:
                if readiness.burn_in_days < threshold_policy.minimum_burn_in_days:
                    found_reasons.add("burn_in_period_too_short")
            elif has_partial_narrower_observation_approval:
                found_reasons.add("narrower_observation_risk_decision_required")
                if readiness.burn_in_days < threshold_policy.minimum_burn_in_days:
                    found_reasons.add("burn_in_period_too_short")
            else:
                found_reasons.add("production_observation_period_too_short")
                if readiness.burn_in_days < threshold_policy.minimum_burn_in_days:
                    found_reasons.add("burn_in_period_too_short")
        elif readiness.burn_in_days < threshold_policy.minimum_burn_in_days:
            found_reasons.add("burn_in_period_too_short")
        if candidate.rejection_rate > threshold_policy.maximum_rejection_rate:
            found_reasons.add("rejection_rate_too_high")
        if candidate.rollback_rate > threshold_policy.maximum_rollback_rate:
            found_reasons.add("rollback_rate_too_high")
        if candidate.changed_lines > threshold_policy.max_changed_lines:
            found_reasons.add("max_changed_lines_exceeded")
        if candidate.instruction_token_delta is None:
            found_reasons.add("instruction_token_delta_missing")
        elif candidate.instruction_token_delta > min(
            policy.max_instruction_token_delta,
            threshold_policy.max_instruction_token_delta,
        ):
            found_reasons.add("max_instruction_token_delta_exceeded")

    if confirmation is None or not confirmation.confirmed or not confirmation.actor:
        found_reasons.add("cli_confirmation_required")
    elif policy is not None and confirmation.policy_version != policy.version:
        found_reasons.add("cli_confirmation_policy_version_mismatch")

    allowed_change_classes = lane.allowed_change_classes if lane is not None else ()
    if policy is not None and lane is None:
        allowed_change_classes = policy.allowed_change_classes
    if candidate.change_class != "A" or (
        policy is not None and candidate.change_class not in allowed_change_classes
    ):
        found_reasons.add("change_class_not_allowed")
    if policy is not None and lane is None:
        found_reasons.add("auto_apply_change_type_not_allowed")
    if not candidate.held_out_eval_passed:
        found_reasons.add("held_out_eval_failed")
    if not candidate.governance_regression_passed:
        found_reasons.add("governance_regression_failed")
    if "skill_improvement" in {_category_key(category) for category in candidate.categories}:
        if not candidate.skill_report_passed:
            found_reasons.add("skill_report_failed")
        if not candidate.skill_held_out_behavior_passed:
            found_reasons.add("skill_held_out_behavior_failed")
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
        return AutoApplyDecision(
            eligible=False,
            reasons=reasons,
            lane=lane.name if lane is not None else None,
        )

    if policy is None or confirmation is None or lane is None:
        raise AssertionError("eligible auto-apply requires policy and confirmation")
    return AutoApplyDecision(
        eligible=True,
        reasons=(),
        lane=lane.name,
        approval_bundle=AutoApplyApprovalBundle(
            actor=confirmation.actor,
            candidate_id=candidate.candidate_id,
            change_class=candidate.change_class,
            policy_version=policy.version,
            repository=candidate.repository,
            rollback_command=candidate.vcs_proof.rollback_commands[0],
            vcs=candidate.vcs_proof,
            lane=lane.name,
        ),
    )


def _matching_lane(
    policy: AutoApplyPolicy,
    candidate: AutoApplyCandidate,
    *,
    include_disabled: bool = False,
) -> AutoApplyLanePolicy | None:
    if candidate.change_class not in policy.allowed_change_classes:
        return None
    candidate_categories = {_category_key(category) for category in candidate.categories}
    for lane in policy.lanes:
        if not include_disabled and not lane.enabled:
            continue
        if candidate.change_class not in lane.allowed_change_classes:
            continue
        if candidate_categories.intersection(lane.allowed_categories):
            return lane
    return None


__all__ = [
    "AutoApplyApprovalBundle",
    "AutoApplyCandidate",
    "AutoApplyConfirmation",
    "AutoApplyIncidentState",
    "AutoApplyLanePolicy",
    "AutoApplyDecision",
    "AutoApplyPolicy",
    "AutoApplyReadiness",
    "ALLOWED_CHANGE_CATEGORIES",
    "DEFAULT_AUTO_APPLY_LANES",
    "FORBIDDEN_CATEGORIES",
    "VcsProof",
    "evaluate_auto_apply",
]
