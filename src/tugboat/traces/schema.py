from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TraceEvent:
    evidence_id: str
    event_type: str
    source_trust: str
    line_number: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class TraceBundle:
    trace_path: Path
    events: tuple[TraceEvent, ...]
