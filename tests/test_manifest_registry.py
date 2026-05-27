from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tugboat.artifacts import JSON_ARTIFACT_JSON_SCHEMAS
from tugboat.manifests import (
    REQUIRED_MANIFEST_NAMES,
    ManifestRecord,
    manifests_are_allowed_by_policy,
    materialize_manifests,
    validate_manifest_contracts,
)
from tugboat.models import Policy


EXPECTED_OUTPUT_ARTIFACTS = {
    "instruction-index.yaml": {"instruction_index": "instruction-index.raw.json"},
    "episode-audit.yaml": {
        "audit_report": "audit.raw.json",
        "evidence_ids": "evidence-ids.raw.json",
    },
    "drift-detect.yaml": {
        "drift_clusters": "drift.raw.json",
        "optimizer_notes": "optimizer-notes.raw.json",
    },
    "patch-propose.yaml": {
        "candidate_patch": "candidate.raw.json",
        "proposal_rationale": "proposal-rationale.raw.json",
    },
    "patch-eval.yaml": {
        "eval_report": "eval-report.raw.json",
        "policy_decision": "policy-decision.raw.json",
    },
    "acceptance-summary.yaml": {
        "acceptance_summary": "acceptance-summary.raw.json",
    },
}


def _manifest_record(tmp_path: Path, name: str, text: str) -> ManifestRecord:
    path = tmp_path / ".sidecar" / "manifests" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ManifestRecord(name=name, path=path, sha256="x" * 64)


def test_materialize_manifests_writes_required_templates(tmp_path: Path):
    records = materialize_manifests(tmp_path)

    assert [record.name for record in records] == list(REQUIRED_MANIFEST_NAMES)
    assert all(isinstance(record, ManifestRecord) for record in records)

    for record in records:
        assert record.path == tmp_path / ".sidecar" / "manifests" / record.name
        assert len(record.sha256) == 64
        assert record.path.exists()

        manifest = yaml.safe_load(record.path.read_text(encoding="utf-8"))
        assert manifest["name"] == record.name.removesuffix(".yaml")
        assert manifest["purpose"]
        assert manifest["inputs"]
        assert manifest["outputs"]
        assert manifest["output_artifacts"] == EXPECTED_OUTPUT_ARTIFACTS[record.name]


def test_manifest_templates_bind_outputs_to_json_artifact_schemas(tmp_path: Path):
    records = materialize_manifests(tmp_path, overwrite=True)

    for record in records:
        manifest = yaml.safe_load(record.path.read_text(encoding="utf-8"))
        output_artifacts = manifest["output_artifacts"]

        assert output_artifacts == EXPECTED_OUTPUT_ARTIFACTS[record.name]
        assert set(output_artifacts) == set(manifest["outputs"])
        assert set(output_artifacts.values()).issubset(JSON_ARTIFACT_JSON_SCHEMAS)


def test_patch_propose_manifest_declares_optimizer_memory_input(tmp_path: Path):
    records = materialize_manifests(tmp_path)
    patch_propose = next(record for record in records if record.name == "patch-propose.yaml")

    manifest = yaml.safe_load(patch_propose.path.read_text(encoding="utf-8"))

    assert "optimizer_memory" in manifest["inputs"]


def test_materialize_manifests_preserves_existing_files_without_overwrite(tmp_path: Path):
    manifest_dir = tmp_path / ".sidecar" / "manifests"
    manifest_dir.mkdir(parents=True)
    existing = manifest_dir / "episode-audit.yaml"
    existing.write_text("name: local-episode-audit\n", encoding="utf-8")

    records = materialize_manifests(tmp_path)

    assert existing.read_text(encoding="utf-8") == "name: local-episode-audit\n"
    episode_record = next(record for record in records if record.name == "episode-audit.yaml")
    assert episode_record.sha256 == (
        "f75b04a4f3aceabf37c0fc2c047f00d0fb464487f0761f3c61d640d8901be133"
    )


def test_materialize_manifests_replaces_existing_files_with_overwrite(tmp_path: Path):
    manifest_dir = tmp_path / ".sidecar" / "manifests"
    manifest_dir.mkdir(parents=True)
    existing = manifest_dir / "episode-audit.yaml"
    existing.write_text("name: local-episode-audit\n", encoding="utf-8")

    records = materialize_manifests(tmp_path, overwrite=True)

    episode_record = next(record for record in records if record.name == "episode-audit.yaml")
    assert episode_record.path.read_text(encoding="utf-8") != "name: local-episode-audit\n"
    assert episode_record.sha256 != (
        "f75b04a4f3aceabf37c0fc2c047f00d0fb464487f0761f3c61d640d8901be133"
    )


def test_policy_compatibility_allows_empty_allowlist(tmp_path: Path):
    records = materialize_manifests(tmp_path)

    assert manifests_are_allowed_by_policy(records, Policy()) is True


def test_policy_compatibility_requires_every_manifest_hash(tmp_path: Path):
    records = materialize_manifests(tmp_path)
    one_missing = tuple(record.sha256 for record in records[:-1])
    policy = Policy(allowed_manifest_hashes=one_missing)

    assert manifests_are_allowed_by_policy(records, policy) is False
    assert manifests_are_allowed_by_policy(
        records,
        Policy(allowed_manifest_hashes=tuple(record.sha256 for record in records)),
    ) is True


def test_manifest_contract_validator_reports_preserved_local_manifest_missing_output_artifacts(
    tmp_path: Path,
):
    manifest_dir = tmp_path / ".sidecar" / "manifests"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "episode-audit.yaml").write_text(
        """
name: episode-audit
inputs:
  trace: trace.jsonl
outputs:
  audit_report: audit.raw.json
""".lstrip(),
        encoding="utf-8",
    )
    records = materialize_manifests(tmp_path)

    result = validate_manifest_contracts(records)

    assert result.passed is False
    assert (
        "episode-audit.yaml missing required manifest field output_artifacts"
        in result.findings
    )


def test_manifest_contract_validator_rejects_name_that_does_not_match_file_stem(
    tmp_path: Path,
):
    records = materialize_manifests(tmp_path)
    patch_eval = tmp_path / ".sidecar" / "manifests" / "patch-eval.yaml"
    manifest = yaml.safe_load(patch_eval.read_text(encoding="utf-8"))
    manifest["name"] = "other-manifest"
    patch_eval.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = validate_manifest_contracts(
        tuple(record for record in records if record.name != "patch-eval.yaml")
        + (ManifestRecord("patch-eval.yaml", patch_eval, "x" * 64),)
    )

    assert result.passed is False
    assert "patch-eval.yaml name must match file stem patch-eval" in result.findings


def test_manifest_contract_validator_requires_outputs_and_artifact_keys_to_match(
    tmp_path: Path,
):
    records = materialize_manifests(tmp_path)
    manifest_path = tmp_path / ".sidecar" / "manifests" / "acceptance-summary.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"].append("extra")
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = validate_manifest_contracts(
        tuple(
            record
            if record.name != "acceptance-summary.yaml"
            else ManifestRecord(record.name, manifest_path, "x" * 64)
            for record in records
        )
    )

    assert result.passed is False
    assert (
        "acceptance-summary.yaml outputs and output_artifacts keys must match: "
        "missing output_artifacts for extra"
    ) in result.findings


def test_manifest_contract_validator_requires_known_json_artifact_schema_names(
    tmp_path: Path,
):
    records = materialize_manifests(tmp_path)
    manifest_path = tmp_path / ".sidecar" / "manifests" / "drift-detect.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["output_artifacts"]["drift_clusters"] = "unknown.raw.json"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = validate_manifest_contracts(
        tuple(
            record
            if record.name != "drift-detect.yaml"
            else ManifestRecord(record.name, manifest_path, "x" * 64)
            for record in records
        )
    )

    assert result.passed is False
    assert (
        "drift-detect.yaml output_artifacts.drift_clusters references unknown JSON artifact schema unknown.raw.json"
        in result.findings
    )


def test_manifest_contract_validator_requires_all_required_manifests_present(
    tmp_path: Path,
):
    records = materialize_manifests(tmp_path)

    result = validate_manifest_contracts(records[:-1])

    assert result.passed is False
    assert "missing required manifest acceptance-summary.yaml" in result.findings


@pytest.mark.parametrize(
    ("manifest_text", "expected_finding"),
    (
        ("name: [", "episode-audit.yaml is not valid YAML:"),
        ("[]\n", "episode-audit.yaml must contain a YAML object"),
        (
            """
name: ""
inputs:
  - trace
outputs:
  - audit_report
output_artifacts:
  audit_report: audit.raw.json
""".lstrip(),
            "episode-audit.yaml name must be a non-empty string",
        ),
        (
            """
name: episode-audit
inputs:
  - trace
outputs: 7
output_artifacts:
  audit_report: audit.raw.json
""".lstrip(),
            "episode-audit.yaml outputs must be a mapping or list",
        ),
        (
            """
name: episode-audit
inputs:
  - trace
outputs: []
output_artifacts:
  audit_report: audit.raw.json
""".lstrip(),
            "episode-audit.yaml outputs must not be empty",
        ),
        (
            """
name: episode-audit
inputs:
  - trace
outputs:
  - 123
output_artifacts:
  audit_report: audit.raw.json
""".lstrip(),
            "episode-audit.yaml outputs entries must be non-empty strings",
        ),
        (
            """
name: episode-audit
inputs: 7
outputs:
  - audit_report
output_artifacts:
  audit_report: audit.raw.json
""".lstrip(),
            "episode-audit.yaml inputs must be a mapping or list",
        ),
        (
            """
name: episode-audit
inputs: []
outputs:
  - audit_report
output_artifacts:
  audit_report: audit.raw.json
""".lstrip(),
            "episode-audit.yaml inputs must not be empty",
        ),
        (
            """
name: episode-audit
inputs:
  - 123
outputs:
  - audit_report
output_artifacts:
  audit_report: audit.raw.json
""".lstrip(),
            "episode-audit.yaml inputs entries must be non-empty strings",
        ),
        (
            """
name: episode-audit
inputs:
  - trace
outputs:
  - audit_report
output_artifacts: []
""".lstrip(),
            "episode-audit.yaml output_artifacts must be a mapping",
        ),
        (
            """
name: episode-audit
inputs:
  - trace
outputs:
  - audit_report
output_artifacts: {}
""".lstrip(),
            "episode-audit.yaml output_artifacts must not be empty",
        ),
        (
            """
name: episode-audit
inputs:
  - trace
outputs:
  - audit_report
output_artifacts:
  123: audit.raw.json
""".lstrip(),
            "episode-audit.yaml output_artifacts keys must be non-empty strings",
        ),
        (
            """
name: episode-audit
inputs:
  - trace
outputs:
  - audit_report
output_artifacts:
  audit_report: 123
""".lstrip(),
            "episode-audit.yaml output_artifacts.audit_report must be a non-empty string",
        ),
        (
            """
name: episode-audit
inputs:
  - trace
outputs:
  - audit_report
output_artifacts:
  audit_report: audit.raw.json
  extra: audit.raw.json
""".lstrip(),
            "episode-audit.yaml outputs and output_artifacts keys must match: "
            "missing outputs for extra",
        ),
    ),
)
def test_manifest_contract_validator_reports_malformed_local_manifest_contracts(
    tmp_path: Path,
    manifest_text: str,
    expected_finding: str,
):
    record = _manifest_record(tmp_path, "episode-audit.yaml", manifest_text)

    result = validate_manifest_contracts((record,))

    assert result.passed is False
    assert any(finding.startswith(expected_finding) for finding in result.findings)
