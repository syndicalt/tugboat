from __future__ import annotations

import sys
from dataclasses import dataclass, field


DEFAULT_FIXTURE_LLMFF_BINARY = f"{sys.executable} -m tugboat.llmff.fixture_backend"


@dataclass(frozen=True)
class InstructionFilePolicy:
    path: str
    kind: str
    precedence: int
    protected: bool = False


@dataclass(frozen=True)
class ChunkRecord:
    heading_path: tuple[str, ...]
    anchor: str
    byte_start: int
    byte_end: int
    text_hash: str
    text: str


@dataclass(frozen=True)
class DocumentRecord:
    path: str
    kind: str
    precedence: int
    protected: bool
    hash: str
    mtime: float
    parser_version: str
    chunks: tuple[ChunkRecord, ...]


@dataclass(frozen=True)
class IndexResult:
    documents: tuple[DocumentRecord, ...]

    @property
    def indexed_count(self) -> int:
        return len(self.documents)


@dataclass(frozen=True)
class InstructionGraph:
    documents: tuple[DocumentRecord, ...]


@dataclass(frozen=True)
class AutoApplyLaneConfig:
    name: str
    enabled: bool
    allowed_categories: tuple[str, ...]
    allowed_risk_classes: tuple[str, ...] = ("A",)
    max_changed_lines: int = 30
    max_instruction_token_delta: int = 50
    minimum_burn_in_days: int = 14
    maximum_rejection_rate: float = 0.10
    maximum_rollback_rate: float = 0.02


DEFAULT_AUTO_APPLY_LANES = (
    AutoApplyLaneConfig(
        name="docs_hygiene",
        enabled=True,
        allowed_categories=(
            "broken_internal_link",
            "duplicate_sentence_removal",
            "formatting_normalization",
            "stale_command_reference",
            "typo_fix",
        ),
        max_changed_lines=50,
        max_instruction_token_delta=50,
        minimum_burn_in_days=3,
        maximum_rejection_rate=0.20,
        maximum_rollback_rate=0.05,
    ),
    AutoApplyLaneConfig(
        name="skill_improvement",
        enabled=True,
        allowed_categories=("skill_improvement",),
        max_changed_lines=30,
        max_instruction_token_delta=30,
        minimum_burn_in_days=7,
        maximum_rejection_rate=0.15,
        maximum_rollback_rate=0.03,
    ),
)


@dataclass(frozen=True)
class Policy:
    version: int = 1
    mode: str = "proposal_only"
    instruction_files: tuple[InstructionFilePolicy, ...] = field(default_factory=tuple)
    auto_apply_enabled: bool = False
    auto_apply_max_changed_lines: int = 50
    auto_apply_max_instruction_token_delta: int = 50
    auto_apply_allowed_repositories: tuple[str, ...] = field(default_factory=tuple)
    auto_apply_allowed_risk_classes: tuple[str, ...] = ("A",)
    auto_apply_paused_repositories: tuple[str, ...] = field(default_factory=tuple)
    auto_apply_paused_lanes: tuple[str, ...] = field(default_factory=tuple)
    auto_apply_paused_categories: tuple[str, ...] = field(default_factory=tuple)
    auto_apply_pause_for_incident: bool = False
    auto_apply_lanes: tuple[AutoApplyLaneConfig, ...] = DEFAULT_AUTO_APPLY_LANES
    roadmap_learning_rate_max_files_touched: int = 2
    roadmap_learning_rate_max_sections_touched: int = 4
    roadmap_learning_rate_max_changed_lines: int = 20
    roadmap_learning_rate_max_normative_changes: int = 2
    roadmap_learning_rate_operator_risk_limits: dict[str, int] = field(default_factory=dict)
    roadmap_drift_cluster_max_evidence_refs: int = 8
    risk_class_changed_line_budgets: dict[str, int] = field(default_factory=dict)
    editable_headings: tuple[str, ...] = field(default_factory=tuple)
    auto_apply_minimum_burn_in_days: int = 14
    auto_apply_maximum_rejection_rate: float = 0.10
    auto_apply_maximum_rollback_rate: float = 0.02
    forbidden_terms: tuple[str, ...] = (
        "approval",
        "sandbox",
        "secret",
        "deploy",
        "network",
        "permission",
        "must",
        "never",
    )
    llmff_binary: str = DEFAULT_FIXTURE_LLMFF_BINARY
    llmff_require_inspect: bool = True
    llmff_allow_network: bool = False
    llmff_timeout_ms: int = 60_000
    llmff_retry_attempts: int = 0
    llmff_retry_backoff_ms: int = 0
    allowed_manifest_hashes: tuple[str, ...] = field(default_factory=tuple)
    llmff_allowed_providers: tuple[str, ...] = field(default_factory=tuple)
    vcs_pull_request_enabled: bool = False
    vcs_pull_request_provider: str = ""
    vcs_pull_request_remote: str = "origin"
    vcs_pull_request_base_branch: str = ""
    vcs_pull_request_draft: bool = True
    raw_traces_retention_days: int = 14
    checkpoints_retention_days: int = 7
    retention_scan_file_budget: int = 100_000
    index_max_instruction_files: int = 10_000
    trace_max_input_bytes: int = 50_000_000
    trace_max_events: int = 100_000
    provider_smoke_enabled: bool = False
    provider_smoke_provider: str = ""
    provider_smoke_command: str = ""
    mcp_allowed_repositories: tuple[str, ...] = field(default_factory=tuple)
    mcp_tool_policy: dict[str, str] = field(default_factory=dict)
