from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.traces.ingest import _evidence_id, canonical_episode_from_bundle
from tugboat.traces.schema import CanonicalEpisode, TraceBundle, TraceEvent


def ingest_codex_session(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_codex_session_bundle(path))


def ingest_codex_session_bundle(path: Path) -> TraceBundle:
    events: list[dict[str, Any]] = []
    for row in _read_jsonl(path):
        role = row.get("role")
        if role == "user":
            events.append({"type": "user_request", "content": row.get("content", "")})
        elif role == "assistant":
            events.append({"type": "final_answer", "content": row.get("content", "")})
        elif row.get("type") in {"tool_call", "tool_result", "diff", "test_result"}:
            events.append(dict(row))
    return _bundle_from_payloads(path, events)


def ingest_claude_transcript(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_claude_transcript_bundle(path))


def ingest_claude_transcript_bundle(path: Path) -> TraceBundle:
    payload = json.loads(path.read_text(encoding="utf-8"))
    messages = payload.get("messages", [])
    events: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "user" and message.get("kind") == "correction":
            events.append({"type": "user_correction", "content": message.get("content", "")})
        elif role == "user":
            events.append({"type": "user_request", "content": message.get("content", "")})
        elif role == "assistant":
            events.append({"type": "final_answer", "content": message.get("content", "")})
        elif role == "subagent":
            events.append(
                {
                    "type": "subagent_report",
                    "agent": message.get("name", "unknown"),
                    "summary": message.get("content", ""),
                }
            )
    return _bundle_from_payloads(path, events)


def ingest_ci_failure(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_ci_failure_bundle(path))


def ingest_ci_failure_bundle(path: Path) -> TraceBundle:
    payload = json.loads(path.read_text(encoding="utf-8"))
    suite = str(payload.get("suite", "ci"))
    exit_code = int(payload.get("exit_code", 1))
    events = [
        {
            "type": "tool_result",
            "tool": str(payload.get("command", "ci")),
            "exit_code": exit_code,
            "output": str(payload.get("output", "")),
        },
        {"type": "test_result", "suite": suite, "passed": exit_code == 0},
        {"type": "outcome_label", "label": "ci_failed" if exit_code else "ci_passed"},
    ]
    return _bundle_from_payloads(path, events)


def ingest_mcp_session(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_mcp_session_bundle(path))


def ingest_mcp_session_bundle(path: Path) -> TraceBundle:
    events: list[dict[str, Any]] = []
    for row in _read_jsonl(path):
        event = row.get("event")
        if event == "request":
            events.append({"type": "user_request", "content": row.get("text", "")})
        elif event == "tool.started":
            events.append({"type": "tool_call", "tool": row.get("tool", "unknown")})
        elif event == "tool.finished":
            events.append(
                {
                    "type": "tool_result",
                    "tool": row.get("tool", "unknown"),
                    "exit_code": int(row.get("exit_code", 0)),
                }
            )
        elif event == "agent.final":
            events.append({"type": "final_answer", "content": row.get("text", "")})
    return _bundle_from_payloads(path, events)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
    return rows


def _bundle_from_payloads(path: Path, payloads: list[dict[str, Any]]) -> TraceBundle:
    events = tuple(
        TraceEvent(
            evidence_id=_evidence_id(index, payload),
            event_type=str(payload.get("type", "unknown")),
            source_trust=_source_trust(str(payload.get("type", "unknown"))),
            line_number=index,
            payload=payload,
        )
        for index, payload in enumerate(payloads, start=1)
    )
    return TraceBundle(trace_path=path, events=events)


def _source_trust(event_type: str) -> str:
    if event_type in {"user_request", "user_correction"}:
        return "user"
    if event_type in {"tool_call", "tool_result"}:
        return "tool"
    if event_type in {"diff", "test_result"}:
        return "artifact"
    if event_type == "final_answer":
        return "agent"
    return "untrusted"
