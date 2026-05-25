import os
import subprocess
import sys
from pathlib import Path


def test_cli_module_can_run_from_source_tree():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")

    completed = subprocess.run(
        [sys.executable, "-m", "tugboat.cli", "doctor"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0
    assert "tugboat: ok" in completed.stdout


def test_cli_module_can_run_audit_from_source_tree(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tugboat.cli",
            "audit",
            "--repo",
            str(repo),
            "--trace",
            str(trace),
            "--mock-llmff-inspect",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert "audit run:" in completed.stdout
