import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from stat import S_IMODE

from tugboat.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def test_fresh_repo_runs_credential_free_proposal_loop_with_shipped_fixture_backend(
    tmp_path: Path,
):
    repo = tmp_path / "fresh"
    repo.mkdir()
    codex = repo / "CODEX.md"
    original = "# Rules\n\nUse tests.\n"
    codex.write_text(original, encoding="utf-8")
    trace = repo / "traces" / "episode.jsonl"
    trace.parent.mkdir()
    trace.write_text(
        '{"type":"user_request","text":"Fix bug"}\n'
        '{"type":"user_correction","text":"You skipped the regression test"}\n',
        encoding="utf-8",
    )

    assert main(["init", "--repo", str(repo)]) == 0
    assert main(["index", "--repo", str(repo)]) == 0
    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0
    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 0
    assert main(["report", "--repo", str(repo), "--run", "latest"]) == 0

    assert codex.read_text(encoding="utf-8") == original
    policy = (repo / ".sidecar" / "policy.yaml").read_text(encoding="utf-8")
    assert "allow_network: false" in policy
    assert "allowed_providers" not in policy
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert (run_dir / "audit.json").exists()
    assert (run_dir / "candidate.json").exists()
    assert (run_dir / "candidate.diff").exists()
    assert (run_dir / "eval-report.json").exists()
    assert (run_dir / "report.md").exists()
    inspect = json.loads(
        (run_dir / "patch-propose" / "llmff-inspect.json").read_text(encoding="utf-8")
    )
    assert inspect["network_required"] is False
    assert inspect["external_calls"] == []
    assert inspect["inspect"]["fixture_backend"] == "tugboat"


def test_missing_llmff_binary_returns_clear_adoption_error(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    trace = repo / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
llmff:
  binary: definitely-missing-tugboat-llmff
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 1

    output = capsys.readouterr().out
    assert "instruction index blocked: llmff inspect failed: binary not found" in output
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "llmff_inspect_failed"
    assert audit["llmff_failure_kind"] == "inspect_failed"


def test_audit_missing_trace_returns_clear_error_without_traceback(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    missing = repo / "traces" / "session.jsonl"

    assert main(["audit", "--repo", str(repo), "--trace", str(missing)]) == 1

    output = capsys.readouterr().out
    assert f"audit blocked: trace file not found: {missing}" in output
    assert f"next: create or export the trace file at {missing}" in output
    assert "Traceback" not in output
    assert not (repo / ".sidecar" / "runs").exists()


def test_audit_trace_directory_returns_clear_error_without_traceback(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_dir = repo / "traces"
    trace_dir.mkdir()

    assert main(["audit", "--repo", str(repo), "--trace", str(trace_dir)]) == 1

    output = capsys.readouterr().out
    assert f"audit blocked: trace path is not a file: {trace_dir}" in output
    assert f"next: pass a trace file path instead of directory {trace_dir}" in output
    assert "Traceback" not in output
    assert not (repo / ".sidecar" / "runs").exists()


def test_optimize_missing_trace_returns_clear_error_without_traceback(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    missing = repo / "traces" / "session.jsonl"

    assert main(["optimize", "--repo", str(repo), "--trace", str(missing), "--suite", "all"]) == 1

    output = capsys.readouterr().out
    assert f"audit blocked: trace file not found: {missing}" in output
    assert f"next: create or export the trace file at {missing}" in output
    assert "Traceback" not in output
    assert not (repo / ".sidecar" / "runs").exists()


def test_optimize_trace_directory_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    trace_dir = repo / "traces"
    trace_dir.mkdir()

    assert (
        main(["optimize", "--repo", str(repo), "--trace", str(trace_dir), "--suite", "all"])
        == 1
    )

    output = capsys.readouterr().out
    assert f"audit blocked: trace path is not a file: {trace_dir}" in output
    assert f"next: pass a trace file path instead of directory {trace_dir}" in output
    assert "Traceback" not in output
    assert not (repo / ".sidecar" / "runs").exists()


def test_optimize_malformed_json_trace_returns_actionable_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "claude.json"
    trace.write_text('{"messages": [', encoding="utf-8")

    assert main(["optimize", "--repo", str(repo), "--trace", str(trace), "--suite", "all"]) == 1

    output = capsys.readouterr().out
    assert (
        "audit blocked: invalid trace: JSON trace contains invalid JSON "
        "at line 1 column 15"
    ) in output
    assert "next: validate the trace as JSONL or JSON and rerun with --trace-format auto" in output
    assert "Traceback" not in output
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "invalid_trace"
    assert audit["llmff_failure_message"] == (
        "JSON trace contains invalid JSON at line 1 column 15"
    )


def test_optimize_index_budget_failure_exits_cleanly_and_records_failed_run(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    sidecar = repo / ".sidecar"
    docs.mkdir()
    sidecar.mkdir()
    (docs / "one.md").write_text("# One\n\nFirst.\n", encoding="utf-8")
    (docs / "two.md").write_text("# Two\n\nSecond.\n", encoding="utf-8")
    (sidecar / "policy.yaml").write_text(
        """
version: 1
index:
  max_instruction_files: 1
instruction_files:
  - path: docs/**/*.md
    kind: repo_policy
    precedence: 50
    protected: true
llmff:
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )
    trace = repo / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    exit_code = main(["optimize", "--repo", str(repo), "--trace", str(trace), "--suite", "all"])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "audit blocked: instruction file budget exceeded: 2 discovered, limit 1" in output
    assert "Traceback" not in output
    run_dirs = list((sidecar / "runs").iterdir())
    assert len(run_dirs) == 1
    with closing(sqlite3.connect(sidecar / "db.sqlite")) as connection:
        rows = connection.execute("SELECT stage, status FROM runs ORDER BY rowid").fetchall()
    assert rows[-1] == ("audit", "failed")


def test_audit_index_budget_failure_exits_cleanly_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    sidecar = repo / ".sidecar"
    docs.mkdir()
    sidecar.mkdir()
    (docs / "one.md").write_text("# One\n\nFirst.\n", encoding="utf-8")
    (docs / "two.md").write_text("# Two\n\nSecond.\n", encoding="utf-8")
    (sidecar / "policy.yaml").write_text(
        """
version: 1
index:
  max_instruction_files: 1
instruction_files:
  - path: docs/**/*.md
    kind: repo_policy
    precedence: 50
    protected: true
""".lstrip(),
        encoding="utf-8",
    )
    trace = repo / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    exit_code = main(["audit", "--repo", str(repo), "--trace", str(trace)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "audit blocked: instruction file budget exceeded: 2 discovered, limit 1" in output
    assert "Traceback" not in output


def test_audit_trace_input_budget_failure_exits_before_run_artifacts(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        """
version: 1
trace:
  max_input_bytes: 10
""".lstrip(),
        encoding="utf-8",
    )
    trace = repo / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"This is too large"}\n', encoding="utf-8")
    trace_size = trace.stat().st_size

    exit_code = main(["audit", "--repo", str(repo), "--trace", str(trace)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert (
        f"audit blocked: trace input size budget exceeded: {trace_size} bytes, limit 10"
        in output
    )
    assert "Traceback" not in output
    assert not (sidecar / "runs").exists()
    assert not (sidecar / "db.sqlite").exists()


def test_optimize_train_trace_input_budget_failure_exits_before_run_artifacts(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        """
version: 1
trace:
  max_input_bytes: 10
""".lstrip(),
        encoding="utf-8",
    )
    trigger = repo / "trigger.jsonl"
    trigger.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    train = repo / "train.jsonl"
    train.write_text('{"type":"user_request","text":"Training trace is too large"}\n', encoding="utf-8")
    train_size = train.stat().st_size

    exit_code = main(
        [
            "optimize",
            "--repo",
            str(repo),
            "--trace",
            str(trigger),
            "--train-trace",
            str(train),
            "--suite",
            "all",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert (
        f"audit blocked: trace input size budget exceeded: {train_size} bytes, limit 10"
        in output
    )
    assert "Traceback" not in output
    assert not (sidecar / "runs").exists()
    assert not (sidecar / "db.sqlite").exists()


def test_audit_trace_event_budget_failure_writes_failed_audit_without_raw_payload(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    (sidecar / "policy.yaml").write_text(
        """
version: 1
trace:
  max_events: 2
instruction_files:
  - path: CODEX.md
    kind: agent_policy
    precedence: 70
    protected: true
""".lstrip(),
        encoding="utf-8",
    )
    trace = repo / "trace.jsonl"
    trace.write_text(
        '{"type":"user_request","text":"Fix one"}\n'
        '{"type":"tool_call","tool":"pytest"}\n'
        '{"type":"tool_result","tool":"pytest","output":"RAW_PAYLOAD_SHOULD_NOT_LEAK"}\n',
        encoding="utf-8",
    )

    exit_code = main(
        [
            "audit",
            "--repo",
            str(repo),
            "--trace",
            str(trace),
            "--trace-format",
            "generic-jsonl",
            "--mock-llmff-inspect",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "audit blocked: trace event budget exceeded: 3 events, limit 2" in output
    assert "Traceback" not in output
    run_dir = next((sidecar / "runs").iterdir())
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "trace_event_budget_exceeded"
    assert audit["llmff_failure_message"] == "trace event budget exceeded: 3 events, limit 2"
    assert "RAW_PAYLOAD_SHOULD_NOT_LEAK" not in json.dumps(audit)


def test_optimize_missing_trace_cli_exits_cleanly_without_python_traceback(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    missing = repo / "traces" / "session.jsonl"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tugboat",
            "optimize",
            "--repo",
            str(repo),
            "--trace",
            str(missing),
            "--suite",
            "all",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert f"audit blocked: trace file not found: {missing}" in result.stdout
    assert "Traceback" not in result.stdout


def test_optimize_malformed_policy_cli_exits_cleanly_without_python_traceback(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: [\n", encoding="utf-8")
    trace = repo / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tugboat",
            "optimize",
            "--repo",
            str(repo),
            "--trace",
            str(trace),
            "--suite",
            "all",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "optimize blocked: policy invalid:" in result.stdout
    assert "Traceback" not in result.stdout


def test_audit_malformed_policy_cli_exits_cleanly_without_python_traceback(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: [\n", encoding="utf-8")
    trace = repo / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tugboat",
            "audit",
            "--repo",
            str(repo),
            "--trace",
            str(trace),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "audit blocked: policy invalid:" in result.stdout
    assert "Traceback" not in result.stdout


def test_propose_malformed_policy_cli_exits_cleanly_without_python_traceback(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    (repo / ".sidecar" / "policy.yaml").write_text("version: [\n", encoding="utf-8")
    (run_dir / "audit.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audit_id": 1,
                "edit_warranted": True,
                "evidence_refs": ["ev-1"],
                "failure_class": "instruction_missing",
                "severity": "medium",
                "confidence": 0.9,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "audit.raw.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "instruction-index.raw.json").write_text("{}\n", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tugboat",
            "propose",
            "--repo",
            str(repo),
            "--audit",
            "run-1",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "propose blocked: policy invalid:" in result.stdout
    assert "Traceback" not in result.stdout


def test_propose_malformed_policy_main_returns_clear_error(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: [\n", encoding="utf-8")

    assert main(["propose", "--repo", str(repo), "--audit", "missing"]) == 1

    output = capsys.readouterr().out
    assert "propose blocked: policy invalid:" in output
    assert "missing" not in output


def test_eval_malformed_policy_cli_exits_cleanly_without_python_traceback(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    (repo / ".sidecar" / "policy.yaml").write_text("version: [\n", encoding="utf-8")
    _write_report_candidate_artifacts(run_dir)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "tugboat",
            "eval",
            "--repo",
            str(repo),
            "--candidate",
            "run-1",
            "--suite",
            "all",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "eval blocked: policy invalid:" in result.stdout
    assert "Traceback" not in result.stdout


def test_eval_malformed_policy_main_returns_clear_error(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: [\n", encoding="utf-8")

    assert main(["eval", "--repo", str(repo), "--candidate", "missing", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    assert "eval blocked: policy invalid:" in output
    assert "missing" not in output


def test_optimize_missing_train_trace_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    main_trace = repo / "trace.jsonl"
    main_trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    missing_train = repo / "traces" / "success.jsonl"

    assert (
        main(
            [
                "optimize",
                "--repo",
                str(repo),
                "--trace",
                str(main_trace),
                "--train-trace",
                str(missing_train),
                "--suite",
                "all",
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert f"audit blocked: trace file not found: {missing_train}" in output
    assert f"next: create or export the trace file at {missing_train}" in output
    assert "Traceback" not in output
    assert not (repo / ".sidecar" / "runs").exists()


def test_optimize_train_trace_directory_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    main_trace = repo / "trace.jsonl"
    main_trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    train_dir = repo / "traces"
    train_dir.mkdir()

    assert (
        main(
            [
                "optimize",
                "--repo",
                str(repo),
                "--trace",
                str(main_trace),
                "--train-trace",
                str(train_dir),
                "--suite",
                "all",
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert f"audit blocked: trace path is not a file: {train_dir}" in output
    assert f"next: pass a trace file path instead of directory {train_dir}" in output
    assert "Traceback" not in output
    assert not (repo / ".sidecar" / "runs").exists()


def test_audit_invalid_trace_returns_clear_error_without_traceback(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = repo / "trace.jsonl"
    trace.write_text("{not-json\n", encoding="utf-8")

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 1

    output = capsys.readouterr().out
    assert "audit blocked: invalid trace:" in output
    assert "next: validate the trace as JSONL or JSON and rerun with --trace-format auto" in output
    assert "Traceback" not in output
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "invalid_trace"


def test_audit_empty_trace_returns_clear_error_without_traceback(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = repo / "trace.jsonl"
    trace.write_text("\n\n", encoding="utf-8")

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 1

    output = capsys.readouterr().out
    assert "audit blocked: invalid trace: trace contains no events" in output
    assert "Traceback" not in output
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "invalid_trace"
    assert audit["llmff_failure_message"] == "trace contains no events"


def test_audit_forced_trace_format_mismatch_returns_actionable_hint(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = repo / "trace.jsonl"
    trace.write_text(
        json.dumps({"type": "user_request", "content": "Fix bug"}) + "\n",
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
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert (
        "audit blocked: invalid trace: trace format claude produced no recognized "
        "events; rerun with --trace-format auto or generic-jsonl"
    ) in output
    assert "next: rerun with --trace-format auto or generic-jsonl" in output
    assert "Traceback" not in output
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "invalid_trace"
    assert audit["llmff_failure_message"] == (
        "trace format claude produced no recognized events; "
        "rerun with --trace-format auto or generic-jsonl"
    )


def test_optimize_forced_trace_format_mismatch_returns_actionable_hint(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = repo / "trace.jsonl"
    trace.write_text(
        json.dumps({"type": "user_request", "content": "Fix bug"}) + "\n",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "optimize",
                "--repo",
                str(repo),
                "--trace",
                str(trace),
                "--suite",
                "all",
                "--trace-format",
                "claude",
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert (
        "audit blocked: invalid trace: trace format claude produced no recognized "
        "events; rerun with --trace-format auto or generic-jsonl"
    ) in output
    assert "next: rerun with --trace-format auto or generic-jsonl" in output
    assert "Traceback" not in output
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "invalid_trace"
    assert audit["llmff_failure_message"] == (
        "trace format claude produced no recognized events; "
        "rerun with --trace-format auto or generic-jsonl"
    )


def test_propose_missing_latest_run_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 1

    output = capsys.readouterr().out
    assert "propose blocked: no tugboat run directories exist" in output
    assert "Traceback" not in output


def test_eval_missing_latest_run_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    assert "eval blocked: no tugboat run directories exist" in output
    assert "Traceback" not in output


def test_propose_malformed_audit_artifact_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "audit.json").write_text("{}\n", encoding="utf-8")

    assert main(["propose", "--repo", str(repo), "--audit", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "propose blocked: audit.json missing required field: schema_version" in output
    assert "Traceback" not in output


def test_propose_missing_audit_artifact_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    (repo / ".sidecar" / "runs" / "run-1").mkdir(parents=True)

    assert main(["propose", "--repo", str(repo), "--audit", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "propose blocked:" in output
    assert "audit.json" in output
    assert "Traceback" not in output


def test_eval_malformed_candidate_artifact_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate.json").write_text("{}\n", encoding="utf-8")

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    assert "eval blocked: candidate.json missing required field: schema_version" in output
    assert "Traceback" not in output


def test_eval_missing_candidate_artifact_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    (repo / ".sidecar" / "runs" / "run-1").mkdir(parents=True)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    assert "eval blocked:" in output
    assert "candidate.json" in output
    assert "Traceback" not in output


def _write_report_candidate_artifacts(run_dir: Path) -> None:
    diff = ""
    (run_dir / "candidate.diff").write_text(diff, encoding="utf-8")
    (run_dir / "candidate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audit_id": 1,
                "candidate_id": 1,
                "base_file": "CODEX.md",
                "base_hash": "abc",
                "diff_hash": "e3b0c44298fc1c149afbf4c8996fb924"
                "27ae41e4649b934ca495991b7852b855",
                "expected_behavior_change": "No behavior change.",
                "evals_required": [],
                "risk_class": "low",
                "rationale": "Test candidate.",
                "rollback_plan": [],
                "sources": [{"source_id": "ev-1", "trusted": True}],
                "bounded_edit_metadata": [
                    {
                        "operator": "add",
                        "file": "CODEX.md",
                        "section": "Rules",
                        "changed_lines": 1,
                        "normative_changes": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_report_malformed_candidate_artifact_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate.json").write_text("{}\n", encoding="utf-8")
    (run_dir / "candidate.diff").write_text("", encoding="utf-8")
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True, "reasons": []}),
        encoding="utf-8",
    )

    assert main(["report", "--repo", str(repo), "--run", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "report blocked: candidate.json missing required field:" in output
    assert "Traceback" not in output
    assert not (run_dir / "report.md").exists()


def test_report_missing_policy_gate_artifact_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_report_candidate_artifacts(run_dir)

    assert main(["report", "--repo", str(repo), "--run", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "report blocked: missing artifact: policy-gate.json" in output
    assert "Traceback" not in output
    assert not (run_dir / "report.md").exists()


def test_report_policy_gate_missing_field_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_report_candidate_artifacts(run_dir)
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True}),
        encoding="utf-8",
    )

    assert main(["report", "--repo", str(repo), "--run", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "report blocked: policy-gate.json missing required field: reasons" in output
    assert "Traceback" not in output
    assert not (run_dir / "report.md").exists()


def test_report_malformed_eval_report_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_report_candidate_artifacts(run_dir)
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True, "reasons": []}),
        encoding="utf-8",
    )
    (run_dir / "eval-report.json").write_text("[]\n", encoding="utf-8")

    assert main(["report", "--repo", str(repo), "--run", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "report blocked: eval report must be a JSON object" in output
    assert "Traceback" not in output
    assert not (run_dir / "report.md").exists()


def test_report_non_object_candidate_artifact_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "candidate.json").write_text("[]\n", encoding="utf-8")
    (run_dir / "candidate.diff").write_text("", encoding="utf-8")
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True, "reasons": []}),
        encoding="utf-8",
    )

    assert main(["report", "--repo", str(repo), "--run", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "report blocked: candidate.json must be a JSON object" in output
    assert "Traceback" not in output
    assert not (run_dir / "report.md").exists()


def test_report_malformed_candidate_sources_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    diff = ""
    (run_dir / "candidate.diff").write_text(diff, encoding="utf-8")
    (run_dir / "candidate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audit_id": 1,
                "candidate_id": 1,
                "base_file": "CODEX.md",
                "base_hash": "abc",
                "diff_hash": "e3b0c44298fc1c149afbf4c8996fb924"
                "27ae41e4649b934ca495991b7852b855",
                "expected_behavior_change": "No behavior change.",
                "evals_required": [],
                "risk_class": "low",
                "rationale": "Test candidate.",
                "rollback_plan": [],
                "sources": "ev-1",
                "bounded_edit_metadata": [
                    {
                        "operator": "add",
                        "file": "CODEX.md",
                        "section": "Rules",
                        "changed_lines": 1,
                        "normative_changes": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True, "reasons": []}),
        encoding="utf-8",
    )

    assert main(["report", "--repo", str(repo), "--run", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "report blocked: candidate.json field has wrong type: sources" in output
    assert "Traceback" not in output
    assert not (run_dir / "report.md").exists()


def test_report_non_object_policy_gate_artifact_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_report_candidate_artifacts(run_dir)
    (run_dir / "policy-gate.json").write_text("[]\n", encoding="utf-8")

    assert main(["report", "--repo", str(repo), "--run", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "report blocked: policy-gate.json must be a JSON object" in output
    assert "Traceback" not in output
    assert not (run_dir / "report.md").exists()


def test_report_malformed_policy_gate_allowed_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_report_candidate_artifacts(run_dir)
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": "true", "reasons": []}),
        encoding="utf-8",
    )

    assert main(["report", "--repo", str(repo), "--run", "run-1"]) == 1

    output = capsys.readouterr().out
    assert "report blocked: policy-gate.json field has wrong type: allowed" in output
    assert "Traceback" not in output
    assert not (run_dir / "report.md").exists()


def test_report_latest_without_runs_returns_clear_error_without_traceback(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()

    assert main(["report", "--repo", str(repo), "--run", "latest"]) == 1

    output = capsys.readouterr().out
    assert "report blocked: no tugboat run directories exist" in output
    assert "Traceback" not in output


def _write_fake_llmff(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:3] == ["inspect", "--format", "json"]:
    provider_backed = "provider" in Path(sys.argv[0]).name
    print(json.dumps({
        "manifest": Path(args[3]).stem,
        "network_required": provider_backed,
        "providers": ["openai"] if provider_backed else [],
        "external_calls": [{"kind": "model_provider", "target": "openai"}] if provider_backed else [],
    }))
    raise SystemExit(0)

if args[:1] == ["run"]:
    manifest = Path(args[1]).stem
    trace = Path(args[args.index("--trace") + 1])
    events = Path(args[args.index("--events") + 1])
    checkpoint = Path(args[args.index("--checkpoint") + 1])
    outputs = {}
    inputs = {}
    index = 0
    while index < len(args):
        if args[index] == "--input":
            inputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        if args[index] == "--output":
            outputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        index += 1
    trace.write_text('{"event":"step"}\\n', encoding="utf-8")
    events.write_text('{"event":"run_completed"}\\n', encoding="utf-8")
    checkpoint.write_text(
        json.dumps({"manifest_hash": hashlib.sha256(Path(args[1]).read_bytes()).hexdigest()}) + "\\n",
        encoding="utf-8",
    )
    if manifest == "instruction-index":
        outputs["instruction_index"].write_text(json.dumps({
            "documents": [{
                "path": "CODEX.md",
                "obligations": ["Use tests."],
                "chunks": [{
                    "ref": "CODEX.md#rules",
                    "anchor": "rules",
                    "heading_path": ["Rules"],
                }],
            }]
        }) + "\\n", encoding="utf-8")
    elif manifest == "episode-audit":
        episode = json.loads(inputs["episode_trace"].read_text(encoding="utf-8"))
        evidence_id = next(
            (
                event["evidence_id"]
                for event in episode["events"]
                if event["event_type"] == "user_correction"
            ),
            episode["events"][0]["evidence_id"],
        )
        outputs["audit_report"].write_text(json.dumps({
            "edit_warranted": True,
            "failure_class": "instruction_missing",
            "severity": "medium",
            "confidence": 0.82,
            "evidence_refs": [evidence_id],
            "instruction_refs": ["CODEX.md#rules"],
        }) + "\\n", encoding="utf-8")
        outputs["evidence_ids"].write_text(json.dumps({
            "evidence_ids": [evidence_id],
        }) + "\\n", encoding="utf-8")
    elif manifest == "drift-detect":
        audit = json.loads(inputs["audit_reports"].read_text(encoding="utf-8"))
        evidence_refs = audit["evidence_refs"]
        outputs["drift_clusters"].write_text(json.dumps({
            "clusters": [{"cluster_id": "drift-1", "evidence_refs": evidence_refs}]
        }) + "\\n", encoding="utf-8")
        if "optimizer_notes" in outputs:
            outputs["optimizer_notes"].write_text(json.dumps({
                "notes": [{"summary": "Use drift evidence for the proposal.", "evidence_refs": evidence_refs}]
            }) + "\\n", encoding="utf-8")
    elif manifest == "patch-propose":
        repo = outputs["candidate_patch"].parents[3]
        base = repo / "CODEX.md"
        drift = json.loads(inputs["drift_clusters"].read_text(encoding="utf-8"))
        evidence_refs = drift["clusters"][0]["evidence_refs"]
        if "proposal_rationale" in outputs:
            outputs["proposal_rationale"].write_text(json.dumps({
                "rationale": "Patch proposal is grounded in e2e drift evidence.",
                "evidence_refs": evidence_refs,
                "style_constraints": ["Preserve concise instruction style."],
            }) + "\\n", encoding="utf-8")
        outputs["candidate_patch"].write_text(json.dumps({
            "base_file": "CODEX.md",
            "base_hash": hashlib.sha256(base.read_bytes()).hexdigest(),
            "diff": "--- a/CODEX.md\\n+++ b/CODEX.md\\n@@ -1,3 +1,4 @@\\n # Rules\\n \\n Use tests.\\n+Add regression-test guidance.\\n",
            "risk_class": "instruction_clarification",
            "rationale": "llmff proposed this from audited evidence",
            "expected_behavior_change": "Agents add regression-test guidance before closing fixes.",
            "evals_required": ["governance-regression"],
            "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
            "sources": [{"source_id": evidence_refs[0], "trusted": True}],
            "bounded_edit_metadata": [{
                "operator": "add",
                "file": "CODEX.md",
                "section": "Rules",
                "changed_lines": 1,
                "normative_changes": 0
            }],
        }) + "\\n", encoding="utf-8")
    elif manifest == "patch-eval":
        validation_splits = {
            "trigger": ["incident_replay:e2e-regression"],
            "held_out": ["held-out:e2e-no-regression"],
            "governance": ["governance:e2e-policy"],
        }
        outputs["eval_report"].write_text(json.dumps({
            "passed": True,
            "trigger_score": 0.75,
            "held_out_score": 0.88,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {
                "governance_regressions": 0,
                "held_out_cases": 3,
                "incident_replay_cases": 1,
            },
            "validation_splits": validation_splits,
            "eval_cases": [
                {
                    "case_id": case_id,
                    "case_hash": hashlib.sha256(case_id.encode("utf-8")).hexdigest(),
                    "split_name": split_name,
                }
                for split_name, case_ids in validation_splits.items()
                for case_id in case_ids
            ],
        }) + "\\n", encoding="utf-8")
        outputs["policy_decision"].write_text(json.dumps({
            "allowed": True,
            "reasons": [],
        }) + "\\n", encoding="utf-8")
    elif manifest == "acceptance-summary":
        outputs["acceptance_summary"].write_text(json.dumps({
            "decision_recommendation": "needs_review",
            "reasons": ["policy gate and eval report passed"],
            "evidence": ["audit:1"],
            "reviewer_checklist": [
                "Review candidate diff and proposal rationale against trace evidence.",
                "Confirm risk classification matches the bounded edit.",
                "Verify source evidence supports the recommendation.",
                "Confirm expected behavior change is narrow and intentional.",
                "Confirm rollback command before applying.",
            ],
            "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
        }) + "\\n", encoding="utf-8")
    raise SystemExit(0)

raise SystemExit(64)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_proposal_loop_writes_review_artifacts_without_mutating_instructions(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    original = "# Rules\n\nUse tests.\n"
    codex.write_text(original, encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        '{"type":"user_request","text":"Fix bug"}\n'
        '{"type":"user_correction","text":"You skipped the regression test"}\n',
        encoding="utf-8",
    )
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )

    previous_umask = os.umask(0o022)
    try:
        assert main(["index", "--repo", str(repo)]) == 0
        assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
        assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0
        assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 0
        capsys.readouterr()
        assert main(["inspect-decision", "--repo", str(repo), "--decision", "latest"]) == 0
        inspect_output = capsys.readouterr().out
        assert main(["report", "--repo", str(repo), "--run", "latest"]) == 0
    finally:
        os.umask(previous_umask)

    run_dirs = sorted((repo / ".sidecar" / "runs").iterdir())
    assert run_dirs
    run_dir = run_dirs[-1]
    assert f"decision_trace: {run_dir / 'decision-trace.json'}" in inspect_output
    assert f"run_id: {run_dir.name}" in inspect_output
    assert "decision: needs_review" in inspect_output
    assert "candidate_file: CODEX.md" in inspect_output
    assert "candidate_state: needs_review" in inspect_output
    assert "evals: all=passed" in inspect_output
    assert "rollback_ready: no" in inspect_output
    assert "review_next: inspect .sidecar/runs/" in inspect_output
    assert "You skipped the regression test" not in inspect_output
    assert "payload_snippet" not in inspect_output
    assert "[REDACTED:" not in inspect_output
    assert (run_dir / "trace-input.jsonl").exists()
    assert S_IMODE((run_dir / "trace-input.jsonl").stat().st_mode) == 0o600
    assert S_IMODE((run_dir / "trace-redacted.jsonl").stat().st_mode) == 0o600
    assert S_IMODE((run_dir / "patch-eval" / "llmff-trace.jsonl").stat().st_mode) == 0o600
    assert S_IMODE((run_dir / "patch-eval" / "llmff-events.jsonl").stat().st_mode) == 0o600
    assert S_IMODE((run_dir / "patch-eval" / "checkpoint.json").stat().st_mode) == 0o600
    assert (run_dir / "instruction-snapshot").is_dir()
    assert S_IMODE((run_dir / "audit.json").stat().st_mode) == 0o600
    assert S_IMODE((run_dir / "instruction-graph.json").stat().st_mode) == 0o600
    assert S_IMODE((run_dir / "instruction-snapshot").stat().st_mode) == 0o700
    assert S_IMODE((run_dir / "instruction-snapshot" / "CODEX.md").stat().st_mode) == 0o600
    manifest_dir = repo / ".sidecar" / "manifests"
    assert sorted(path.name for path in manifest_dir.glob("*.yaml")) == [
        "acceptance-summary.yaml",
        "drift-detect.yaml",
        "episode-audit.yaml",
        "instruction-index.yaml",
        "patch-eval.yaml",
        "patch-propose.yaml",
    ]
    inspect = json.loads(
        (run_dir / "patch-eval" / "llmff-inspect.json").read_text(encoding="utf-8")
    )
    assert inspect["manifest_path"].endswith(".sidecar/manifests/patch-eval.yaml")
    assert (run_dir / "audit.json").exists()
    assert (run_dir / "candidate.diff").exists()
    assert (run_dir / "candidate.json").exists()
    assert (run_dir / "policy-gate.json").exists()
    assert (run_dir / "eval-report.json").exists()
    assert (run_dir / "acceptance-summary.raw.json").exists()
    assert (run_dir / "optimization-summary.json").exists()
    assert (run_dir / "decision.json").exists()
    assert (run_dir / "decision-trace.json").exists()
    assert (run_dir / "report.md").exists()
    decision_trace = json.loads((run_dir / "decision-trace.json").read_text(encoding="utf-8"))
    assert f"candidate_id: {decision_trace['candidate']['candidate_id']}" in inspect_output
    assert f"risk_class: {decision_trace['candidate']['risk_class']}" in inspect_output
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    evidence_id = audit["evidence_refs"][0]
    assert decision_trace["schema_version"] == 1
    assert decision_trace["decision"]["decision"] == "needs_review"
    assert decision_trace["decision"]["event_hash"]
    assert decision_trace["candidate"]["base_file"] == "CODEX.md"
    assert decision_trace["candidate"]["diff_path"].endswith("candidate.diff")
    assert decision_trace["audit"]["evidence_refs"] == [evidence_id]
    assert decision_trace["audit"]["instruction_refs"] == ["CODEX.md#rules"]
    assert decision_trace["trace_events"] == [
        {
            "audit_event_sequence": decision_trace["trace_events"][0]["audit_event_sequence"],
            "event_hash": decision_trace["trace_events"][0]["event_hash"],
            "event_type": "user_correction",
            "evidence_id": evidence_id,
            "line_number": 2,
            "payload_snippet": decision_trace["trace_events"][0]["payload_snippet"],
            "payload_truncated": False,
            "source_trust": "user",
        }
    ]
    assert "You skipped the regression test" in decision_trace["trace_events"][0]["payload_snippet"]
    assert decision_trace["evals"][0]["suite_id"] == "all"
    assert decision_trace["evals"][0]["passed"] is True
    assert [
        job["manifest_name"] for job in decision_trace["llmff_jobs"]
    ] == [
        "instruction-index.yaml",
        "episode-audit.yaml",
        "drift-detect.yaml",
        "patch-propose.yaml",
        "patch-eval.yaml",
        "acceptance-summary.yaml",
    ]
    assert all(job["status"] == "completed" for job in decision_trace["llmff_jobs"])
    assert all(job["exit_code"] == 0 for job in decision_trace["llmff_jobs"])
    assert {
        output["output_name"]
        for job in decision_trace["llmff_jobs"]
        for output in job["outputs"]
    } >= {
        "audit_report",
        "candidate_patch",
        "eval_report",
        "acceptance_summary",
    }
    assert all(
        "payload" not in event
        for job in decision_trace["llmff_jobs"]
        for event in job["events"]
    )
    assert decision_trace["artifacts"]["candidate_diff"].endswith("candidate.diff")
    assert decision_trace["artifacts"]["decision_artifact"].endswith("decision.json")
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "- source_evidence: " in report
    assert "- expected_behavior_change: " in report
    assert "payload_snippet" not in report
    assert "You skipped the regression test" not in report
    for artifact_ref in (
        "trace_input: .sidecar/runs/",
        "instruction_snapshot: .sidecar/runs/",
        "instruction_graph: .sidecar/runs/",
        "audit_report: .sidecar/runs/",
        "candidate_metadata: .sidecar/runs/",
        "candidate_diff: .sidecar/runs/",
        "policy_gate: .sidecar/runs/",
        "eval_report: .sidecar/runs/",
        "decision_artifact: .sidecar/runs/",
        "acceptance_summary: .sidecar/runs/",
    ):
        assert artifact_ref in report
    assert "- acceptance_reason: policy gate and eval report passed" in report
    assert (
        "- reviewer_checklist: Review candidate diff and proposal rationale against trace evidence.; "
        "Confirm risk classification matches the bounded edit.; Verify source evidence supports "
        "the recommendation.; Confirm expected behavior change is narrow and intentional.; "
        "Confirm rollback command before applying."
    ) in report
    assert "- rollback_command: tugboat rollback --decision latest" in report
    assert json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "allowed": True,
        "reasons": [],
    }
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    optimization_summary = json.loads(
        (run_dir / "optimization-summary.json").read_text(encoding="utf-8")
    )
    decision = json.loads((run_dir / "decision.json").read_text(encoding="utf-8"))
    assert audit["schema_version"] == 1
    assert candidate["schema_version"] == 1
    assert eval_report["schema_version"] == 1
    assert decision["schema_version"] == 1
    assert candidate["audit_id"] == audit["audit_id"]
    assert eval_report["candidate_id"] == candidate["candidate_id"]
    assert optimization_summary["decision"] == "needs_review"
    assert optimization_summary["accepted_bounded_edit_metadata"] == [
        {
            "changed_lines": 1,
            "file": "CODEX.md",
            "normative_changes": 0,
            "operator": "add",
            "section": "Rules",
        }
    ]
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0] == 2
        snapshot = connection.execute(
            """
            SELECT path, artifact_path, content_hash, audit_event_sequence
            FROM instruction_snapshots
            """
        ).fetchone()
        graph = connection.execute(
            """
            SELECT artifact_path, graph_hash, audit_event_sequence
            FROM instruction_graphs
            """
        ).fetchone()
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE stage = 'audit' AND episode_id IS NOT NULL"
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM audits").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM evals").fetchone()[0] >= 1
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] >= 1
        decision_id = connection.execute(
            """
            SELECT id
            FROM decisions
            WHERE candidate_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (candidate["candidate_id"],),
        ).fetchone()[0]
        assert connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] >= 5
    assert main(["inspect-decision", "--repo", str(repo), "--decision", run_dir.name]) == 0
    run_ref_trace = json.loads((run_dir / "decision-trace.json").read_text(encoding="utf-8"))
    assert run_ref_trace["decision_ref"] == run_dir.name
    assert run_ref_trace["decision"]["decision_id"] == decision_id
    assert main(["inspect-decision", "--repo", str(repo), "--decision", str(decision_id)]) == 0
    decision_id_trace = json.loads((run_dir / "decision-trace.json").read_text(encoding="utf-8"))
    assert decision_id_trace["decision_ref"] == str(decision_id)
    assert decision_id_trace["decision"]["decision_id"] == decision_id
    assert snapshot == (
        "CODEX.md",
        str(run_dir / "instruction-snapshot" / "CODEX.md"),
        snapshot[2],
        snapshot[3],
    )
    assert len(snapshot[2]) == 64
    assert snapshot[3] is not None
    assert graph == (str(run_dir / "instruction-graph.json"), graph[1], graph[2])
    assert len(graph[1]) == 64
    assert graph[2] is not None
    assert (run_dir / "instruction-graph.json").exists()
    assert codex.read_text(encoding="utf-8") == original


def test_auto_detected_codex_raw_episode_runs_full_proposal_loop(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    original = "# Rules\n\nUse tests.\n"
    codex.write_text(original, encoding="utf-8")
    trace = tmp_path / "codex-raw.jsonl"
    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: CODEX.md\n"
        "@@\n"
        " Use tests.\n"
        "+Add regression-test guidance.\n"
        "*** End Patch\n"
    )
    trace.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "base_instructions": {
                                "source": "CODEX.md",
                                "text": original,
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
                            "content": [{"type": "input_text", "text": "Fix the regression bug"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-pytest",
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
                            "call_id": "call-pytest",
                            "output": "1 failed\nProcess exited with code 1",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "call-patch",
                            "name": "apply_patch",
                            "arguments": patch_text,
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call-patch",
                            "output": "Success. Updated files.",
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
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0
    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 0
    assert main(["inspect-decision", "--repo", str(repo), "--decision", "latest"]) == 0
    assert main(["report", "--repo", str(repo), "--run", "latest"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    canonical = json.loads((run_dir / "canonical-episode.json").read_text(encoding="utf-8"))
    event_types = [event["event_type"] for event in canonical["events"]]
    assert event_types == [
        "instruction_snapshot",
        "user_request",
        "tool_call",
        "tool_result",
        "test_result",
        "tool_call",
        "diff",
        "tool_result",
        "final_answer",
    ]
    assert canonical["events"][6]["payload"]["path"] == "CODEX.md"
    assert canonical["events"][4]["payload"]["passed"] is False
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    decision_trace = json.loads((run_dir / "decision-trace.json").read_text(encoding="utf-8"))
    evidence_id = canonical["events"][0]["evidence_id"]
    assert audit["evidence_refs"] == [evidence_id]
    assert candidate["sources"] == [{"source_id": evidence_id, "trusted": True}]
    assert decision_trace["trace_events"][0]["evidence_id"] == evidence_id
    assert decision_trace["trace_events"][0]["event_type"] == "instruction_snapshot"
    assert decision_trace["candidate"]["candidate_id"] == candidate["candidate_id"]
    assert (run_dir / "report.md").exists()
    assert codex.read_text(encoding="utf-8") == original


def test_inspect_decision_spans_codex_export_to_apply_and_rollback(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tugboat@example.test")
    _git(repo, "config", "user.name", "Tugboat Tests")
    codex = repo / "CODEX.md"
    original = "# Rules\n\nUse tests.\n"
    codex.write_text(original, encoding="utf-8")
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / ".gitignore").write_text("*\n!.gitignore\n!policy.yaml\n", encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "fake-llmff")
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    trace = FIXTURES / "traces" / "codex-local-session-export.jsonl"
    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0
    assert main(["eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all"]) == 0
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    assert "Add regression-test guidance." in codex.read_text(encoding="utf-8")
    assert main(["rollback", "--repo", str(repo), "--decision", "latest", "--execute"]) == 0
    assert codex.read_text(encoding="utf-8") == original
    assert main(["inspect-decision", "--repo", str(repo), "--decision", "latest"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    decision_trace = json.loads((run_dir / "decision-trace.json").read_text(encoding="utf-8"))
    canonical = json.loads((run_dir / "canonical-episode.json").read_text(encoding="utf-8"))
    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    rollback_plan = json.loads((run_dir / "rollback-plan.json").read_text(encoding="utf-8"))

    assert decision_trace["decision"]["decision"] == "applied"
    assert decision_trace["decision"]["applied_commit"] == apply_plan["applied_commit"]
    assert decision_trace["audit"]["evidence_refs"] == [canonical["events"][0]["evidence_id"]]
    assert decision_trace["trace_events"][0]["evidence_id"] == canonical["events"][0]["evidence_id"]
    assert decision_trace["trace_events"][0]["event_type"] == "instruction_snapshot"
    assert decision_trace["candidate"]["diff_path"] == decision_trace["artifacts"]["candidate_diff"]
    assert decision_trace["edit_operations"][0]["payload"] == {
        "changed_lines": 1,
        "file": "CODEX.md",
        "normative_changes": 0,
        "operator": "add",
        "section": "Rules",
    }
    assert {split["split_name"] for split in decision_trace["validation_splits"]} >= {
        "trigger",
        "held_out",
        "governance",
    }
    assert decision_trace["artifacts"]["apply_plan"] == apply_plan["provenance_bundle"].replace(
        "provenance-bundle.json",
        "apply-plan.json",
    )
    assert decision_trace["artifacts"]["provenance_bundle"] == apply_plan["provenance_bundle"]
    assert decision_trace["artifacts"]["rollback_plan"] == (
        ".sidecar/runs/" + run_dir.name + "/rollback-plan.json"
    )
    assert decision_trace["rollbacks"][0]["executed"] is True
    assert decision_trace["rollbacks"][0]["revert_commit"] == rollback_plan["revert_commit"]
    assert decision_trace["rollbacks"][0]["post_rollback_eval_result"]["restored_pre_hashes"] is True
    assert all(
        decision_trace[section][0]["event_hash"]
        for section in (
            "trace_events",
            "edit_operations",
            "eval_runs",
            "validation_splits",
            "rollbacks",
        )
    )


def test_provider_backed_llmff_requires_explicit_network_policy(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "provider-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
  allowed_providers:
    - openai
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 1

    output = capsys.readouterr().out
    assert "instruction index blocked:" in output
    assert "policy disallows network" in output


def test_provider_backed_llmff_requires_provider_allowlist(tmp_path: Path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "provider-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: true
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 1

    output = capsys.readouterr().out
    assert "instruction index blocked:" in output
    assert "provider is not allowed by policy: openai" in output


def test_provider_backed_llmff_opt_in_records_declared_provider_evidence(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")
    fake_llmff = _write_fake_llmff(tmp_path / "provider-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: true
  allowed_providers:
    - openai
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    inspect = json.loads(
        (run_dir / "instruction-index" / "llmff-inspect.json").read_text(encoding="utf-8")
    )
    assert inspect["network_required"] is True
    assert inspect["external_calls"] == [{"kind": "model_provider", "target": "openai"}]
    assert inspect["inspect"]["providers"] == ["openai"]


def test_mock_audit_records_chunk_granularity_instruction_refs(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Rules\n\nUse tests.\n\n## Review\n\nCheck the failure first.\n",
        encoding="utf-8",
    )
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    monkeypatch.setattr(
        "tugboat.audit.pipeline._scored_audit_payload",
        lambda bundle: {
            "edit_warranted": True,
            "evidence_refs": [event.evidence_id for event in bundle.events],
            "failure_class": "instruction_missing",
            "severity": "medium",
            "confidence": 0.75,
        },
    )

    assert main(["index", "--repo", str(repo)]) == 0
    assert main(["audit", "--repo", str(repo), "--trace", str(trace), "--mock-llmff-inspect"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    graph = json.loads((run_dir / "instruction-graph.json").read_text(encoding="utf-8"))
    expected_refs = ["CODEX.md#rules", "CODEX.md#review"]
    assert audit["instruction_refs"] == expected_refs
    assert [
        chunk["source_ref"]
        for document in graph["documents"]
        for chunk in document["chunks"]
    ] == expected_refs
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        stored_refs = json.loads(
            connection.execute("SELECT instruction_refs_json FROM audits").fetchone()[0]
        )
    assert stored_refs == expected_refs
