from pathlib import Path
from contextlib import closing
import json
import sqlite3

from tugboat.cli import main


def test_index_command_writes_sidecar_db(tmp_path: Path, capsys):
    (tmp_path / "CODEX.md").write_text("# Rules\n\nMust test.\n", encoding="utf-8")

    exit_code = main(["index", "--repo", str(tmp_path)])

    assert exit_code == 0
    assert (tmp_path / ".sidecar" / "db.sqlite").exists()
    assert "indexed documents: 1" in capsys.readouterr().out
    with closing(sqlite3.connect(tmp_path / ".sidecar" / "db.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
        rows = connection.execute(
            "SELECT event_type, payload_json FROM audit_events ORDER BY sequence"
        ).fetchall()
    assert [row[0] for row in rows] == [
        "document.indexed",
        "instruction_chunk.indexed",
        "documents.indexed",
    ]
    assert json.loads(rows[-1][1]) == {"documents": 1, "repo": str(tmp_path)}


def test_index_check_reports_invalid_policy_without_traceback(tmp_path: Path, capsys):
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: [\n", encoding="utf-8")

    exit_code = main(["index", "--repo", str(tmp_path), "--check"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "index blocked: policy invalid:" in output
    assert "Traceback" not in output
    assert not (sidecar / "db.sqlite").exists()


def test_index_write_reports_invalid_policy_without_traceback(tmp_path: Path, capsys):
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: [\n", encoding="utf-8")

    exit_code = main(["index", "--repo", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "index blocked: policy invalid:" in output
    assert "Traceback" not in output
    assert not (sidecar / "db.sqlite").exists()
