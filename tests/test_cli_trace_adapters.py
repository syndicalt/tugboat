from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from tugboat.audit.pipeline import detect_trace_format
from tugboat.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def _event_rows(repo: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        return connection.execute(
            """
            SELECT event_type, payload_json
            FROM trace_events
            ORDER BY line_number
            """
        ).fetchall()


def test_detect_trace_format_classifies_supported_real_trace_shapes(tmp_path: Path):
    claude_json = tmp_path / "claude.json"
    claude_json.write_text(
        json.dumps({"messages": [{"role": "user", "content": "Fix bug"}]}),
        encoding="utf-8",
    )
    mcp_jsonl = tmp_path / "mcp.jsonl"
    mcp_jsonl.write_text(json.dumps({"event": "request", "text": "Fix bug"}) + "\n")
    generic_jsonl = tmp_path / "generic.jsonl"
    generic_jsonl.write_text(
        json.dumps({"type": "user_request", "content": "Fix bug"}) + "\n",
        encoding="utf-8",
    )

    assert detect_trace_format(claude_json) == "claude"
    assert detect_trace_format(mcp_jsonl) == "mcp"
    assert detect_trace_format(generic_jsonl) == "generic-jsonl"


@pytest.mark.parametrize(
    "filename, payload, expected_format",
    [
        (
            "codex-session-meta.jsonl",
            json.dumps({"type": "session_meta", "payload": {"id": "session-1"}}) + "\n",
            "codex",
        ),
        (
            "codex-response-item.jsonl",
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
            + "\n",
            "codex",
        ),
        (
            "claude.json",
            json.dumps({"messages": [{"role": "user", "content": "Fix bug"}]}),
            "claude",
        ),
        (
            "claude.jsonl",
            json.dumps({"message": {"role": "user", "content": "Fix bug"}}) + "\n",
            "claude",
        ),
        (
            "mcp.jsonl",
            json.dumps({"event": "request", "text": "Fix bug"}) + "\n",
            "mcp",
        ),
        (
            "ci-failure.json",
            json.dumps(
                {
                    "suite": "unit",
                    "command": "pytest -q",
                    "exit_code": 1,
                    "output": "1 failed",
                }
            ),
            "ci",
        ),
        (
            "generic.jsonl",
            json.dumps({"type": "user_request", "content": "Fix bug"}) + "\n",
            "generic-jsonl",
        ),
    ],
)
def test_detect_trace_format_generated_minimal_shapes_are_stable(
    tmp_path: Path,
    filename: str,
    payload: str,
    expected_format: str,
):
    trace = tmp_path / filename
    trace.write_text(payload, encoding="utf-8")
    trace_with_leading_blank = tmp_path / f"blank-{filename}"
    trace_with_leading_blank.write_text("\n\n" + payload, encoding="utf-8")

    assert detect_trace_format(trace) == expected_format
    assert detect_trace_format(trace_with_leading_blank) == expected_format


def test_audit_cli_ingests_claude_trace_format_as_normalized_events(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    instruction_text = "# Rules\n\nUse tests.\n"
    (repo / "CODEX.md").write_text(instruction_text, encoding="utf-8")
    trace = tmp_path / "claude.json"
    trace.write_text(
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": "Fix bug"},
                    {"role": "user", "kind": "correction", "content": "Use TDD"},
                    {"role": "subagent", "name": "reviewer", "content": "Missing test"},
                    {"role": "assistant", "content": "Done"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "claude",
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == [
        "user_request",
        "user_correction",
        "subagent_report",
        "final_answer",
    ]
    assert json.loads(rows[2][1])["agent"] == "reviewer"


def test_audit_cli_ingests_claude_jsonl_content_blocks(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "claude.jsonl"
    trace.write_text(
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

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "claude",
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == [
        "user_request",
        "final_answer",
        "tool_call",
        "tool_result",
        "test_result",
    ]
    assert json.loads(rows[2][1])["tool"] == "Bash"
    assert json.loads(rows[3][1])["output"] == "1 failed"
    assert json.loads(rows[4][1]) == {
        "type": "test_result",
        "suite": "pytest",
        "passed": False,
        "command": "pytest -q",
        "source_tool": "Bash",
        "call_id": "toolu_1",
        "derived_from": "toolu_1",
    }


def test_audit_cli_ingests_codex_trace_format_without_collapsing_tool_events(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "codex.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "Fix bug"}),
                json.dumps({"type": "tool_call", "tool": "pytest", "args": ["-q"]}),
                json.dumps({"type": "tool_result", "tool": "pytest", "exit_code": 0, "content": "2 passed"}),
                json.dumps({"role": "assistant", "content": "Done"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "codex",
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == [
        "user_request",
        "tool_call",
        "tool_result",
        "test_result",
        "final_answer",
    ]
    assert json.loads(rows[1][1])["tool"] == "pytest"
    assert json.loads(rows[2][1])["exit_code"] == 0
    assert json.loads(rows[2][1])["output"] == "2 passed"
    assert json.loads(rows[3][1]) == {
        "type": "test_result",
        "suite": "pytest",
        "passed": True,
        "command": "pytest -q",
        "source_tool": "pytest",
    }


def test_audit_cli_ingests_codex_response_item_envelopes(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "codex.jsonl"
    trace.write_text(
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

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "codex",
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == [
        "user_request",
        "tool_call",
        "tool_result",
        "final_answer",
    ]
    assert json.loads(rows[1][1])["tool"] == "exec_command"
    assert json.loads(rows[2][1])["output"] == "1 failed"


def test_audit_cli_auto_detects_codex_response_item_trace(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "codex.jsonl"
    trace.write_text(
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
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == [
        "user_request",
        "tool_call",
        "tool_result",
    ]
    assert json.loads(rows[1][1])["tool"] == "exec_command"
    assert json.loads(rows[2][1])["tool"] == "exec_command"


def test_audit_cli_reports_adapter_jsonl_non_object_line_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "codex.jsonl"
    trace.write_text(
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

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "codex",
                "--mock-llmff-inspect",
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "audit blocked: invalid trace: trace line 2 must be a JSON object" in output
    assert "Traceback" not in output


def test_audit_cli_auto_trace_format_reports_non_object_sample_line_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps({"type": "user_request", "content": "Fix bug"}) + "\n"
        + json.dumps(["not", "an", "object"])
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "auto",
                "--mock-llmff-inspect",
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "audit blocked: invalid trace: trace line 2 must be a JSON object" in output
    assert "Traceback" not in output


def test_audit_cli_reports_adapter_jsonl_invalid_json_line_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "codex.jsonl"
    trace.write_text(
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
        + "{not-json\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "codex",
                "--mock-llmff-inspect",
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "audit blocked: invalid trace: trace line 2 contains invalid JSON" in output
    assert "Traceback" not in output


def test_audit_cli_auto_trace_format_reports_invalid_json_sample_line_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        json.dumps({"type": "user_request", "content": "Fix bug"}) + "\n"
        + "{not-json\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "auto",
                "--mock-llmff-inspect",
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "audit blocked: invalid trace: trace line 2 contains invalid JSON" in output
    assert "Traceback" not in output


def test_audit_cli_ingests_codex_local_session_export_fixture(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    original = "# Rules\n\nUse tests.\n"
    (repo / "CODEX.md").write_text(original, encoding="utf-8")
    trace = FIXTURES / "traces" / "codex-local-session-export.jsonl"

    assert detect_trace_format(trace) == "codex"
    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == [
        "instruction_snapshot",
        "policy_context",
        "user_request",
        "tool_call",
        "tool_result",
        "test_result",
        "tool_call",
        "diff",
        "tool_result",
        "final_answer",
    ]
    assert json.loads(rows[0][1]) == {
        "type": "instruction_snapshot",
        "source": "CODEX.md",
        "text": original,
    }
    assert json.loads(rows[1][1]) == {
        "type": "policy_context",
        "approval_policy": "never",
        "cwd": "/workspace/example",
        "current_date": "2026-05-20",
        "model": "gpt-5.5",
        "sandbox_policy": {"type": "danger-full-access"},
        "source": "codex_turn_context",
        "timezone": "America/Chicago",
    }
    assert json.loads(rows[5][1]) == {
        "type": "test_result",
        "suite": "pytest",
        "passed": False,
        "command": "pytest tests/test_harness.py -q",
        "source_tool": "exec_command",
        "call_id": "call_test_1",
        "derived_from": "call_test_1",
    }
    assert json.loads(rows[7][1])["path"] == "CODEX.md"

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    canonical = json.loads((run_dir / "canonical-episode.json").read_text(encoding="utf-8"))
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    evidence_ids = {event["evidence_id"] for event in canonical["events"]}
    assert set(audit["evidence_refs"]).issubset(evidence_ids)
    assert canonical["request"] == "Fix the regression and keep the harness guidance test-backed."
    assert canonical["final_answer"] == "Implemented the harness guidance change and verified the focused test."
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original


def test_audit_cli_ingests_codex_custom_tool_response_items(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "codex.jsonl"
    trace.write_text(
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

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "codex",
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == ["tool_call", "tool_result"]
    assert json.loads(rows[0][1])["tool"] == "apply_patch"
    assert json.loads(rows[1][1])["output"] == "Success. Updated the following files:\nM CODEX.md\n"


def test_audit_cli_ingests_codex_session_meta_instruction_snapshot(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "codex.jsonl"
    trace.write_text(
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

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "codex",
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == ["instruction_snapshot", "user_request"]
    assert json.loads(rows[0][1]) == {
        "type": "instruction_snapshot",
        "source": "CODEX.md",
        "text": "Use tests and cite verification.",
    }


def test_audit_cli_ingests_mcp_session_rich_canonical_events(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "mcp.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps({"event": "request", "text": "Fix bug"}),
                json.dumps(
                    {
                        "event": "instruction.snapshot",
                        "source": "CODEX.md",
                        "text": "Use regression tests.",
                    }
                ),
                json.dumps({"event": "user.correction", "text": "Add the failing test first"}),
                json.dumps(
                    {
                        "event": "subagent.report",
                        "agent": "reviewer",
                        "summary": "Missing test coverage",
                    }
                ),
                json.dumps(
                    {
                        "event": "diff.applied",
                        "path": "CODEX.md",
                        "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use tests.\n",
                    }
                ),
                json.dumps(
                    {
                        "event": "test.result",
                        "suite": "pytest",
                        "passed": "false",
                        "output": "1 failed",
                    }
                ),
                json.dumps({"event": "outcome.label", "label": "needs_revision"}),
                json.dumps(
                    {
                        "event": "verifier.score",
                        "verifier": "pytest",
                        "score": 0.25,
                    }
                ),
                json.dumps({"event": "agent.final", "text": "I will revise."}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "mcp",
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == [
        "user_request",
        "instruction_snapshot",
        "user_correction",
        "subagent_report",
        "diff",
        "test_result",
        "outcome_label",
        "verifier_score",
        "final_answer",
    ]
    assert json.loads(rows[1][1]) == {
        "type": "instruction_snapshot",
        "source": "CODEX.md",
        "text": "Use regression tests.",
    }
    assert json.loads(rows[3][1]) == {
        "type": "subagent_report",
        "agent": "reviewer",
        "summary": "Missing test coverage",
    }
    assert json.loads(rows[4][1])["path"] == "CODEX.md"
    assert json.loads(rows[5][1]) == {
        "type": "test_result",
        "suite": "pytest",
        "passed": False,
        "output": "1 failed",
    }
    assert json.loads(rows[7][1]) == {
        "type": "verifier_score",
        "name": "pytest",
        "verifier": "pytest",
        "score": 0.25,
        "trusted": False,
    }
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    canonical_episode = json.loads((run_dir / "canonical-episode.json").read_text(encoding="utf-8"))
    test_events = [
        event for event in canonical_episode["events"] if event["event_type"] == "test_result"
    ]
    assert test_events[0]["payload"]["passed"] is False
    verifier_events = [
        event for event in canonical_episode["events"] if event["event_type"] == "verifier_score"
    ]
    assert verifier_events[0]["source_trust"] == "untrusted"
    assert verifier_events[0]["payload"]["score"] == 0.25
    assert canonical_episode["outcome_labels"] == []
    assert canonical_episode["verifier_scores"] == {}


def test_audit_cli_writes_canonical_redacted_trace_for_llmff_input(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "claude.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps({"type": "user", "message": {"role": "user", "content": "Fix bug"}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_1",
                                    "name": "Bash",
                                    "input": {"command": "env"},
                                }
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
                                    "content": "command output without secrets",
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

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "claude",
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    canonical_rows = [
        json.loads(line)
        for line in (run_dir / "trace-redacted.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    canonical_episode = json.loads((run_dir / "canonical-episode.json").read_text(encoding="utf-8"))

    assert [row["event_type"] for row in canonical_rows] == [
        "user_request",
        "tool_call",
        "tool_result",
    ]
    assert all(row["evidence_id"].startswith("ev_") for row in canonical_rows)
    assert canonical_rows[2]["payload"] == {
        "type": "tool_result",
        "tool": "Bash",
        "call_id": "toolu_1",
        "output": "command output without secrets",
        "is_error": False,
    }
    assert canonical_episode["request"] == "Fix bug"
    assert [row["event_type"] for row in canonical_episode["events"]] == [
        "user_request",
        "tool_call",
        "tool_result",
    ]


def test_audit_cli_canonical_episode_groups_policy_events(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(
            [
                json.dumps({"type": "user_request", "content": "Fix bug"}),
                json.dumps(
                    {
                        "type": "policy_violation",
                        "policy": "approval_boundary",
                        "status": "failed",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    canonical_episode = json.loads((run_dir / "canonical-episode.json").read_text(encoding="utf-8"))

    assert canonical_episode["policy_events"] == [
        {
            "evidence_id": canonical_episode["events"][1]["evidence_id"],
            "event_type": "policy_violation",
            "source_trust": "policy",
            "line_number": 2,
            "payload": {
                "type": "policy_violation",
                "policy": "approval_boundary",
                "status": "failed",
            },
        }
    ]


def test_audit_cli_canonical_episode_includes_active_instruction_snapshot(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    instruction_text = "# Rules\n\nUse tests.\n"
    (repo / "CODEX.md").write_text(instruction_text, encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","content":"Fix bug"}\n', encoding="utf-8")

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    canonical_episode = json.loads((run_dir / "canonical-episode.json").read_text(encoding="utf-8"))

    assert canonical_episode["instruction_snapshot"] == [
        {
            "type": "instruction_snapshot",
            "source": "CODEX.md",
            "origin": "audit_snapshot",
            "sha256": hashlib.sha256(instruction_text.encode("utf-8")).hexdigest(),
            "text": instruction_text,
        }
    ]


def test_audit_cli_redacted_trace_includes_instruction_snapshot(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "codex.jsonl"
    trace.write_text(
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

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--trace-format",
                "codex",
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    canonical_rows = [
        json.loads(line)
        for line in (run_dir / "trace-redacted.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert [row["event_type"] for row in canonical_rows] == [
        "instruction_snapshot",
        "user_request",
    ]
    assert canonical_rows[0]["payload"] == {
        "type": "instruction_snapshot",
        "source": "CODEX.md",
        "text": "Use tests and cite verification.",
    }


def test_audit_cli_auto_detects_ci_failure_json_trace(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "ci-failure.json"
    trace.write_text(
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

    assert (
        main(
            [
                "audit",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--mock-llmff-inspect",
            ]
        )
        == 0
    )

    rows = _event_rows(repo)
    assert [event_type for event_type, _ in rows] == [
        "tool_result",
        "test_result",
        "outcome_label",
    ]
    assert json.loads(rows[0][1])["tool"] == "pytest -q"
    assert json.loads(rows[1][1]) == {"type": "test_result", "suite": "unit", "passed": False}
