from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tugboat.models import InstructionFilePolicy, Policy


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


def load_policy(repo: Path) -> Policy:
    path = repo / ".sidecar" / "policy.yaml"
    if not path.exists():
        return Policy(instruction_files=DEFAULT_INSTRUCTION_FILES)

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    auto_apply = raw.get("auto_apply", {}) or {}
    llmff = raw.get("llmff", {}) or {}
    mcp = raw.get("mcp", {}) or {}
    retention = raw.get("retention", {}) or {}
    roadmap = raw.get("roadmap", {}) or {}
    learning_rate_budget = roadmap.get("learning_rate_budget", {}) or {}
    entries = tuple(_as_instruction_file(item) for item in raw.get("instruction_files", []))

    allowed_manifest_hashes = llmff.get(
        "allowed_manifest_hashes",
        raw.get("allowed_manifest_hashes", []),
    )

    return Policy(
        version=int(raw.get("version", 1)),
        mode=str(raw.get("mode", "proposal_only")),
        instruction_files=entries or DEFAULT_INSTRUCTION_FILES,
        auto_apply_enabled=bool(auto_apply.get("enabled", False)),
        auto_apply_max_changed_lines=int(auto_apply.get("max_changed_lines", 20)),
        auto_apply_allowed_repositories=tuple(
            str(Path(item).expanduser().resolve())
            for item in auto_apply.get("allowed_repositories", [])
        ),
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
        llmff_binary=str(llmff.get("binary", "llmff")),
        llmff_require_inspect=bool(llmff.get("require_inspect", True)),
        llmff_allow_network=bool(llmff.get("allow_network", False)),
        llmff_timeout_ms=_as_non_negative_int(
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
        raw_traces_retention_days=_as_non_negative_days(
            retention.get("raw_traces_days", Policy().raw_traces_retention_days),
            "retention.raw_traces_days",
        ),
        checkpoints_retention_days=_as_non_negative_days(
            retention.get("checkpoints_days", Policy().checkpoints_retention_days),
            "retention.checkpoints_days",
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
