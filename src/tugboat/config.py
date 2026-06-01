from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tugboat.models import AutoApplyLaneConfig, InstructionFilePolicy, Policy


DEFAULT_INSTRUCTION_FILES = (
    InstructionFilePolicy("AGENTS.md", "repo_policy", 80, True),
    InstructionFilePolicy("CODEX.md", "agent_policy", 70, True),
    InstructionFilePolicy("CLAUDE.md", "agent_policy", 70, True),
    InstructionFilePolicy("SKILL.md", "skill", 60, False),
    InstructionFilePolicy(".codex/skills/**/SKILL.md", "skill", 60, False),
)


def _as_instruction_file(raw: dict[str, Any]) -> InstructionFilePolicy:
    return InstructionFilePolicy(
        path=str(raw["path"]),
        kind=str(raw.get("kind", "repo_policy")),
        precedence=int(raw.get("precedence", 50)),
        protected=bool(raw.get("protected", False)),
    )


def _as_non_negative_days(raw: Any, field_name: str) -> int:
    days = int(raw)
    if days < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return days


def _as_non_negative_int(raw: Any, field_name: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isdecimal():
        value = int(raw)
    else:
        raise ValueError(f"{field_name} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return value


def _as_positive_int(raw: Any, field_name: str) -> int:
    value = _as_non_negative_int(raw, field_name)
    if value == 0:
        raise ValueError(f"{field_name} must be positive")
    return value


def as_positive_version(raw: Any, field_name: str) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{field_name} must be a positive integer")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.isdecimal():
        value = int(raw)
    else:
        raise ValueError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _as_operator_risk_limits(raw: Any) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("roadmap.learning_rate_budget.operator_risk_limits must be a mapping")
    return {
        str(operator): _as_non_negative_int(
            limit,
            f"roadmap.learning_rate_budget.operator_risk_limits.{operator}",
        )
        for operator, limit in raw.items()
    }


def _as_risk_class_changed_line_budgets(raw: Any) -> dict[str, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("policy.risk_class_changed_line_budgets must be a mapping")
    return {
        str(risk_class): _as_non_negative_int(
            limit,
            f"policy.risk_class_changed_line_budgets.{risk_class}",
        )
        for risk_class, limit in raw.items()
    }


def _as_string_tuple(raw: Any, field_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list | tuple):
        raise ValueError(f"{field_name} must be a list")
    if not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field_name} entries must be strings")
    return tuple(raw)


def _as_non_empty_string_tuple(raw: Any, field_name: str) -> tuple[str, ...]:
    values = _as_string_tuple(raw, field_name)
    if not all(item.strip() for item in values):
        raise ValueError(f"{field_name} entries must be non-empty strings")
    return tuple(item.strip() for item in values)


def _as_auto_apply_lanes(raw: Any) -> tuple[AutoApplyLaneConfig, ...]:
    defaults_by_name = {lane.name: lane for lane in Policy().auto_apply_lanes}
    if raw is None:
        return Policy().auto_apply_lanes
    if not isinstance(raw, dict):
        raise ValueError("auto_apply.lanes must be a mapping")

    lanes: list[AutoApplyLaneConfig] = []
    for lane_name, lane_raw in raw.items():
        if not isinstance(lane_raw, dict):
            raise ValueError(f"auto_apply.lanes.{lane_name} must be a mapping")
        name = str(lane_name)
        default = defaults_by_name.get(name)
        lanes.append(
            AutoApplyLaneConfig(
                name=name,
                enabled=bool(lane_raw.get("enabled", default.enabled if default else False)),
                allowed_categories=_as_string_tuple(
                    lane_raw.get(
                        "allowed_categories",
                        list(default.allowed_categories) if default else [],
                    ),
                    f"auto_apply.lanes.{name}.allowed_categories",
                ),
                allowed_risk_classes=_as_string_tuple(
                    lane_raw.get(
                        "allowed_risk_classes",
                        list(default.allowed_risk_classes) if default else ["A"],
                    ),
                    f"auto_apply.lanes.{name}.allowed_risk_classes",
                ),
                max_changed_lines=_as_non_negative_int(
                    lane_raw.get(
                        "max_changed_lines",
                        default.max_changed_lines if default else 30,
                    ),
                    f"auto_apply.lanes.{name}.max_changed_lines",
                ),
                max_instruction_token_delta=_as_non_negative_int(
                    lane_raw.get(
                        "max_instruction_token_delta",
                        default.max_instruction_token_delta if default else 50,
                    ),
                    f"auto_apply.lanes.{name}.max_instruction_token_delta",
                ),
                minimum_burn_in_days=_as_non_negative_days(
                    lane_raw.get(
                        "minimum_burn_in_days",
                        default.minimum_burn_in_days if default else 14,
                    ),
                    f"auto_apply.lanes.{name}.minimum_burn_in_days",
                ),
                maximum_rejection_rate=float(
                    lane_raw.get(
                        "maximum_rejection_rate",
                        default.maximum_rejection_rate if default else 0.10,
                    )
                ),
                maximum_rollback_rate=float(
                    lane_raw.get(
                        "maximum_rollback_rate",
                        default.maximum_rollback_rate if default else 0.02,
                    )
                ),
            )
        )
    return tuple(lanes)


def load_policy(repo: Path) -> Policy:
    path = repo / ".sidecar" / "policy.yaml"
    if not path.exists():
        return Policy(instruction_files=DEFAULT_INSTRUCTION_FILES)

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(".sidecar/policy.yaml must contain a mapping")
    auto_apply = raw.get("auto_apply", {}) or {}
    llmff = raw.get("llmff", {}) or {}
    mcp = raw.get("mcp", {}) or {}
    provider_smoke = raw.get("provider_smoke", {}) or {}
    retention = raw.get("retention", {}) or {}
    roadmap = raw.get("roadmap", {}) or {}
    vcs = raw.get("vcs", {}) or {}
    pull_request = vcs.get("pull_request", {}) or {}
    learning_rate_budget = roadmap.get("learning_rate_budget", {}) or {}
    drift_cluster = roadmap.get("drift_cluster", {}) or {}
    entries = tuple(_as_instruction_file(item) for item in raw.get("instruction_files", []))

    allowed_manifest_hashes = llmff.get(
        "allowed_manifest_hashes",
        raw.get("allowed_manifest_hashes", []),
    )

    return Policy(
        version=as_positive_version(raw.get("version", 1), ".sidecar/policy.yaml version"),
        mode=str(raw.get("mode", "proposal_only")),
        instruction_files=entries or DEFAULT_INSTRUCTION_FILES,
        auto_apply_enabled=bool(auto_apply.get("enabled", False)),
        auto_apply_max_changed_lines=_as_non_negative_int(
            auto_apply.get("max_changed_lines", Policy().auto_apply_max_changed_lines),
            "auto_apply.max_changed_lines",
        ),
        auto_apply_max_instruction_token_delta=_as_non_negative_int(
            auto_apply.get(
                "max_instruction_token_delta",
                Policy().auto_apply_max_instruction_token_delta,
            ),
            "auto_apply.max_instruction_token_delta",
        ),
        auto_apply_allowed_repositories=tuple(
            str(Path(item).expanduser().resolve())
            for item in auto_apply.get("allowed_repositories", [])
        ),
        auto_apply_paused_repositories=tuple(
            str(Path(item).expanduser().resolve())
            for item in _as_string_tuple(
                auto_apply.get("paused_repositories", []),
                "auto_apply.paused_repositories",
            )
        ),
        auto_apply_allowed_risk_classes=_as_string_tuple(
            auto_apply.get(
                "allowed_risk_classes",
                list(Policy().auto_apply_allowed_risk_classes),
            ),
            "auto_apply.allowed_risk_classes",
        ),
        auto_apply_paused_lanes=_as_string_tuple(
            auto_apply.get("paused_lanes", []),
            "auto_apply.paused_lanes",
        ),
        auto_apply_paused_categories=_as_string_tuple(
            auto_apply.get("paused_categories", []),
            "auto_apply.paused_categories",
        ),
        auto_apply_pause_for_incident=bool(auto_apply.get("pause_for_incident", False)),
        auto_apply_lanes=_as_auto_apply_lanes(auto_apply.get("lanes")),
        roadmap_learning_rate_max_files_touched=_as_non_negative_int(
            learning_rate_budget.get(
                "max_files_touched",
                Policy().roadmap_learning_rate_max_files_touched,
            ),
            "roadmap.learning_rate_budget.max_files_touched",
        ),
        roadmap_learning_rate_max_sections_touched=_as_non_negative_int(
            learning_rate_budget.get(
                "max_sections_touched",
                Policy().roadmap_learning_rate_max_sections_touched,
            ),
            "roadmap.learning_rate_budget.max_sections_touched",
        ),
        roadmap_learning_rate_max_changed_lines=_as_non_negative_int(
            learning_rate_budget.get(
                "max_changed_lines",
                Policy().roadmap_learning_rate_max_changed_lines,
            ),
            "roadmap.learning_rate_budget.max_changed_lines",
        ),
        roadmap_learning_rate_max_normative_changes=_as_non_negative_int(
            learning_rate_budget.get(
                "max_normative_changes",
                Policy().roadmap_learning_rate_max_normative_changes,
            ),
            "roadmap.learning_rate_budget.max_normative_changes",
        ),
        roadmap_learning_rate_operator_risk_limits=_as_operator_risk_limits(
            learning_rate_budget.get("operator_risk_limits", {})
        ),
        roadmap_drift_cluster_max_evidence_refs=_as_positive_int(
            drift_cluster.get(
                "max_evidence_refs",
                Policy().roadmap_drift_cluster_max_evidence_refs,
            ),
            "roadmap.drift_cluster.max_evidence_refs",
        ),
        risk_class_changed_line_budgets=_as_risk_class_changed_line_budgets(
            raw.get("risk_class_changed_line_budgets", {})
        ),
        editable_headings=_as_string_tuple(raw.get("editable_headings", []), "editable_headings"),
        auto_apply_minimum_burn_in_days=_as_non_negative_days(
            auto_apply.get(
                "minimum_burn_in_days",
                Policy().auto_apply_minimum_burn_in_days,
            ),
            "auto_apply.minimum_burn_in_days",
        ),
        auto_apply_maximum_rejection_rate=float(
            auto_apply.get(
                "maximum_rejection_rate",
                Policy().auto_apply_maximum_rejection_rate,
            )
        ),
        auto_apply_maximum_rollback_rate=float(
            auto_apply.get(
                "maximum_rollback_rate",
                Policy().auto_apply_maximum_rollback_rate,
            )
        ),
        forbidden_terms=tuple(auto_apply.get("forbidden_terms", Policy().forbidden_terms)),
        llmff_binary=str(llmff.get("binary", Policy().llmff_binary)),
        llmff_require_inspect=bool(llmff.get("require_inspect", True)),
        llmff_allow_network=bool(llmff.get("allow_network", False)),
        llmff_timeout_ms=_as_positive_int(
            llmff.get("timeout_ms", Policy().llmff_timeout_ms),
            "llmff.timeout_ms",
        ),
        llmff_retry_attempts=_as_non_negative_int(
            llmff.get("retry_attempts", Policy().llmff_retry_attempts),
            "llmff.retry_attempts",
        ),
        llmff_retry_backoff_ms=_as_non_negative_int(
            llmff.get("retry_backoff_ms", Policy().llmff_retry_backoff_ms),
            "llmff.retry_backoff_ms",
        ),
        allowed_manifest_hashes=tuple(str(item) for item in allowed_manifest_hashes),
        llmff_allowed_providers=_as_non_empty_string_tuple(
            llmff.get("allowed_providers", []),
            "llmff.allowed_providers",
        ),
        vcs_pull_request_enabled=bool(pull_request.get("enabled", False)),
        vcs_pull_request_provider=str(
            pull_request.get("provider", Policy().vcs_pull_request_provider)
        ),
        vcs_pull_request_remote=str(
            pull_request.get("remote", Policy().vcs_pull_request_remote)
        ),
        vcs_pull_request_base_branch=str(
            pull_request.get("base_branch", Policy().vcs_pull_request_base_branch)
        ),
        vcs_pull_request_draft=bool(
            pull_request.get("draft", Policy().vcs_pull_request_draft)
        ),
        raw_traces_retention_days=_as_non_negative_days(
            retention.get("raw_traces_days", Policy().raw_traces_retention_days),
            "retention.raw_traces_days",
        ),
        checkpoints_retention_days=_as_non_negative_days(
            retention.get("checkpoints_days", Policy().checkpoints_retention_days),
            "retention.checkpoints_days",
        ),
        provider_smoke_enabled=bool(
            provider_smoke.get("enabled", Policy().provider_smoke_enabled)
        ),
        provider_smoke_provider=str(
            provider_smoke.get("provider", Policy().provider_smoke_provider)
        ),
        provider_smoke_command=str(
            provider_smoke.get("command", Policy().provider_smoke_command)
        ),
        mcp_allowed_repositories=tuple(
            str(Path(item).expanduser().resolve())
            for item in mcp.get("allowed_repositories", [])
        ),
        mcp_tool_policy={
            str(tool): str(decision)
            for tool, decision in (mcp.get("tool_policy", {}) or {}).items()
        },
    )
