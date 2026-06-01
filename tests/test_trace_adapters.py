from __future__ import annotations

import json
from pathlib import Path

import pytest

from tugboat.audit.pipeline import detect_trace_format
from tugboat.traces.adapters import (
    ingest_ci_failure,
    ingest_claude_transcript,
    ingest_codex_session,
    ingest_codex_session_bundle,
    ingest_mcp_session,
    ingest_mcp_session_bundle,
)
from tugboat.traces.ingest import ingest_jsonl_trace

FIXTURE_TRACE_DIR = Path(__file__).parent / "fixtures" / "traces"


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
    assert episode.test_results[0].payload == {
        "type": "test_result",
        "suite": "pytest",
        "passed": False,
        "command": "pytest -q",
        "source_tool": "pytest",
    }
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
    assert episode.test_results == ()
    assert episode.final_answer == "Done"


def test_ingest_codex_session_rejects_non_object_jsonl_line_with_line_number(
    tmp_path: Path,
):
    session = tmp_path / "codex-session.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix bug"}],
                },
            }
        )
        + "\n"
        + json.dumps(["not", "an", "object"])
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="trace line 2 must be a JSON object"):
        ingest_codex_session(session)


@pytest.mark.parametrize(
    "ingest_bundle",
    [ingest_codex_session_bundle, ingest_mcp_session_bundle, ingest_jsonl_trace],
)
@pytest.mark.parametrize(
    "bad_line, expected_error",
    [
        (json.dumps(["not", "an", "object"]), "trace line 2 must be a JSON object"),
        ("{not-json", "trace line 2 contains invalid JSON"),
    ],
)
def test_jsonl_trace_ingestors_report_malformed_line_numbers(
    tmp_path: Path,
    ingest_bundle,
    bad_line: str,
    expected_error: str,
):
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        (
            json.dumps({"type": "user_request", "content": "Fix bug"})
            + "\n"
            + bad_line
            + "\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=expected_error):
        ingest_bundle(trace)


def test_ingest_codex_large_response_item_trace_preserves_order_and_evidence_ids(
    tmp_path: Path,
):
    session = tmp_path / "codex-large.jsonl"
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"Fix issue {index}"}],
            },
        }
        for index in range(150)
    ]
    session.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    bundle = ingest_codex_session_bundle(session)

    assert len(bundle.events) == 150
    assert [event.payload["content"] for event in bundle.events[:3]] == [
        "Fix issue 0",
        "Fix issue 1",
        "Fix issue 2",
    ]
    assert [event.line_number for event in bundle.events[-3:]] == [148, 149, 150]
    assert len({event.evidence_id for event in bundle.events}) == 150


def test_ingest_codex_session_derives_test_result_from_process_exit_status(
    tmp_path: Path,
):
    session = tmp_path / "codex-session.jsonl"
    session.write_text(
        "\n".join(
            [
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
                            "output": "==================\\nProcess exited with code 1",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_codex_session(session)

    assert episode.test_results[0].payload == {
        "type": "test_result",
        "suite": "pytest",
        "passed": False,
        "command": "pytest -q",
        "source_tool": "exec_command",
        "call_id": "call-1",
        "derived_from": "call-1",
    }


def test_ingest_codex_session_maps_custom_tool_response_items(tmp_path: Path):
    session = tmp_path / "codex-session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call",
                            "call_id": "call-1",
                            "name": "apply_patch",
                            "input": "*** Begin Patch\n*** End Patch\n",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "custom_tool_call_output",
                            "call_id": "call-1",
                            "output": "Success. Updated the following files:\nM CODEX.md\n",
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_codex_session(session)

    assert episode.tool_calls[0].payload == {
        "type": "tool_call",
        "tool": "apply_patch",
        "call_id": "call-1",
        "arguments": "*** Begin Patch\n*** End Patch\n",
    }
    assert episode.command_outputs[0].payload == {
        "type": "tool_result",
        "tool": "apply_patch",
        "call_id": "call-1",
        "output": "Success. Updated the following files:\nM CODEX.md\n",
    }


def test_ingest_codex_session_derives_diff_evidence_from_apply_patch_call(
    tmp_path: Path,
):
    patch = (
        "*** Begin Patch\n"
        "*** Update File: CODEX.md\n"
        "@@\n"
        "-Use tests.\n"
        "+Use regression tests.\n"
        "*** End Patch\n"
    )
    session = tmp_path / "codex-session.jsonl"
    session.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-1",
                    "name": "apply_patch",
                    "input": patch,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_codex_session(session)

    assert episode.diffs[0].payload == {
        "type": "diff",
        "path": "CODEX.md",
        "diff": patch,
        "source_tool": "apply_patch",
        "call_id": "call-1",
    }


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
    assert episode.test_results[0].payload == {
        "type": "test_result",
        "suite": "pytest",
        "passed": False,
        "command": "pytest -q",
        "source_tool": "Bash",
        "call_id": "toolu_1",
        "derived_from": "toolu_1",
    }


def test_ingest_claude_transcript_derives_diff_evidence_from_edit_tool(
    tmp_path: Path,
):
    transcript = tmp_path / "claude.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_edit",
                            "name": "Edit",
                            "input": {
                                "file_path": "CODEX.md",
                                "old_string": "Use tests.",
                                "new_string": "Use regression tests.",
                            },
                        }
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_claude_transcript(transcript)

    assert episode.diffs[0].payload == {
        "type": "diff",
        "path": "CODEX.md",
        "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n-Use tests.\n+Use regression tests.\n",
        "source_tool": "Edit",
        "call_id": "toolu_edit",
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


def test_ingest_mcp_session_preserves_untrusted_outcome_assertions_without_promoting(
    tmp_path: Path,
):
    session = tmp_path / "mcp-session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps({"event": "outcome.label", "label": "accepted"}),
                json.dumps({"event": "verifier.score", "name": "quality", "score": 1.0}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_mcp_session(session)

    assert [event.source_trust for event in episode.outcome_label_events] == ["untrusted"]
    assert [event.source_trust for event in episode.verifier_score_events] == ["untrusted"]
    assert episode.outcome_label_events[0].payload["label"] == "accepted"
    assert episode.verifier_score_events[0].payload["score"] == 1.0
    assert episode.outcome_labels == ()
    assert episode.verifier_scores == {}


def test_ingest_mcp_session_promotes_explicitly_trusted_outcome_assertions(
    tmp_path: Path,
):
    session = tmp_path / "mcp-session.jsonl"
    session.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "outcome.label",
                        "label": "accepted",
                        "trusted": True,
                    }
                ),
                json.dumps(
                    {
                        "event": "verifier.score",
                        "name": "quality",
                        "score": 0.75,
                        "trusted": True,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    episode = ingest_mcp_session(session)

    assert [event.source_trust for event in episode.outcome_label_events] == ["verifier"]
    assert [event.source_trust for event in episode.verifier_score_events] == ["verifier"]
    assert episode.outcome_labels == ("accepted",)
    assert episode.verifier_scores == {"quality": 0.75}


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


def test_ingest_mcp_session_rejects_non_object_jsonl_line_with_line_number(
    tmp_path: Path,
):
    session = tmp_path / "mcp-session.jsonl"
    session.write_text(
        json.dumps({"event": "request", "text": "Update docs"})
        + "\n"
        + json.dumps("not an object")
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="trace line 2 must be a JSON object"):
        ingest_mcp_session_bundle(session)


@pytest.mark.parametrize(
    ("fixture_name", "trace_format"),
    (
        ("claude-transcript.json", "claude"),
        ("mcp-session.jsonl", "mcp"),
        ("ci-failure.json", "ci"),
    ),
)
def test_shared_trace_fixtures_are_ingestible(fixture_name: str, trace_format: str):
    fixture = FIXTURE_TRACE_DIR / fixture_name

    assert fixture.exists()
    assert detect_trace_format(fixture) == trace_format

    if trace_format == "claude":
        episode = ingest_claude_transcript(fixture)
        assert episode.request == "Fix the failing harness check."
        assert episode.final_answer == "Added regression coverage."
    elif trace_format == "mcp":
        episode = ingest_mcp_session(fixture)
        assert episode.request == "Review active Tugboat status."
        assert episode.final_answer == "Status reviewed."
    else:
        episode = ingest_ci_failure(fixture)
        assert episode.outcome_labels == ("ci_failed",)
        assert episode.test_results[0].payload["suite"] == "unit"
