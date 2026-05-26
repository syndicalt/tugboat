from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tugboat.llmff.runner import (
    CheckpointMismatchError,
    MissingOutputError,
    OutputPathError,
    run_manifest,
)
from tugboat.models import Policy
from tugboat.security.secrets import SecretScanError


def test_run_manifest_invokes_subprocess_with_file_backed_streams(
    monkeypatch, tmp_path: Path
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    run_manifest(
        manifest,
        run_dir=run_dir,
        policy=Policy(llmff_binary="custom-llmff"),
        timeout_ms=12_000,
        retry_attempts=2,
        retry_backoff_ms=250,
    )

    assert calls == [
        (
            (
                [
                    "custom-llmff",
                    "run",
                    str(manifest),
                    "--trace",
                    str(run_dir / "episode-audit" / "llmff-trace.jsonl"),
                    "--events",
                    str(run_dir / "episode-audit" / "llmff-events.jsonl"),
                    "--checkpoint",
                    str(run_dir / "episode-audit" / "checkpoint.json"),
                    "--timeout-ms",
                    "12000",
                    "--retry-attempts",
                    "2",
                    "--retry-backoff-ms",
                    "250",
                ],
            ),
            {"check": False, "capture_output": True, "text": True},
        )
    ]


def test_run_manifest_invokes_subprocess_with_explicit_inputs_and_outputs(
    monkeypatch, tmp_path: Path
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        output_path = Path(args[0][args[0].index("--output") + 2])
        output_path.write_text("{}\n", encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    result = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=Policy(llmff_binary="custom-llmff"),
        timeout_ms=12_000,
        retry_attempts=2,
        retry_backoff_ms=250,
        input_paths={"episode_trace": run_dir / "trace-input.jsonl"},
        output_paths={"audit_report": run_dir / "audit.raw.json"},
    )

    command = calls[0][0][0]
    assert command[-6:] == [
        "--input",
        "episode_trace",
        str(run_dir / "trace-input.jsonl"),
        "--output",
        "audit_report",
        str(run_dir / "audit.raw.json"),
    ]
    assert result.output_paths == {"audit_report": run_dir / "audit.raw.json"}


def test_successful_run_returns_exit_code_and_artifact_paths(monkeypatch, tmp_path: Path):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    result = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=Policy(),
        timeout_ms=12_000,
        retry_attempts=2,
        retry_backoff_ms=250,
    )

    assert result.exit_code == 0
    assert result.trace_path == run_dir / "episode-audit" / "llmff-trace.jsonl"
    assert result.events_path == run_dir / "episode-audit" / "llmff-events.jsonl"
    assert result.checkpoint_path == run_dir / "episode-audit" / "checkpoint.json"
    assert result.failure_kind is None
    assert result.failure_message is None


def test_failed_run_returns_sanitized_last_run_failed_event(
    monkeypatch, tmp_path: Path
):
    def fake_run(*args, **kwargs):
        events_path = Path(args[0][args[0].index("--events") + 1])
        events_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "event": "run_failed",
                            "failure_kind": "provider_error",
                            "failure_message": "first failure",
                        }
                    ),
                    json.dumps(
                        {
                            "event": "step_finished",
                            "payload": {"prompt": "do not read me"},
                        }
                    ),
                    json.dumps(
                        {
                            "event": "run_failed",
                            "failure_kind": "timeout",
                            "failure_message": "Timed out after 12000 ms",
                            "payload": {"model": "do not read me"},
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 7, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")

    result = run_manifest(
        manifest,
        run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
        policy=Policy(),
        timeout_ms=12_000,
        retry_attempts=2,
        retry_backoff_ms=250,
    )

    assert result.exit_code == 7
    assert result.failure_kind == "timeout"
    assert result.failure_message == "Timed out after 12000 ms"


def test_non_json_event_lines_are_ignored_safely(monkeypatch, tmp_path: Path):
    def fake_run(*args, **kwargs):
        events_path = Path(args[0][args[0].index("--events") + 1])
        events_path.write_text(
            "not json\n"
            + json.dumps(
                {
                    "event": "run_failed",
                    "failure_kind": "validation_error",
                    "failure_message": "Invalid manifest output",
                }
            )
            + "\n{not json either\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 3, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")

    result = run_manifest(
        manifest,
        run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
        policy=Policy(),
        timeout_ms=12_000,
        retry_attempts=2,
        retry_backoff_ms=250,
    )

    assert result.exit_code == 3
    assert result.failure_kind == "validation_error"
    assert result.failure_message == "Invalid manifest output"


def test_run_manifest_rejects_checkpoint_for_different_manifest(tmp_path: Path):
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    checkpoint_path = run_dir / "episode-audit" / "checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text(
        json.dumps({"manifest_hash": "different"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CheckpointMismatchError, match="manifest hash"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )


def test_run_manifest_rejects_outputs_outside_run_dir(tmp_path: Path):
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(OutputPathError, match="outside run directory"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
            output_paths={"audit_report": tmp_path / "outside.json"},
        )


def test_run_manifest_rejects_successful_run_missing_declared_output(
    monkeypatch, tmp_path: Path
):
    def fake_run(*args, **kwargs):
        events_path = Path(args[0][args[0].index("--events") + 1])
        events_path.write_text('{"event":"run_completed"}\n', encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(MissingOutputError, match="audit_report"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
            output_paths={"audit_report": run_dir / "audit.raw.json"},
        )


def test_run_manifest_rejects_secret_in_checkpoint(monkeypatch, tmp_path: Path):
    def fake_run(*args, **kwargs):
        checkpoint_path = Path(args[0][args[0].index("--checkpoint") + 1])
        checkpoint_path.write_text(
            json.dumps({"token": "ghp_abcdefghijklmnopqrstuvwx"}) + "\n",
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")

    with pytest.raises(SecretScanError, match="ghp_token"):
        run_manifest(
            manifest,
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
            policy=Policy(),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )
