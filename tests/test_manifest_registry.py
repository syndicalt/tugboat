from __future__ import annotations

from pathlib import Path

import yaml

from tugboat.artifacts import JSON_ARTIFACT_JSON_SCHEMAS
from tugboat.manifests import (
    REQUIRED_MANIFEST_NAMES,
    ManifestRecord,
    manifests_are_allowed_by_policy,
    materialize_manifests,
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
