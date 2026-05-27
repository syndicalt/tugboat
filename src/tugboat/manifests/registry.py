from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from tugboat.artifacts import JSON_ARTIFACT_JSON_SCHEMAS
from tugboat.models import Policy
from tugboat.paths import sidecar_dir

REQUIRED_MANIFEST_NAMES = (
    "instruction-index.yaml",
    "episode-audit.yaml",
    "drift-detect.yaml",
    "patch-propose.yaml",
    "patch-eval.yaml",
    "acceptance-summary.yaml",
)


@dataclass(frozen=True)
class ManifestRecord:
    name: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class ManifestContractResult:
    passed: bool
    findings: tuple[str, ...]


def _template_bytes(name: str) -> bytes:
    return (
        files("tugboat.manifests")
        .joinpath("templates")
        .joinpath(name)
        .read_bytes()
    )


def _file_record(path: Path) -> ManifestRecord:
    return ManifestRecord(name=path.name, path=path, sha256=sha256(path.read_bytes()).hexdigest())


def materialize_manifests(repo: Path, *, overwrite: bool = False) -> tuple[ManifestRecord, ...]:
    manifest_dir = sidecar_dir(repo) / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    records: list[ManifestRecord] = []
    for name in REQUIRED_MANIFEST_NAMES:
        path = manifest_dir / name
        if overwrite or not path.exists():
            path.write_bytes(_template_bytes(name))
        records.append(_file_record(path))
    return tuple(records)


def manifests_are_allowed_by_policy(records: tuple[ManifestRecord, ...], policy: Policy) -> bool:
    allowlist = set(getattr(policy, "allowed_manifest_hashes", ()))
    if not allowlist:
        return True
    return all(record.sha256 in allowlist for record in records)


def validate_manifest_contracts(records: tuple[ManifestRecord, ...]) -> ManifestContractResult:
    findings: list[str] = []
    by_name = {record.name: record for record in records}
    for required_name in REQUIRED_MANIFEST_NAMES:
        if required_name not in by_name:
            findings.append(f"missing required manifest {required_name}")
    for record in sorted(records, key=lambda item: item.name):
        findings.extend(_manifest_contract_findings(record))
    return ManifestContractResult(passed=not findings, findings=tuple(findings))


def _manifest_contract_findings(record: ManifestRecord) -> list[str]:
    findings: list[str] = []
    try:
        payload = yaml.safe_load(record.path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        return [f"{record.name} is not valid YAML: {error}"]
    if not isinstance(payload, dict):
        return [f"{record.name} must contain a YAML object"]

    for field_name in ("name", "inputs", "outputs", "output_artifacts"):
        if field_name not in payload:
            findings.append(f"{record.name} missing required manifest field {field_name}")
    if findings:
        return findings

    manifest_name = payload["name"]
    if not isinstance(manifest_name, str) or not manifest_name.strip():
        findings.append(f"{record.name} name must be a non-empty string")
    elif manifest_name != record.name.removesuffix(".yaml"):
        findings.append(f"{record.name} name must match file stem {record.name.removesuffix('.yaml')}")

    outputs = _names_or_finding(record.name, "outputs", payload["outputs"], findings)
    output_artifacts = _mapping_or_finding(
        record.name,
        "output_artifacts",
        payload["output_artifacts"],
        findings,
    )
    _names_or_finding(record.name, "inputs", payload["inputs"], findings)
    if outputs is None or output_artifacts is None:
        return findings

    missing_artifacts = sorted(set(outputs) - set(output_artifacts))
    missing_outputs = sorted(set(output_artifacts) - set(outputs))
    if missing_artifacts or missing_outputs:
        details = []
        if missing_artifacts:
            details.append("missing output_artifacts for " + ", ".join(missing_artifacts))
        if missing_outputs:
            details.append("missing outputs for " + ", ".join(missing_outputs))
        findings.append(f"{record.name} outputs and output_artifacts keys must match: {'; '.join(details)}")

    for output_name, artifact_name in sorted(output_artifacts.items()):
        if not isinstance(artifact_name, str) or not artifact_name.strip():
            findings.append(f"{record.name} output_artifacts.{output_name} must be a non-empty string")
            continue
        if artifact_name not in JSON_ARTIFACT_JSON_SCHEMAS:
            findings.append(
                f"{record.name} output_artifacts.{output_name} references unknown JSON artifact schema {artifact_name}"
            )
    return findings


def _names_or_finding(
    manifest_name: str,
    field_name: str,
    value: Any,
    findings: list[str],
) -> tuple[str, ...] | None:
    if isinstance(value, dict):
        names = tuple(value)
    elif isinstance(value, list):
        names = tuple(value)
    else:
        findings.append(f"{manifest_name} {field_name} must be a mapping or list")
        return None
    if not names:
        findings.append(f"{manifest_name} {field_name} must not be empty")
        return None
    if not all(isinstance(name, str) and name.strip() for name in names):
        findings.append(f"{manifest_name} {field_name} entries must be non-empty strings")
        return None
    return tuple(str(name) for name in names)


def _mapping_or_finding(
    manifest_name: str,
    field_name: str,
    value: Any,
    findings: list[str],
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        findings.append(f"{manifest_name} {field_name} must be a mapping")
        return None
    if not value:
        findings.append(f"{manifest_name} {field_name} must not be empty")
        return None
    if not all(isinstance(key, str) and key.strip() for key in value):
        findings.append(f"{manifest_name} {field_name} keys must be non-empty strings")
        return None
    return value
