from __future__ import annotations

from pathlib import Path

from tugboat.cli import main


def test_status_reports_empty_sidecar_state(tmp_path: Path, capsys):
    assert main(["status", "--repo", str(tmp_path)]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "mode: proposal_only",
        "auto_apply: disabled",
        "indexed_documents: 0",
        "latest_run: none",
        "pending_candidates: 0",
    ]


def test_status_reports_indexed_documents_and_latest_run(tmp_path: Path, capsys):
    (tmp_path / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")

    assert main(["index", "--repo", str(tmp_path)]) == 0
    assert main(["audit", "--repo", str(tmp_path), "--trace", str(trace), "--mock-llmff-inspect"]) == 0
    capsys.readouterr()

    assert main(["status", "--repo", str(tmp_path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "indexed_documents: 1" in lines
    assert "latest_run: audit completed" in lines
