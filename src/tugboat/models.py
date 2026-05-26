from __future__ import annotations

from dataclasses import dataclass, field


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
class Policy:
    version: int = 1
    mode: str = "proposal_only"
    instruction_files: tuple[InstructionFilePolicy, ...] = field(default_factory=tuple)
    auto_apply_enabled: bool = False
    auto_apply_max_changed_lines: int = 20
    auto_apply_allowed_repositories: tuple[str, ...] = field(default_factory=tuple)
    auto_apply_minimum_burn_in_days: int = 30
    auto_apply_maximum_rejection_rate: float = 0.05
    auto_apply_maximum_rollback_rate: float = 0.01
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
    llmff_binary: str = "llmff"
    llmff_require_inspect: bool = True
    llmff_allow_network: bool = False
    allowed_manifest_hashes: tuple[str, ...] = field(default_factory=tuple)
    raw_traces_retention_days: int = 14
    checkpoints_retention_days: int = 7
    mcp_allowed_repositories: tuple[str, ...] = field(default_factory=tuple)
    mcp_tool_policy: dict[str, str] = field(default_factory=dict)
