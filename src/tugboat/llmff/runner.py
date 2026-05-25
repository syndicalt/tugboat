from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from tugboat.llmff.contracts import InspectPolicyError, InspectResult, LlmffRunner
from tugboat.models import Policy


class FixtureLlmffRunner:
    def __init__(self, inspect_payload: dict[str, Any]):
        self.inspect_payload = inspect_payload
        self.inspect_calls: list[Path] = []

    def inspect(self, manifest_path: Path) -> dict[str, Any]:
        self.inspect_calls.append(manifest_path)
        return dict(self.inspect_payload)


class SubprocessLlmffRunner:
    def __init__(self, binary: str = "llmff", timeout_seconds: int = 60):
        self.binary = binary
        self.timeout_seconds = timeout_seconds

    def inspect(self, manifest_path: Path) -> dict[str, Any]:
        completed = subprocess.run(
            [self.binary, "inspect", "--format", "json", str(manifest_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError("llmff inspect output must be a JSON object")
        return payload


def _manifest_hash(manifest_path: Path) -> str:
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def _network_required(inspect_payload: dict[str, Any]) -> bool:
    return bool(
        inspect_payload.get("network_required")
        or inspect_payload.get("requires_network")
        or inspect_payload.get("network", {}).get("required", False)
    )


def inspect_manifest(
    manifest_path: Path,
    *,
    run_dir: Path,
    policy: Policy,
    runner: LlmffRunner | None = None,
) -> InspectResult:
    actual_runner = runner or SubprocessLlmffRunner(policy.llmff_binary)
    inspect_payload = actual_runner.inspect(manifest_path)
    network_required = _network_required(inspect_payload)
    if network_required and not policy.llmff_allow_network:
        raise InspectPolicyError("llmff inspect requires network but policy disallows network")

    manifest_digest = _manifest_hash(manifest_path)
    artifact_path = run_dir / "llmff-inspect.json"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "manifest_path": str(manifest_path),
        "manifest_hash": manifest_digest,
        "network_required": network_required,
        "inspect": inspect_payload,
    }
    artifact_path.write_text(
        json.dumps(artifact, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return InspectResult(
        manifest_path=manifest_path,
        manifest_hash=manifest_digest,
        artifact_path=artifact_path,
        inspect=inspect_payload,
        network_required=network_required,
    )
