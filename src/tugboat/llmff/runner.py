from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
from tugboat.llmff.contracts import InspectPolicyError, InspectResult, LlmffRunner, RunResult
from tugboat.models import Policy
from tugboat.paths import ensure_private_dir, mark_private_file
from tugboat.security.secrets import SecretFinding, SecretScanError, scan_path


class CheckpointMismatchError(RuntimeError):
    pass


class OutputPathError(ValueError):
    pass


class CheckpointPathError(ValueError):
    pass


class MissingOutputError(RuntimeError):
    pass


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


class LlmffRunSupervisor:
    def __init__(self, binary: str = "llmff"):
        self.binary = binary

    def run_manifest(
        self,
        manifest_path: Path,
        *,
        run_dir: Path,
        timeout_ms: int,
        retry_attempts: int,
        retry_backoff_ms: int,
        checkpoint_path: Path | None = None,
        input_paths: dict[str, Path] | None = None,
        output_paths: dict[str, Path] | None = None,
    ) -> RunResult:
        lifecycle_dir = _manifest_lifecycle_dir(run_dir, manifest_path)
        trace_path = lifecycle_dir / "llmff-trace.jsonl"
        events_path = lifecycle_dir / "llmff-events.jsonl"
        actual_checkpoint_path = checkpoint_path or lifecycle_dir / "checkpoint.json"
        outputs = dict(output_paths or {})
        ensure_private_dir(run_dir)
        ensure_private_dir(lifecycle_dir)
        _validate_checkpoint_path(run_dir, actual_checkpoint_path)
        _reject_checkpoint_mismatch(actual_checkpoint_path, manifest_path)
        _validate_output_paths(run_dir, outputs)
        for path in outputs.values():
            ensure_private_dir(path.parent)

        command = [
            self.binary,
            "run",
            str(manifest_path),
            "--trace",
            str(trace_path),
            "--events",
            str(events_path),
            "--checkpoint",
            str(actual_checkpoint_path),
            "--timeout-ms",
            str(timeout_ms),
            "--retry-attempts",
            str(retry_attempts),
            "--retry-backoff-ms",
            str(retry_backoff_ms),
        ]
        for name, path in (input_paths or {}).items():
            command.extend(["--input", name, str(path)])
        for name, path in outputs.items():
            command.extend(["--output", name, str(path)])

        boundary_timeout = False
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_ms / 1000,
            )
        except subprocess.TimeoutExpired:
            boundary_timeout = True
            completed = subprocess.CompletedProcess(
                command,
                124,
                stdout="",
                stderr=f"Timed out after {timeout_ms} ms",
            )
        if completed.returncode == 0:
            _validate_declared_outputs_exist(outputs)
        for path in (trace_path, events_path, actual_checkpoint_path, *outputs.values()):
            if path.exists():
                mark_private_file(path)
                _scan_path_or_remove_secret_bearing_files(path)
        failure_kind, failure_message = (None, None)
        if boundary_timeout:
            failure_kind, failure_message = ("timeout", f"Timed out after {timeout_ms} ms")
        elif completed.returncode != 0:
            failure_kind, failure_message = _last_run_failure(events_path)

        return RunResult(
            manifest_path=manifest_path,
            exit_code=completed.returncode,
            trace_path=trace_path,
            events_path=events_path,
            checkpoint_path=actual_checkpoint_path,
            output_paths=outputs,
            failure_kind=failure_kind,
            failure_message=failure_message,
        )


def _safe_text(value: object) -> str | None:
    if value is None:
        return None
    return " ".join(str(value).split())[:1_000]


def _last_run_failure(events_path: Path) -> tuple[str | None, str | None]:
    if not events_path.exists():
        return (None, None)

    failure_kind = None
    failure_message = None
    with events_path.open(encoding="utf-8") as events:
        for line in events:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("event") != "run_failed":
                continue

            details = event.get("run_failed")
            if not isinstance(details, dict):
                details = event
            failure_kind = _safe_text(details.get("failure_kind"))
            failure_message = _safe_text(details.get("failure_message"))
    return (failure_kind, failure_message)


def _reject_checkpoint_mismatch(checkpoint_path: Path, manifest_path: Path) -> None:
    if not checkpoint_path.exists():
        return
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise CheckpointMismatchError("checkpoint manifest hash cannot be verified") from None
    if not isinstance(checkpoint, dict) or "manifest_hash" not in checkpoint:
        raise CheckpointMismatchError("checkpoint manifest hash cannot be verified")
    if str(checkpoint["manifest_hash"]) != _manifest_hash(manifest_path):
        raise CheckpointMismatchError("checkpoint manifest hash does not match current manifest")


def _validate_output_paths(run_dir: Path, output_paths: dict[str, Path]) -> None:
    run_root = run_dir.resolve()
    for path in output_paths.values():
        try:
            path.resolve().relative_to(run_root)
        except ValueError as exc:
            raise OutputPathError("llmff output path is outside run directory") from exc


def _validate_checkpoint_path(run_dir: Path, checkpoint_path: Path) -> None:
    try:
        checkpoint_path.resolve().relative_to(run_dir.resolve())
    except ValueError as exc:
        raise CheckpointPathError("llmff checkpoint path is outside run directory") from exc


def _validate_declared_outputs_exist(output_paths: dict[str, Path]) -> None:
    missing = sorted(name for name, path in output_paths.items() if not path.exists())
    if missing:
        raise MissingOutputError(f"llmff run succeeded without declared output: {missing[0]}")


def run_manifest(
    manifest_path: Path,
    *,
    run_dir: Path,
    policy: Policy,
    timeout_ms: int,
    retry_attempts: int,
    retry_backoff_ms: int,
    checkpoint_path: Path | None = None,
    input_paths: dict[str, Path] | None = None,
    output_paths: dict[str, Path] | None = None,
) -> RunResult:
    supervisor = LlmffRunSupervisor(policy.llmff_binary)
    return supervisor.run_manifest(
        manifest_path,
        run_dir=run_dir,
        timeout_ms=timeout_ms,
        retry_attempts=retry_attempts,
        retry_backoff_ms=retry_backoff_ms,
        checkpoint_path=checkpoint_path,
        input_paths=input_paths,
        output_paths=output_paths,
    )


def _manifest_hash(manifest_path: Path) -> str:
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def _manifest_lifecycle_dir(run_dir: Path, manifest_path: Path) -> Path:
    return run_dir / manifest_path.stem


def _network_required(inspect_payload: dict[str, Any]) -> bool:
    declarations: list[bool] = []
    for key in ("network_required", "requires_network"):
        if key not in inspect_payload:
            continue
        value = inspect_payload[key]
        if not isinstance(value, bool):
            raise InspectPolicyError("network_required must be a boolean")
        declarations.append(value)

    if "network" in inspect_payload:
        network = inspect_payload["network"]
        if not isinstance(network, dict):
            raise InspectPolicyError("network_required must be a boolean")
        if "required" not in network:
            raise InspectPolicyError("network_required must be declared")
        value = network["required"]
        if not isinstance(value, bool):
            raise InspectPolicyError("network_required must be a boolean")
        declarations.append(value)

    if not declarations:
        raise InspectPolicyError("network_required must be declared")
    if any(value != declarations[0] for value in declarations):
        raise InspectPolicyError("network_required declarations conflict")
    return declarations[0]


def _declared_providers(inspect_payload: dict[str, Any]) -> tuple[str, ...]:
    raw_providers = inspect_payload.get("providers", [])
    if not isinstance(raw_providers, list):
        raise InspectPolicyError("providers must be a list of non-empty strings")
    if not all(isinstance(provider, str) and provider.strip() for provider in raw_providers):
        raise InspectPolicyError("providers must be a list of non-empty strings")
    providers = tuple(provider.strip() for provider in raw_providers)

    raw_provider = inspect_payload.get("provider")
    if raw_provider is not None:
        if not isinstance(raw_provider, str) or not raw_provider.strip():
            raise InspectPolicyError("provider must be a non-empty string")
        providers = (*providers, raw_provider.strip())
    return tuple(dict.fromkeys(provider.strip() for provider in providers if provider.strip()))


def _external_calls(
    inspect_payload: dict[str, Any],
    *,
    network_required: bool,
    declared_providers: tuple[str, ...],
) -> tuple[dict[str, str], ...]:
    if "external_calls" not in inspect_payload:
        raise InspectPolicyError("external_calls must be declared")
    raw_calls = inspect_payload["external_calls"]
    if not isinstance(raw_calls, list):
        raise InspectPolicyError("external_calls must be a list")
    calls: list[dict[str, str]] = []
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            raise InspectPolicyError("external_calls entries must be objects")
        kind = raw_call.get("kind")
        target = raw_call.get("target")
        if not isinstance(kind, str) or not kind.strip():
            raise InspectPolicyError("external_calls entries require non-empty kind")
        if not isinstance(target, str) or not target.strip():
            raise InspectPolicyError("external_calls entries require non-empty target")
        calls.append({"kind": kind.strip(), "target": target.strip()})

    if network_required and not calls:
        raise InspectPolicyError("external_calls must declare at least one call")
    if not network_required and calls:
        raise InspectPolicyError("external_calls conflict with network_required")

    provider_targets = {
        call["target"]
        for call in calls
        if call["kind"] in {"model_provider", "provider"}
    }
    for provider in declared_providers:
        if provider not in provider_targets:
            raise InspectPolicyError(f"provider missing from external_calls: {provider}")
    return tuple(calls)


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
    declared_providers = _declared_providers(inspect_payload)
    external_calls = _external_calls(
        inspect_payload,
        network_required=network_required,
        declared_providers=declared_providers,
    )
    allowed_providers = set(policy.llmff_allowed_providers)
    if declared_providers and not allowed_providers.issuperset(declared_providers):
        provider = next(provider for provider in declared_providers if provider not in allowed_providers)
        raise InspectPolicyError(f"provider is not allowed by policy: {provider}")

    manifest_digest = _manifest_hash(manifest_path)
    if policy.allowed_manifest_hashes and manifest_digest not in policy.allowed_manifest_hashes:
        raise InspectPolicyError("manifest hash is not allowed by policy")
    artifact_path = _manifest_lifecycle_dir(run_dir, manifest_path) / "llmff-inspect.json"
    ensure_private_dir(artifact_path.parent)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "manifest_path": str(manifest_path),
        "manifest_hash": manifest_digest,
        "network_required": network_required,
        "external_calls": list(external_calls),
        "inspect": inspect_payload,
    }
    validate_json_artifact("llmff-inspect.json", artifact)
    artifact_path.write_text(
        json.dumps(artifact, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    mark_private_file(artifact_path)
    _scan_path_or_remove_secret_bearing_files(artifact_path)
    return InspectResult(
        manifest_path=manifest_path,
        manifest_hash=manifest_digest,
        artifact_path=artifact_path,
        inspect=inspect_payload,
        network_required=network_required,
        external_calls=external_calls,
    )


def _scan_path_or_remove_secret_bearing_files(path: Path) -> None:
    try:
        scan_path(path)
    except SecretScanError as exc:
        _remove_secret_bearing_files(exc.findings)
        raise


def _remove_secret_bearing_files(findings: tuple[SecretFinding, ...]) -> None:
    for finding in findings:
        finding_path = Path(finding.path)
        if finding_path.exists() and finding_path.is_file():
            finding_path.unlink()
