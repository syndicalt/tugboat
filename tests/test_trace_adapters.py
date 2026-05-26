from __future__ import annotations

import json
from pathlib import Path

from tugboat.traces.adapters import (
    ingest_ci_failure,
    ingest_claude_transcript,
    ingest_codex_session,
    ingest_mcp_session,
)


def test_ingest_codex_session_maps_tool_events_to_canonical_episode(tmp_path: Path):
    session = tmp_path / "codex-session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "Fix bug"}),
                json.dumps({"type": "tool_call", "tool": "pytest", "args": ["-q"]}),
                json.dumps({"type": "tool_result", "tool": "pytest", "exit_code": 1}),
                json.dumps({"role": "assistant", "content": "Done"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_codex_session(session)

    assert episode.request == "Fix bug"
    assert episode.tool_calls[0].payload["tool"] == "pytest"
    assert episode.command_outputs[0].payload["exit_code"] == 1
    assert episode.final_answer == "Done"


def test_ingest_codex_session_maps_response_item_envelopes(tmp_path: Path):
    session = tmp_path / "codex-session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Fix bug"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "exec_command",
                            "arguments": '{"cmd":"pytest -q"}',
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-1",
                            "output": "1 failed",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "Done"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_codex_session(session)

    assert episode.request == "Fix bug"
    assert episode.tool_calls[0].payload == {
        "type": "tool_call",
        "tool": "exec_command",
        "call_id": "call-1",
        "arguments": '{"cmd":"pytest -q"}',
    }
    assert episode.command_outputs[0].payload == {
        "type": "tool_result",
        "tool": "exec_command",
        "call_id": "call-1",
        "output": "1 failed",
    }
    assert episode.final_answer == "Done"


def test_ingest_codex_session_maps_session_meta_base_instructions(tmp_path: Path):
    session = tmp_path / "codex-session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "base_instructions": {
                                "source": "CODEX.md",
                                "text": "Use tests and cite verification.",
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Fix bug"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_codex_session(session)

    assert episode.instruction_snapshot == (
        {
            "type": "instruction_snapshot",
            "source": "CODEX.md",
            "text": "Use tests and cite verification.",
        },
    )


def test_ingest_claude_transcript_maps_corrections_and_subagents(tmp_path: Path):
    transcript = tmp_path / "claude.json"
    transcript.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "Add feature"},
                    {"role": "user", "kind": "correction", "content": "Use TDD"},
                    {"role": "assistant", "content": "Implemented"},
                    {"role": "subagent", "name": "reviewer", "content": "Missing test"},
                ]
            }
        ),
        encoding="utf-8",
    )

    episode = ingest_claude_transcript(transcript)

    assert episode.request == "Add feature"
    assert episode.user_corrections[0].payload["content"] == "Use TDD"
    assert episode.subagent_reports[0].payload["agent"] == "reviewer"
    assert episode.final_answer == "Implemented"


def test_ingest_claude_transcript_maps_jsonl_content_blocks(tmp_path: Path):
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "Fix bug"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "text", "text": "I'll inspect."},
                                {
                                    "type": "tool_use",
                                    "id": "toolu_1",
                                    "name": "Bash",
                                    "input": {"command": "pytest -q"},
                                },
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_1",
                                    "content": "1 failed",
                                    "is_error": True,
                                }
                            ],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_claude_transcript(transcript)

    assert episode.request == "Fix bug"
    assert episode.final_answer == "I'll inspect."
    assert episode.tool_calls[0].payload == {
        "type": "tool_call",
        "tool": "Bash",
        "call_id": "toolu_1",
        "arguments": '{"command":"pytest -q"}',
    }
    assert episode.command_outputs[0].payload == {
        "type": "tool_result",
        "tool": "Bash",
        "call_id": "toolu_1",
        "output": "1 failed",
        "is_error": True,
    }


def test_ingest_ci_failure_maps_failed_suite_and_outcome(tmp_path: Path):
    failure = tmp_path / "ci-failure.json"
    failure.write_text(
        json.dumps(
            {
                "suite": "unit",
                "command": "pytest -q",
                "exit_code": 1,
                "output": "1 failed",
            }
        ),
        encoding="utf-8",
    )

    episode = ingest_ci_failure(failure)

    assert episode.test_results[0].payload == {
        "type": "test_result",
        "suite": "unit",
        "passed": False,
    }
    assert episode.command_outputs[0].payload["exit_code"] == 1
    assert episode.outcome_labels == ("ci_failed",)


def test_ingest_mcp_session_maps_live_tool_events(tmp_path: Path):
    session = tmp_path / "mcp-session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps({"event": "request", "text": "Update docs"}),
                json.dumps({"event": "tool.started", "tool": "apply_patch"}),
                json.dumps({"event": "tool.finished", "tool": "apply_patch", "exit_code": 0}),
                json.dumps({"event": "agent.final", "text": "Done"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_mcp_session(session)

    assert episode.request == "Update docs"
    assert episode.tool_calls[0].payload["tool"] == "apply_patch"
    assert episode.command_outputs[0].payload["exit_code"] == 0
    assert episode.final_answer == "Done"
