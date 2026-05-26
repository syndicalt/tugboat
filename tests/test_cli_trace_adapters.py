from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from tugboat.cli import main


def _event_rows(repo: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        return connection.execute(
            """
            SELECT event_type, payload_json
            FROM trace_events
            ORDER BY line_number
            """
        ).fetchall()


def test_audit_cli_ingests_claude_trace_format_as_normalized_events(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
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
                json.dumps({"type": "tool_result", "tool": "pytest", "exit_code": 0}),
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
        "final_answer",
    ]
    assert json.loads(rows[1][1])["tool"] == "pytest"
    assert json.loads(rows[2][1])["exit_code"] == 0
