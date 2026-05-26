from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.traces.ingest import _evidence_id, canonical_episode_from_bundle, source_trust_for_event_type
from tugboat.traces.schema import CanonicalEpisode, TraceBundle, TraceEvent


def ingest_codex_session(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_codex_session_bundle(path))


def ingest_codex_session_bundle(path: Path) -> TraceBundle:
    events: list[dict[str, Any]] = []
    tool_names_by_call_id: dict[str, str] = {}
    for row in _read_jsonl(path):
        role = row.get("role")
        if role == "user":
            events.append({"type": "user_request", "content": row.get("content", "")})
        elif role == "assistant":
            events.append({"type": "final_answer", "content": row.get("content", "")})
        elif row.get("type") in {"tool_call", "tool_result", "diff", "test_result"}:
            events.append(_normalize_codex_event(row))
        elif row.get("type") == "response_item" and isinstance(row.get("payload"), dict):
            event = _normalize_codex_response_item(row["payload"], tool_names_by_call_id)
            if event is not None:
                events.append(event)
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


def _normalize_codex_event(row: dict[str, Any]) -> dict[str, Any]:
    event = dict(row)
    if event.get("type") == "tool_result" and "output" not in event and "content" in event:
        event["output"] = str(event["content"])
    return event


def _normalize_codex_response_item(
    payload: dict[str, Any],
    tool_names_by_call_id: dict[str, str],
) -> dict[str, Any] | None:
    item_type = payload.get("type")
    if item_type == "message":
        role = payload.get("role")
        content = _codex_content_text(payload.get("content"))
        if role == "user":
            return {"type": "user_request", "content": content}
        if role == "assistant":
            return {"type": "final_answer", "content": content}
    if item_type == "function_call":
        call_id = str(payload.get("call_id", ""))
        tool = str(payload.get("name", "unknown"))
        if call_id:
            tool_names_by_call_id[call_id] = tool
        return {
            "type": "tool_call",
            "tool": tool,
            "call_id": call_id,
            "arguments": str(payload.get("arguments", "")),
        }
    if item_type == "function_call_output":
        call_id = str(payload.get("call_id", ""))
        return {
            "type": "tool_result",
            "tool": tool_names_by_call_id.get(call_id, "unknown"),
            "call_id": call_id,
            "output": str(payload.get("output", "")),
        }
    return None


def _codex_content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
        return "\n".join(parts)
    return ""


def _bundle_from_payloads(path: Path, payloads: list[dict[str, Any]]) -> TraceBundle:
    events = tuple(
        TraceEvent(
            evidence_id=_evidence_id(index, payload),
            event_type=str(payload.get("type", "unknown")),
            source_trust=source_trust_for_event_type(str(payload.get("type", "unknown"))),
            line_number=index,
            payload=payload,
        )
        for index, payload in enumerate(payloads, start=1)
    )
    return TraceBundle(trace_path=path, events=events)
