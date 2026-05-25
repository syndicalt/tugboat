from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tugboat.audit.service import write_audit
from tugboat.llmff.contracts import InspectPolicyError
from tugboat.llmff.runner import FixtureLlmffRunner, inspect_manifest
from tugboat.models import Policy


def test_inspect_manifest_writes_sidecar_artifact_with_manifest_hash(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\nsteps: []\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    runner = FixtureLlmffRunner(
        inspect_payload={
            "manifest": "audit",
            "network_required": False,
            "providers": [],
        }
    )

    result = inspect_manifest(
        manifest,
        run_dir=run_dir,
        policy=Policy(llmff_allow_network=False),
        runner=runner,
    )

    artifact_path = run_dir / "llmff-inspect.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert result.manifest_hash == artifact["manifest_hash"]
    assert result.network_required is False
    assert artifact["inspect"]["network_required"] is False
    assert runner.inspect_calls == [manifest]


def test_inspect_manifest_fails_closed_when_network_is_disallowed(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    runner = FixtureLlmffRunner(inspect_payload={"network_required": True})

    with pytest.raises(InspectPolicyError, match="network"):
        inspect_manifest(
            manifest,
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
            policy=Policy(llmff_allow_network=False),
            runner=runner,
        )

    assert not (tmp_path / ".sidecar" / "runs" / "run-1" / "llmff-inspect.json").exists()


def test_inspect_manifest_rejects_unpinned_manifest_hash(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    runner = FixtureLlmffRunner(inspect_payload={"network_required": False})

    with pytest.raises(InspectPolicyError, match="manifest hash"):
        inspect_manifest(
            manifest,
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
            policy=Policy(allowed_manifest_hashes=("not-this-manifest",)),
            runner=runner,
        )

    assert not (tmp_path / ".sidecar" / "runs" / "run-1" / "llmff-inspect.json").exists()


def test_write_audit_writes_deterministic_pretty_json(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    path = write_audit(run_dir, {"z": 1, "a": {"b": True}})

    assert path == run_dir / "audit.json"
    assert path.read_text(encoding="utf-8") == (
        '{\n'
        '  "a": {\n'
        '    "b": true\n'
        '  },\n'
        '  "z": 1\n'
        '}\n'
    )


def test_subprocess_inspect_uses_timeout(monkeypatch, tmp_path: Path):
    from tugboat.llmff.runner import SubprocessLlmffRunner

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(args[0], 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")

    SubprocessLlmffRunner("llmff", timeout_seconds=7).inspect(manifest)

    assert calls[0]["timeout"] == 7
