from __future__ import annotations

import json
import hashlib
import os
import subprocess
from pathlib import Path
from stat import S_IMODE

import pytest

from tugboat.llmff.runner import (
    CheckpointPathError,
    CheckpointMismatchError,
    InputPathError,
    InspectPolicyError,
    MissingOutputError,
    OutputPathError,
    run_manifest,
)
from tugboat.models import Policy
from tugboat.security.secrets import SecretScanError


def _manifest_sha256(manifest: Path) -> str:
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def _write_inspect_artifact(
    run_dir: Path,
    manifest: Path,
    *,
    manifest_hash: str | None = None,
    inspect_payload: dict[str, object] | None = None,
) -> Path:
    lifecycle_dir = run_dir / manifest.stem
    lifecycle_dir.mkdir(parents=True, exist_ok=True)
    inspect = inspect_payload or {
        "manifest": manifest.stem,
        "network_required": False,
        "providers": [],
        "external_calls": [],
    }
    artifact = {
        "schema_version": 1,
        "manifest_path": str(manifest),
        "manifest_hash": manifest_hash or _manifest_sha256(manifest),
        "network_required": bool(inspect.get("network_required", False)),
        "external_calls": inspect.get("external_calls", []),
        "inspect": inspect,
    }
    path = lifecycle_dir / "llmff-inspect.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def test_run_manifest_requires_matching_inspect_artifact_before_subprocess(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(InspectPolicyError, match="llmff inspect artifact is required"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=True),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )

    assert calls == []


def test_run_manifest_accepts_matching_inspect_artifact_before_subprocess(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _write_inspect_artifact(run_dir, manifest)

    result = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=Policy(llmff_require_inspect=True),
        timeout_ms=12_000,
        retry_attempts=2,
        retry_backoff_ms=250,
    )

    assert result.exit_code == 0
    assert len(calls) == 1


def test_run_manifest_rejects_stale_inspect_artifact_before_subprocess(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _write_inspect_artifact(run_dir, manifest, manifest_hash="old")

    with pytest.raises(InspectPolicyError, match="manifest hash does not match"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=True),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )

    assert calls == []


def test_run_manifest_rejects_invalid_inspect_artifact_json_before_subprocess(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    inspect_path = run_dir / manifest.stem / "llmff-inspect.json"
    inspect_path.parent.mkdir(parents=True)
    inspect_path.write_text("{not-json\n", encoding="utf-8")

    with pytest.raises(InspectPolicyError, match="valid JSON"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=True),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )

    assert calls == []


def test_run_manifest_rejects_inspect_artifact_outside_policy_allowlist(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _write_inspect_artifact(run_dir, manifest)

    with pytest.raises(InspectPolicyError, match="manifest hash is not allowed"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(
                llmff_require_inspect=True,
                allowed_manifest_hashes=("different",),
            ),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )

    assert calls == []


def test_run_manifest_rejects_inspect_artifact_provider_outside_policy_allowlist(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _write_inspect_artifact(
        run_dir,
        manifest,
        inspect_payload={
            "manifest": manifest.stem,
            "network_required": True,
            "providers": ["anthropic"],
            "external_calls": [{"kind": "model_provider", "target": "anthropic"}],
        },
    )

    with pytest.raises(InspectPolicyError, match="provider is not allowed"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(
                llmff_require_inspect=True,
                llmff_allow_network=True,
                llmff_allowed_providers=("openai",),
            ),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )

    assert calls == []


def test_run_manifest_rechecks_inspect_artifact_network_policy_before_subprocess(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    _write_inspect_artifact(
        run_dir,
        manifest,
        inspect_payload={
            "manifest": manifest.stem,
            "network_required": True,
            "providers": ["openai"],
            "external_calls": [{"kind": "model_provider", "target": "openai"}],
        },
    )

    with pytest.raises(InspectPolicyError, match="policy disallows network"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=True, llmff_allow_network=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )

    assert calls == []


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
        policy=Policy(llmff_binary="custom-llmff", llmff_require_inspect=False),
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
            {"check": False, "capture_output": True, "text": True, "timeout": 12.0},
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
    input_path = run_dir / "trace-input.jsonl"
    input_path.parent.mkdir(parents=True)
    input_path.write_text('{"type":"user_request","text":"Fix"}\n', encoding="utf-8")

    result = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=Policy(llmff_binary="custom-llmff", llmff_require_inspect=False),
        timeout_ms=12_000,
        retry_attempts=2,
        retry_backoff_ms=250,
        input_paths={"episode_trace": input_path},
        output_paths={"audit_report": run_dir / "audit.raw.json"},
    )

    command = calls[0][0][0]
    assert command[-6:] == [
        "--input",
        "episode_trace",
        str(input_path),
        "--output",
        "audit_report",
        str(run_dir / "audit.raw.json"),
    ]
    assert result.output_paths == {"audit_report": run_dir / "audit.raw.json"}


def test_run_manifest_rejects_inputs_outside_sidecar_before_subprocess(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    outside_input = tmp_path / "outside-secret.txt"
    outside_input.write_text("sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8")

    with pytest.raises(InputPathError, match="outside .sidecar"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
            input_paths={"policy": outside_input},
        )

    assert calls == []


def test_run_manifest_scans_sidecar_inputs_before_subprocess(
    monkeypatch,
    tmp_path: Path,
):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    sidecar_input = tmp_path / ".sidecar" / "policy.yaml"
    sidecar_input.parent.mkdir()
    sidecar_input.write_text("api_key: sk-abcdefghijklmnopqrstuvwx\n", encoding="utf-8")

    with pytest.raises(SecretScanError, match="openai_api_key"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
            input_paths={"policy": sidecar_input},
        )

    assert calls == []


def test_run_manifest_marks_lifecycle_artifacts_private_under_permissive_umask(
    monkeypatch,
    tmp_path: Path,
):
    def fake_run(*args, **kwargs):
        command = args[0]
        Path(command[command.index("--trace") + 1]).write_text('{"event":"step"}\n', encoding="utf-8")
        Path(command[command.index("--events") + 1]).write_text(
            '{"event":"run_completed"}\n',
            encoding="utf-8",
        )
        Path(command[command.index("--checkpoint") + 1]).write_text(
            '{"manifest_hash":"fake"}\n',
            encoding="utf-8",
        )
        Path(command[command.index("--output") + 2]).write_text("{}\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    output_path = run_dir / "audit.raw.json"

    previous_umask = os.umask(0o022)
    try:
        result = run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
            output_paths={"audit_report": output_path},
        )
    finally:
        os.umask(previous_umask)

    lifecycle_dir = run_dir / "episode-audit"
    assert S_IMODE(lifecycle_dir.stat().st_mode) == 0o700
    assert S_IMODE(result.trace_path.stat().st_mode) == 0o600
    assert S_IMODE(result.events_path.stat().st_mode) == 0o600
    assert S_IMODE(result.checkpoint_path.stat().st_mode) == 0o600
    assert S_IMODE(output_path.stat().st_mode) == 0o600


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
        policy=Policy(llmff_require_inspect=False),
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
        policy=Policy(llmff_require_inspect=False),
        timeout_ms=12_000,
        retry_attempts=2,
        retry_backoff_ms=250,
    )

    assert result.exit_code == 7
    assert result.failure_kind == "timeout"
    assert result.failure_message == "Timed out after 12000 ms"


def test_python_boundary_timeout_returns_deterministic_failure(
    monkeypatch, tmp_path: Path
):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    result = run_manifest(
        manifest,
        run_dir=run_dir,
        policy=Policy(llmff_require_inspect=False),
        timeout_ms=12_000,
        retry_attempts=2,
        retry_backoff_ms=250,
    )

    assert result.exit_code == 124
    assert result.trace_path == run_dir / "episode-audit" / "llmff-trace.jsonl"
    assert result.events_path == run_dir / "episode-audit" / "llmff-events.jsonl"
    assert result.checkpoint_path == run_dir / "episode-audit" / "checkpoint.json"
    assert result.output_paths == {}
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
        policy=Policy(llmff_require_inspect=False),
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
            policy=Policy(llmff_require_inspect=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )


@pytest.mark.parametrize(
    "checkpoint_text",
    [
        json.dumps({"step": "resume"}) + "\n",
        "{not-json\n",
    ],
)
def test_run_manifest_rejects_unverifiable_checkpoint(
    tmp_path: Path,
    checkpoint_text: str,
):
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    checkpoint_path = run_dir / "episode-audit" / "checkpoint.json"
    checkpoint_path.parent.mkdir(parents=True)
    checkpoint_path.write_text(checkpoint_text, encoding="utf-8")

    with pytest.raises(CheckpointMismatchError, match="manifest hash"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=False),
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
            policy=Policy(llmff_require_inspect=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
            output_paths={"audit_report": tmp_path / "outside.json"},
        )


def test_run_manifest_rejects_checkpoint_outside_run_dir(tmp_path: Path):
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(CheckpointPathError, match="outside run directory"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
            checkpoint_path=tmp_path / "outside-checkpoint.json",
        )


def test_run_manifest_rejects_checkpoint_path_traversal(tmp_path: Path):
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(CheckpointPathError, match="outside run directory"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
            checkpoint_path=run_dir / ".." / "outside-checkpoint.json",
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
            policy=Policy(llmff_require_inspect=False),
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

    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    checkpoint_path = run_dir / "episode-audit" / "checkpoint.json"

    with pytest.raises(SecretScanError, match="ghp_token"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )

    assert not checkpoint_path.exists()


def test_python_boundary_timeout_scans_partial_artifacts(monkeypatch, tmp_path: Path):
    def fake_run(*args, **kwargs):
        checkpoint_path = Path(args[0][args[0].index("--checkpoint") + 1])
        checkpoint_path.write_text(
            json.dumps({"token": "ghp_abcdefghijklmnopqrstuvwx"}) + "\n",
            encoding="utf-8",
        )
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "episode-audit.yaml"
    manifest.write_text("name: episode-audit\n", encoding="utf-8")

    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    checkpoint_path = run_dir / "episode-audit" / "checkpoint.json"

    with pytest.raises(SecretScanError, match="ghp_token"):
        run_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_require_inspect=False),
            timeout_ms=12_000,
            retry_attempts=2,
            retry_backoff_ms=250,
        )

    assert not checkpoint_path.exists()
