from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tugboat.traces.ingest import (
    _evidence_id,
    canonical_episode_from_bundle,
    enforce_trace_event_budget,
    source_trust_for_event_type,
)
from tugboat.traces.schema import CanonicalEpisode, TraceBundle, TraceEvent


def ingest_codex_session(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_codex_session_bundle(path))


def ingest_codex_session_bundle(path: Path, *, max_events: int | None = None) -> TraceBundle:
    events: list[dict[str, Any]] = []
    tool_names_by_call_id: dict[str, str] = {}
    for row in _iter_jsonl_objects(path, max_events=max_events):
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
        elif row.get("type") == "turn_context" and isinstance(row.get("payload"), dict):
            events.append(_normalize_codex_turn_context(row["payload"]))
        elif row.get("type") == "response_item" and isinstance(row.get("payload"), dict):
            events.extend(_normalize_codex_response_item(row["payload"], tool_names_by_call_id))
        enforce_trace_event_budget(len(events), max_events)
    return _bundle_from_payloads(path, _derive_test_results(events), max_events=max_events)


def ingest_claude_transcript(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_claude_transcript_bundle(path))


def ingest_claude_transcript_bundle(path: Path, *, max_events: int | None = None) -> TraceBundle:
    events: list[dict[str, Any]] = []
    tool_names_by_id: dict[str, str] = {}
    if path.suffix == ".jsonl":
        for row in _iter_jsonl_objects(path, max_events=max_events):
            if isinstance(row.get("message"), dict):
                events.extend(_normalize_claude_message(row["message"], tool_names_by_id))
            enforce_trace_event_budget(len(events), max_events)
        return _bundle_from_payloads(path, _derive_test_results(events), max_events=max_events)

    payload = load_json_trace_payload(path)
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
        enforce_trace_event_budget(len(events), max_events)
    return _bundle_from_payloads(path, _derive_test_results(events), max_events=max_events)


def ingest_ci_failure(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_ci_failure_bundle(path))


def ingest_ci_failure_bundle(path: Path, *, max_events: int | None = None) -> TraceBundle:
    payload = load_json_trace_payload(path)
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
    return _bundle_from_payloads(
        path,
        events,
        trusted_outcome_assertions=True,
        max_events=max_events,
    )


def load_json_trace_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            "JSON trace contains invalid JSON "
            f"at line {error.lineno} column {error.colno}"
        ) from error
    if not isinstance(payload, dict):
        raise ValueError("JSON trace must contain an object")
    return payload


def ingest_mcp_session(path: Path) -> CanonicalEpisode:
    return canonical_episode_from_bundle(ingest_mcp_session_bundle(path))


def ingest_mcp_session_bundle(path: Path, *, max_events: int | None = None) -> TraceBundle:
    events: list[dict[str, Any]] = []
    for row in _iter_jsonl_objects(path, max_events=max_events):
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
            events.append(
                {
                    "type": "outcome_label",
                    "label": row.get("label", ""),
                    "trusted": bool(row.get("trusted", False)),
                }
            )
        elif event == "verifier.score":
            verifier_name = row.get("name", row.get("verifier", "unknown"))
            events.append(
                {
                    "type": "verifier_score",
                    "name": verifier_name,
                    "verifier": row.get("verifier", verifier_name),
                    "score": float(row.get("score", 0.0)),
                    "trusted": bool(row.get("trusted", False)),
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
        enforce_trace_event_budget(len(events), max_events)
    return _bundle_from_payloads(path, events, max_events=max_events)


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


def _iter_jsonl_objects(path: Path, *, max_events: int | None = None) -> Iterator[dict[str, Any]]:
    event_count = 0
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"trace line {line_number} contains invalid JSON") from error
            if not isinstance(row, dict):
                raise ValueError(f"trace line {line_number} must be a JSON object")
            event_count += 1
            enforce_trace_event_budget(event_count, max_events)
            yield row


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


def _normalize_codex_turn_context(payload: dict[str, Any]) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "policy_context",
        "source": "codex_turn_context",
    }
    for key in ("approval_policy", "cwd", "current_date", "model", "sandbox_policy", "timezone"):
        if key in payload:
            event[key] = payload[key]
    return event


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


def _derive_test_results(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    calls_by_id: dict[str, dict[str, Any]] = {}
    pending_calls_by_tool: dict[str, dict[str, Any]] = {}
    existing_test_keys = _existing_test_keys(events)

    for event in events:
        result.append(event)
        event_type = event.get("type")
        if event_type == "tool_call":
            call_id = str(event.get("call_id", ""))
            if call_id:
                calls_by_id[call_id] = event
            pending_calls_by_tool[str(event.get("tool", ""))] = event
        elif event_type == "tool_result":
            derived = _test_result_from_tool_result(event, calls_by_id, pending_calls_by_tool)
            if derived is not None and _test_key(derived) not in existing_test_keys:
                result.append(derived)
                existing_test_keys.add(_test_key(derived))
    return result


def _existing_test_keys(events: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        _test_key(event)
        for event in events
        if event.get("type") == "test_result"
    }


def _test_key(event: dict[str, Any]) -> tuple[str, str]:
    call_id = str(event.get("call_id", ""))
    command = str(event.get("command", ""))
    return (call_id, command)


def _test_result_from_tool_result(
    result: dict[str, Any],
    calls_by_id: dict[str, dict[str, Any]],
    pending_calls_by_tool: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    call_id = str(result.get("call_id", ""))
    tool = str(result.get("tool", ""))
    call = calls_by_id.get(call_id) if call_id else pending_calls_by_tool.get(tool)
    command = _test_command_from_call(call, result)
    if command is None:
        return None
    passed = _test_status_from_result(result)
    if passed is None:
        return None
    event = {
        "type": "test_result",
        "suite": _test_suite_from_command(command),
        "passed": passed,
        "command": command,
        "source_tool": tool,
    }
    if call_id:
        event["call_id"] = call_id
        event["derived_from"] = call_id
    return event


def _test_command_from_call(
    call: dict[str, Any] | None,
    result: dict[str, Any],
) -> str | None:
    candidates: list[str] = []
    if call is not None:
        candidates.extend(_command_candidates_from_call(call))
    candidates.extend(_command_candidates_from_result(result))
    for candidate in candidates:
        command = _normalize_command(candidate)
        if _is_test_command(command):
            return command
    return None


def _command_candidates_from_call(call: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    args = call.get("args")
    if isinstance(args, list):
        candidates.append(" ".join([str(call.get("tool", "")), *[str(arg) for arg in args]]))
    arguments = call.get("arguments")
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("cmd", "command"):
                if key in parsed:
                    candidates.append(str(parsed[key]))
        candidates.append(arguments)
    elif isinstance(arguments, dict):
        for key in ("cmd", "command"):
            if key in arguments:
                candidates.append(str(arguments[key]))
    candidates.append(str(call.get("tool", "")))
    return candidates


def _command_candidates_from_result(result: dict[str, Any]) -> list[str]:
    candidates = [str(result.get("tool", ""))]
    if "command" in result:
        candidates.append(str(result["command"]))
    return candidates


def _normalize_command(command: str) -> str:
    return " ".join(command.strip().split())


def _is_test_command(command: str) -> bool:
    if not command:
        return False
    first_words = " ".join(command.split()[:2]).lower()
    first_word = command.split()[0].lower()
    return (
        first_word in {"pytest", "tox"}
        or first_words in {"npm test", "pnpm test", "yarn test", "go test", "cargo test"}
    )


def _test_status_from_result(result: dict[str, Any]) -> bool | None:
    if "exit_code" in result:
        try:
            return int(result["exit_code"]) == 0
        except (TypeError, ValueError):
            return None
    if "is_error" in result:
        return not bool(result["is_error"])
    output = str(result.get("output", ""))
    match = re.search(r"Process exited with code (-?\d+)", output)
    if match is not None:
        return int(match.group(1)) == 0
    return None


def _test_suite_from_command(command: str) -> str:
    first_words = " ".join(command.split()[:2]).lower()
    if first_words in {"npm test", "pnpm test", "yarn test", "go test", "cargo test"}:
        return first_words
    return command.split()[0].lower()


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


def _bundle_from_payloads(
    path: Path,
    payloads: list[dict[str, Any]],
    *,
    trusted_outcome_assertions: bool = False,
    max_events: int | None = None,
) -> TraceBundle:
    enforce_trace_event_budget(len(payloads), max_events)
    events = tuple(
        TraceEvent(
            evidence_id=_evidence_id(index, payload),
            event_type=str(payload.get("type", "unknown")),
            source_trust=source_trust_for_event_type(
                str(payload.get("type", "unknown")),
                trusted_assertions=_trusted_outcome_assertion(
                    payload,
                    default=trusted_outcome_assertions,
                ),
            ),
            line_number=index,
            payload=payload,
        )
        for index, payload in enumerate(payloads, start=1)
    )
    return TraceBundle(trace_path=path, events=events)


def _trusted_outcome_assertion(payload: dict[str, Any], *, default: bool) -> bool:
    if payload.get("type") not in {"outcome_label", "verifier_score"}:
        return True
    return bool(payload.get("trusted", default))
