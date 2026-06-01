from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from stat import S_IMODE

import pytest

from tugboat.audit.service import write_audit
from tugboat.llmff.contracts import InspectPolicyError
from tugboat.llmff.runner import FixtureLlmffRunner, inspect_manifest
from tugboat.models import Policy
from tugboat.security.secrets import SecretScanError


def test_inspect_manifest_writes_sidecar_artifact_with_manifest_hash(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\nsteps: []\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    runner = FixtureLlmffRunner(
        inspect_payload={
            "manifest": "audit",
            "network_required": False,
            "providers": [],
            "external_calls": [],
        }
    )

    result = inspect_manifest(
        manifest,
        run_dir=run_dir,
        policy=Policy(llmff_allow_network=False),
        runner=runner,
    )

    artifact_path = run_dir / "manifest" / "llmff-inspect.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert S_IMODE(artifact_path.parent.stat().st_mode) == 0o700
    assert S_IMODE(artifact_path.stat().st_mode) == 0o600
    assert result.manifest_hash == artifact["manifest_hash"]
    assert result.network_required is False
    assert artifact["external_calls"] == []
    assert artifact["inspect"]["network_required"] is False
    assert runner.inspect_calls == [manifest]


def test_inspect_manifest_enforces_private_modes_under_permissive_umask(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\nsteps: []\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    runner = FixtureLlmffRunner(
        inspect_payload={
            "manifest": "audit",
            "network_required": False,
            "providers": [],
            "external_calls": [],
        }
    )

    previous_umask = os.umask(0o022)
    try:
        result = inspect_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_allow_network=False),
            runner=runner,
        )
    finally:
        os.umask(previous_umask)

    assert S_IMODE(result.artifact_path.parent.stat().st_mode) == 0o700
    assert S_IMODE(result.artifact_path.stat().st_mode) == 0o600


def test_inspect_manifest_fails_closed_when_network_is_disallowed(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    runner = FixtureLlmffRunner(
        inspect_payload={
            "network_required": True,
            "external_calls": [{"kind": "http", "target": "api.example.test"}],
        }
    )

    with pytest.raises(InspectPolicyError, match="network"):
        inspect_manifest(
            manifest,
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
            policy=Policy(llmff_allow_network=False),
            runner=runner,
        )

    assert not (
        tmp_path / ".sidecar" / "runs" / "run-1" / "manifest" / "llmff-inspect.json"
    ).exists()


def test_inspect_manifest_rejects_missing_network_declaration(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    runner = FixtureLlmffRunner(inspect_payload={"providers": []})

    with pytest.raises(InspectPolicyError, match="network_required must be declared"):
        inspect_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(),
            runner=runner,
        )

    assert not (run_dir / "manifest" / "llmff-inspect.json").exists()


def test_inspect_manifest_rejects_missing_external_call_declaration(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    runner = FixtureLlmffRunner(inspect_payload={"network_required": False, "providers": []})

    with pytest.raises(InspectPolicyError, match="external_calls must be declared"):
        inspect_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(),
            runner=runner,
        )

    assert not (run_dir / "manifest" / "llmff-inspect.json").exists()


def test_inspect_manifest_rejects_malformed_network_declaration(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    for inspect_payload in (
        {"network_required": "false", "providers": [], "external_calls": []},
        {"requires_network": 0, "providers": [], "external_calls": []},
        {"network": {"required": "false"}, "providers": [], "external_calls": []},
        {"network": "false", "providers": [], "external_calls": []},
    ):
        with pytest.raises(InspectPolicyError, match="network_required must be a boolean"):
            inspect_manifest(
                manifest,
                run_dir=run_dir,
                policy=Policy(),
                runner=FixtureLlmffRunner(inspect_payload=inspect_payload),
            )

    assert not (run_dir / "manifest" / "llmff-inspect.json").exists()


def test_inspect_manifest_rejects_malformed_external_call_declarations(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    for inspect_payload in (
        {"network_required": False, "providers": [], "external_calls": "none"},
        {"network_required": False, "providers": [], "external_calls": [123]},
        {"network_required": False, "providers": [], "external_calls": [{}]},
        {
            "network_required": False,
            "providers": [],
            "external_calls": [{"kind": "", "target": "openai"}],
        },
        {
            "network_required": False,
            "providers": [],
            "external_calls": [{"kind": "model_provider", "target": ""}],
        },
    ):
        with pytest.raises(InspectPolicyError, match="external_calls"):
            inspect_manifest(
                manifest,
                run_dir=run_dir,
                policy=Policy(),
                runner=FixtureLlmffRunner(inspect_payload=inspect_payload),
            )

    assert not (run_dir / "manifest" / "llmff-inspect.json").exists()


def test_inspect_manifest_rejects_external_calls_when_network_is_false(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    runner = FixtureLlmffRunner(
        inspect_payload={
            "network_required": False,
            "providers": [],
            "external_calls": [{"kind": "http", "target": "api.openai.com"}],
        }
    )

    with pytest.raises(InspectPolicyError, match="external_calls conflict with network_required"):
        inspect_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(),
            runner=runner,
        )

    assert not (run_dir / "manifest" / "llmff-inspect.json").exists()


def test_inspect_manifest_rejects_network_true_without_external_calls(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"
    runner = FixtureLlmffRunner(
        inspect_payload={"network_required": True, "providers": [], "external_calls": []}
    )

    with pytest.raises(InspectPolicyError, match="external_calls must declare at least one call"):
        inspect_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(llmff_allow_network=True),
            runner=runner,
        )

    assert not (run_dir / "manifest" / "llmff-inspect.json").exists()


def test_inspect_manifest_rejects_unpinned_manifest_hash(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    runner = FixtureLlmffRunner(inspect_payload={"network_required": False, "external_calls": []})

    with pytest.raises(InspectPolicyError, match="manifest hash"):
        inspect_manifest(
            manifest,
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
            policy=Policy(allowed_manifest_hashes=("not-this-manifest",)),
            runner=runner,
        )

    assert not (
        tmp_path / ".sidecar" / "runs" / "run-1" / "manifest" / "llmff-inspect.json"
    ).exists()


def test_inspect_manifest_rejects_unallowlisted_declared_provider(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    runner = FixtureLlmffRunner(
        inspect_payload={
            "network_required": True,
            "providers": ["openai"],
            "external_calls": [{"kind": "model_provider", "target": "openai"}],
        }
    )

    with pytest.raises(InspectPolicyError, match="provider is not allowed"):
        inspect_manifest(
            manifest,
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
            policy=Policy(llmff_allow_network=True),
            runner=runner,
        )

    assert not (
        tmp_path / ".sidecar" / "runs" / "run-1" / "manifest" / "llmff-inspect.json"
    ).exists()


def test_inspect_manifest_rejects_provider_missing_from_external_calls(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    runner = FixtureLlmffRunner(
        inspect_payload={
            "network_required": True,
            "providers": ["openai"],
            "external_calls": [{"kind": "http", "target": "api.openai.com"}],
        }
    )

    with pytest.raises(InspectPolicyError, match="provider missing from external_calls"):
        inspect_manifest(
            manifest,
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
            policy=Policy(llmff_allow_network=True, llmff_allowed_providers=("openai",)),
            runner=runner,
        )

    assert not (
        tmp_path / ".sidecar" / "runs" / "run-1" / "manifest" / "llmff-inspect.json"
    ).exists()


def test_inspect_manifest_rejects_provider_backed_manifest_without_reviewed_hash(
    tmp_path: Path,
):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    runner = FixtureLlmffRunner(
        inspect_payload={
            "network_required": True,
            "providers": ["openai"],
            "external_calls": [{"kind": "model_provider", "target": "openai"}],
        }
    )
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(InspectPolicyError, match="reviewed manifest hash"):
        inspect_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(
                llmff_allow_network=True,
                llmff_allowed_providers=("openai",),
            ),
            runner=runner,
        )

    assert not (run_dir / "manifest" / "llmff-inspect.json").exists()


def test_inspect_manifest_allows_declared_provider_when_policy_allowlisted(
    tmp_path: Path,
):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    runner = FixtureLlmffRunner(
        inspect_payload={
            "network_required": True,
            "providers": ["openai"],
            "external_calls": [{"kind": "model_provider", "target": "openai"}],
        }
    )

    result = inspect_manifest(
        manifest,
        run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
        policy=Policy(
            llmff_allow_network=True,
            llmff_allowed_providers=("openai",),
            allowed_manifest_hashes=(
                hashlib.sha256(manifest.read_bytes()).hexdigest(),
            ),
        ),
        runner=runner,
    )

    artifact = json.loads(result.artifact_path.read_text(encoding="utf-8"))
    assert artifact["inspect"]["providers"] == ["openai"]
    assert artifact["external_calls"] == [{"kind": "model_provider", "target": "openai"}]


def test_inspect_manifest_rejects_malformed_provider_declarations(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")

    for inspect_payload in (
        {"network_required": False, "providers": "openai", "external_calls": []},
        {"network_required": False, "providers": [123], "external_calls": []},
        {"network_required": False, "providers": [""], "external_calls": []},
    ):
        with pytest.raises(InspectPolicyError, match="providers must be a list of non-empty strings"):
            inspect_manifest(
                manifest,
                run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
                policy=Policy(llmff_allowed_providers=("openai",)),
                runner=FixtureLlmffRunner(inspect_payload=inspect_payload),
            )

    assert not (
        tmp_path / ".sidecar" / "runs" / "run-1" / "manifest" / "llmff-inspect.json"
    ).exists()


def test_inspect_manifest_removes_secret_bearing_artifact(tmp_path: Path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("name: audit\n", encoding="utf-8")
    runner = FixtureLlmffRunner(
        inspect_payload={
            "network_required": False,
            "providers": [],
            "external_calls": [],
            "example_token": "ghp_abcdefghijklmnopqrstuvwx",
        }
    )
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(SecretScanError, match="ghp_token"):
        inspect_manifest(
            manifest,
            run_dir=run_dir,
            policy=Policy(),
            runner=runner,
        )

    assert not (run_dir / "manifest" / "llmff-inspect.json").exists()


def test_write_audit_writes_deterministic_pretty_json(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    path = write_audit(
        run_dir,
        {
            "audit_id": 1,
            "edit_warranted": True,
            "evidence_refs": [],
            "failure_class": "instruction_missing",
            "severity": "medium",
            "confidence": 0.75,
        },
    )

    assert path == run_dir / "audit.json"
    artifact = json.loads(path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1


def test_write_audit_rejects_secret_bearing_payload(tmp_path: Path):
    run_dir = tmp_path / ".sidecar" / "runs" / "run-1"

    with pytest.raises(SecretScanError, match="openai_api_key"):
        write_audit(
            run_dir,
            {
                "audit_id": 1,
                "edit_warranted": False,
                "evidence_refs": [],
                "failure_class": "llmff_run_failed",
                "severity": "high",
                "confidence": 1.0,
                "llmff_failure_message": "provider returned sk-abcdefghijklmnopqrstuvwx",
            },
        )

    assert not (run_dir / "audit.json").exists()


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
