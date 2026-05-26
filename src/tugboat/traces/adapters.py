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
        elif row.get("type") == "session_meta" and isinstance(row.get("payload"), dict):
            event = _normalize_codex_session_meta(row["payload"])
            if event is not None:
                events.append(event)
        elif row.get("type") == "response_item" and isinstance(row.get("payload"), dict):
            events.extend(_normalize_codex_response_item(row["payload"], tool_names_by_call_id))
    return _bundle_from_payloads(path, events)


def ingest_claude_transcript(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_claude_transcript_bundle(path))


def ingest_claude_transcript_bundle(path: Path) -> TraceBundle:
    events: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}
    if path.suffix == ".jsonl":
        for row in _read_jsonl(path):
            if isinstance(row.get("message"), dict):
                events.extend(_normalize_claude_message(row["message"], tool_names_by_id))
        return _bundle_from_payloads(path, events)

    payload = json.loads(path.read_text(encoding="utf-8"))
    messages = payload.get("messages", [])
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
        elif event == "instruction.snapshot":
            events.append(
                {
                    "type": "instruction_snapshot",
                    "source": row.get("source", "mcp"),
                    "text": row.get("text", ""),
                }
            )
        elif event == "user.correction":
            events.append({"type": "user_correction", "content": row.get("text", "")})
        elif event == "subagent.report":
            events.append(
                {
                    "type": "subagent_report",
                    "agent": row.get("agent", "unknown"),
                    "summary": row.get("summary", ""),
                }
            )
        elif event == "diff.applied":
            events.append(
                {
                    "type": "diff",
                    "path": row.get("path", ""),
                    "diff": row.get("diff", ""),
                }
            )
        elif event == "test.result":
            events.append(
                {
                    "type": "test_result",
                    "suite": row.get("suite", "unknown"),
                    "passed": _bool_from_mcp(row.get("passed", False)),
                    "output": row.get("output", ""),
                }
            )
        elif event == "outcome.label":
            events.append({"type": "outcome_label", "label": row.get("label", "")})
        elif event == "verifier.score":
            verifier_name = row.get("name", row.get("verifier", "unknown"))
            events.append(
                {
                    "type": "verifier_score",
                    "name": verifier_name,
                    "verifier": row.get("verifier", verifier_name),
                    "score": float(row.get("score", 0.0)),
                }
            )
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


def _bool_from_mcp(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "pass", "passed"}:
            return True
        if normalized in {"0", "false", "no", "n", "fail", "failed"}:
            return False
    return default


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
) -> list[dict[str, Any]]:
    item_type = payload.get("type")
    if item_type == "message":
        role = payload.get("role")
        content = _codex_content_text(payload.get("content"))
        if role == "user":
            return [{"type": "user_request", "content": content}]
        if role == "assistant":
            return [{"type": "final_answer", "content": content}]
    if item_type in {"function_call", "custom_tool_call"}:
        call_id = str(payload.get("call_id", ""))
        tool = str(payload.get("name", "unknown"))
        if call_id:
            tool_names_by_call_id[call_id] = tool
        arguments = payload.get("arguments", payload.get("input", ""))
        events = [{
            "type": "tool_call",
            "tool": tool,
            "call_id": call_id,
            "arguments": str(arguments),
        }]
        if tool == "apply_patch":
            diff_event = _codex_apply_patch_diff_event(str(arguments), call_id=call_id)
            if diff_event is not None:
                events.append(diff_event)
        return events
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        call_id = str(payload.get("call_id", ""))
        return [{
            "type": "tool_result",
            "tool": tool_names_by_call_id.get(call_id, "unknown"),
            "call_id": call_id,
            "output": str(payload.get("output", "")),
        }]
    return []


def _normalize_codex_session_meta(payload: dict[str, Any]) -> dict[str, Any] | None:
    base_instructions = payload.get("base_instructions")
    if not isinstance(base_instructions, dict):
        return None
    text = base_instructions.get("text")
    if text is None:
        return None
    return {
        "type": "instruction_snapshot",
        "source": str(base_instructions.get("source", "base_instructions")),
        "text": str(text),
    }


def _normalize_claude_message(
    message: dict[str, Any],
    tool_names_by_id: dict[str, str],
) -> list[dict[str, Any]]:
    role = message.get("role")
    content = message.get("content")
    if role == "user" and isinstance(content, str):
        return [{"type": "user_request", "content": content}]
    if role == "assistant":
        return _normalize_claude_assistant_content(content, tool_names_by_id)
    if role == "user" and isinstance(content, list):
        return _normalize_claude_user_content_blocks(content, tool_names_by_id)
    return []


def _normalize_claude_assistant_content(
    content: object,
    tool_names_by_id: dict[str, str],
) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "final_answer", "content": content}]
    if not isinstance(content, list):
        return []
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and "text" in block:
            text_parts.append(str(block["text"]))
        elif block_type == "tool_use":
            tool_id = str(block.get("id", ""))
            tool = str(block.get("name", "unknown"))
            tool_input = block.get("input", {})
            if tool_id:
                tool_names_by_id[tool_id] = tool
            events.append(
                {
                    "type": "tool_call",
                    "tool": tool,
                    "call_id": tool_id,
                    "arguments": _compact_json(tool_input),
                }
            )
            diff_event = _claude_edit_diff_event(tool, tool_input, call_id=tool_id)
            if diff_event is not None:
                events.append(diff_event)
    if text_parts:
        events.insert(0, {"type": "final_answer", "content": "\n".join(text_parts)})
    return events


def _normalize_claude_user_content_blocks(
    content: list[object],
    tool_names_by_id: dict[str, str],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            call_id = str(block.get("tool_use_id", ""))
            events.append(
                {
                    "type": "tool_result",
                    "tool": tool_names_by_id.get(call_id, "unknown"),
                    "call_id": call_id,
                    "output": _claude_tool_result_text(block.get("content", "")),
                    "is_error": bool(block.get("is_error", False)),
                }
            )
    return events


def _claude_tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block["text"])
            for block in content
            if isinstance(block, dict) and "text" in block
        )
    return ""


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


def _compact_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _codex_apply_patch_diff_event(patch: str, *, call_id: str) -> dict[str, Any] | None:
    path = _apply_patch_target_path(patch)
    if path is None:
        return None
    return {
        "type": "diff",
        "path": path,
        "diff": patch,
        "source_tool": "apply_patch",
        "call_id": call_id,
    }


def _apply_patch_target_path(patch: str) -> str | None:
    for line in patch.splitlines():
        for marker in (
            "*** Update File: ",
            "*** Add File: ",
            "*** Delete File: ",
        ):
            if line.startswith(marker):
                return line.removeprefix(marker).strip()
    return None


def _claude_edit_diff_event(tool: str, tool_input: object, *, call_id: str) -> dict[str, Any] | None:
    if tool not in {"Edit", "MultiEdit"} or not isinstance(tool_input, dict):
        return None
    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return None
    if tool == "Edit":
        old_string = tool_input.get("old_string")
        new_string = tool_input.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return None
        diff = _string_replacement_diff(file_path, old_string, new_string)
    else:
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
        hunks = []
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            old_string = edit.get("old_string")
            new_string = edit.get("new_string")
            if isinstance(old_string, str) and isinstance(new_string, str):
                hunks.append(_diff_hunk(old_string, new_string))
        if not hunks:
            return None
        diff = f"--- a/{file_path}\n+++ b/{file_path}\n" + "".join(hunks)
    return {
        "type": "diff",
        "path": file_path,
        "diff": diff,
        "source_tool": tool,
        "call_id": call_id,
    }


def _string_replacement_diff(path: str, old_string: str, new_string: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n{_diff_hunk(old_string, new_string)}"


def _diff_hunk(old_string: str, new_string: str) -> str:
    old_lines = old_string.splitlines() or [old_string]
    new_lines = new_string.splitlines() or [new_string]
    return "@@\n" + "".join(f"-{line}\n" for line in old_lines) + "".join(
        f"+{line}\n" for line in new_lines
    )


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
