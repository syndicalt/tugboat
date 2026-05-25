from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class InspectPolicyError(RuntimeError):
    pass


class LlmffRunner(Protocol):
    def inspect(self, manifest_path: Path) -> dict[str, Any]:
        pass


@dataclass(frozen=True)
class RunResult:
    manifest_path: Path
    exit_code: int
    trace_path: Path
    events_path: Path
    checkpoint_path: Path
    output_paths: dict[str, Path]
    failure_kind: str | None = None
    failure_message: str | None = None


@dataclass(frozen=True)
class InspectResult:
    manifest_path: Path
    manifest_hash: str
    artifact_path: Path
    inspect: dict[str, Any]
    network_required: bool
