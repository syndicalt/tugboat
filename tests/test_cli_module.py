import json
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


def test_package_module_can_run_from_source_tree():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")

    completed = subprocess.run(
        [sys.executable, "-m", "tugboat", "doctor"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
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


def test_cli_module_can_run_mcp_stdio_from_source_tree(tmp_path: Path):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd() / "src")
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {tmp_path.resolve().as_posix()}
""".lstrip(),
        encoding="utf-8",
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "tugboat_status",
            "arguments": {"repo": str(tmp_path)},
        },
    }

    completed = subprocess.run(
        [sys.executable, "-m", "tugboat.cli", "mcp", "stdio"],
        input=json.dumps(request) + "\n",
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert response["id"] == 1
    assert response["result"]["content"][0]["json"]["mode"] == "proposal_only"
