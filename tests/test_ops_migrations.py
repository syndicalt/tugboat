from __future__ import annotations

import json
import re
from pathlib import Path
from stat import S_IMODE

import pytest
import yaml

from tugboat.artifacts import ArtifactValidationError
from tugboat.ops.migrations import (
    DEFAULT_MIGRATIONS,
    SidecarMigration,
    MigrationPlan,
    MigrationStep,
    execute_migration_plan,
    current_sidecar_version,
    dry_run_migration_plan,
    ordered_migrations_after,
    restore_pre_migration_snapshot,
    write_migration_report,
)
from tugboat.security.secrets import SecretScanError


def _execute_migration_plan(repo: Path):
    try:
        from tugboat.ops.migrations import execute_migration_plan
    except ImportError:
        pytest.fail("execute_migration_plan should execute pending migrations")
    return execute_migration_plan(repo)


def test_current_sidecar_version_reads_version_json(tmp_path: Path) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "version.json").write_text(
        json.dumps({"schema_version": 2}),
        encoding="utf-8",
    )

    assert current_sidecar_version(tmp_path) == 2


def test_current_sidecar_version_treats_existing_unversioned_sidecar_as_v1(
    tmp_path: Path,
) -> None:
    (tmp_path / ".sidecar").mkdir()

    assert current_sidecar_version(tmp_path) == 1


def test_current_sidecar_version_treats_policy_yaml_version_as_metadata_without_marker(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: 2\n", encoding="utf-8")

    assert current_sidecar_version(tmp_path) == 1


def test_dry_run_migration_plan_for_missing_sidecar_has_no_steps(tmp_path: Path) -> None:
    plan = dry_run_migration_plan(tmp_path)

    assert plan.current_version == 0
    assert plan.target_version == 0
    assert plan.steps == ()


def test_ordered_migrations_after_lists_only_pending_migrations() -> None:
    pending = ordered_migrations_after(1, DEFAULT_MIGRATIONS)

    assert [migration.migration_id for migration in pending] == [
        "sidecar-v1-to-v2",
        "sidecar-v2-to-v3",
    ]
    assert [migration.from_version for migration in pending] == [1, 2]
    assert [migration.to_version for migration in pending] == [2, 3]


def test_dry_run_migration_plan_reports_steps_without_mutating_sidecar(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    marker = sidecar / "version.json"
    marker.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")

    before = sorted(path.relative_to(sidecar) for path in sidecar.rglob("*"))
    plan = dry_run_migration_plan(tmp_path)
    after = sorted(path.relative_to(sidecar) for path in sidecar.rglob("*"))

    assert plan.current_version == 2
    assert plan.target_version == 3
    assert [step.migration_id for step in plan.steps] == ["sidecar-v2-to-v3"]
    assert plan.steps[0].actions == (
        "prepare ops observability summary artifact directory",
        "write schema marker after migration execution",
    )
    assert before == after
    assert json.loads(marker.read_text(encoding="utf-8")) == {"schema_version": 2}


def test_dry_run_migration_plan_blocks_newer_sidecar_schema(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    marker = sidecar / "version.json"
    marker.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    with pytest.raises(ValueError, match="sidecar schema version 999 is newer than supported"):
        dry_run_migration_plan(tmp_path)

    assert json.loads(marker.read_text(encoding="utf-8")) == {"schema_version": 999}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"version": 3}, ".sidecar/version.json missing schema_version"),
        (
            {"schema_version": "current"},
            ".sidecar/version.json schema_version must be a positive integer",
        ),
        (["schema_version", 3], ".sidecar/version.json must contain a JSON object"),
    ],
)
def test_current_sidecar_version_rejects_malformed_version_json_cleanly(
    tmp_path: Path,
    payload: object,
    message: str,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    marker = sidecar / "version.json"
    marker.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=re.escape(message)):
        current_sidecar_version(tmp_path)

    assert marker.read_text(encoding="utf-8") == json.dumps(payload)


def test_execute_migration_plan_preserves_older_policy_yaml_version_metadata(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    policy = sidecar / "policy.yaml"
    policy.write_text(
        "version: 1\n"
        "mode: proposal_only\n"
        "retention:\n"
        "  raw_traces_days: 14\n",
        encoding="utf-8",
    )

    result = _execute_migration_plan(tmp_path)

    assert result.current_version == 1
    assert result.target_version == DEFAULT_MIGRATIONS[-1].to_version
    assert [step.migration_id for step in result.steps] == [
        "sidecar-v1-to-v2",
        "sidecar-v2-to-v3",
    ]
    assert current_sidecar_version(tmp_path) == DEFAULT_MIGRATIONS[-1].to_version
    assert (sidecar / "ops" / "observability").is_dir()
    assert json.loads((sidecar / "version.json").read_text(encoding="utf-8")) == {
        "schema_version": DEFAULT_MIGRATIONS[-1].to_version,
    }
    assert yaml.safe_load(policy.read_text(encoding="utf-8")) == {
        "version": 1,
        "mode": "proposal_only",
        "retention": {"raw_traces_days": 14},
    }


def test_execute_migration_plan_preserves_policy_version_when_upgrading_unmarked_0x_sidecar(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    policy = sidecar / "policy.yaml"
    policy.write_text(
        "version: 7\n"
        "mode: proposal_only\n"
        "retention:\n"
        "  raw_traces_days: 14\n",
        encoding="utf-8",
    )

    result = _execute_migration_plan(tmp_path)

    assert result.current_version == 1
    assert result.target_version == DEFAULT_MIGRATIONS[-1].to_version
    assert [step.migration_id for step in result.steps] == [
        "sidecar-v1-to-v2",
        "sidecar-v2-to-v3",
    ]
    assert json.loads((sidecar / "version.json").read_text(encoding="utf-8")) == {
        "schema_version": DEFAULT_MIGRATIONS[-1].to_version,
    }
    assert yaml.safe_load(policy.read_text(encoding="utf-8")) == {
        "version": 7,
        "mode": "proposal_only",
        "retention": {"raw_traces_days": 14},
    }


def test_execute_migration_plan_persists_audit_report(tmp_path: Path) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: 1\n", encoding="utf-8")

    result = _execute_migration_plan(tmp_path)

    assert result.report_path == sidecar / "migrations" / "migration-report.json"
    snapshot_path = sidecar / "migrations" / "pre-migration-state.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot == {
        "schema_version": 1,
        "artifact_kind": "sidecar_migration_snapshot",
        "captured_version": 1,
        "captured_files": [
            {
                "path": ".sidecar/VERSION",
                "existed": False,
                "content": None,
            },
            {
                "path": ".sidecar/policy.yaml",
                "existed": True,
                "content": "version: 1\n",
            },
            {
                "path": ".sidecar/version.json",
                "existed": False,
                "content": None,
            },
        ],
    }
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report == {
        "schema_version": 1,
        "artifact_kind": "sidecar_migration_report",
        "current_version": 1,
        "target_version": DEFAULT_MIGRATIONS[-1].to_version,
        "applied_migrations": [
            {
                "migration_id": "sidecar-v1-to-v2",
                "from_version": 1,
                "to_version": 2,
                "description": "introduce explicit sidecar schema marker",
                "actions": [
                    "read legacy policy and artifact layout",
                    "write schema marker after migration execution",
                ],
            },
            {
                "migration_id": "sidecar-v2-to-v3",
                "from_version": 2,
                "to_version": 3,
                "description": "prepare operations artifact directories",
                "actions": [
                    "prepare ops observability summary artifact directory",
                    "write schema marker after migration execution",
                ],
            },
        ],
        "version_marker": ".sidecar/version.json",
        "pre_migration_snapshot": ".sidecar/migrations/pre-migration-state.json",
    }


def test_execute_migration_plan_writes_owner_only_pre_migration_snapshot(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: 1\n", encoding="utf-8")

    _execute_migration_plan(tmp_path)

    snapshot_path = sidecar / "migrations" / "pre-migration-state.json"
    assert S_IMODE(snapshot_path.stat().st_mode) == 0o600


def test_execute_migration_plan_secret_scans_pre_migration_snapshot_before_mutation(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    policy_path = sidecar / "policy.yaml"
    policy_text = "version: 1\nnotes: sk-" + "a" * 20 + "\n"
    policy_path.write_text(policy_text, encoding="utf-8")

    with pytest.raises(SecretScanError):
        execute_migration_plan(tmp_path)

    assert policy_path.read_text(encoding="utf-8") == policy_text
    assert not (sidecar / "version.json").exists()
    assert not (sidecar / "migrations" / "pre-migration-state.json").exists()
    assert not (sidecar / "migrations" / "migration-report.json").exists()


def test_write_migration_report_validates_payload_before_writing(
    tmp_path: Path,
) -> None:
    plan = MigrationPlan(
        current_version=1,
        target_version=3,
        steps=(
            MigrationStep(
                migration_id="sidecar-v1-to-v2",
                from_version=1,
                to_version=2,
                description="introduce explicit sidecar schema marker",
                actions=(),
            ),
        ),
    )

    with pytest.raises(ArtifactValidationError, match="applied_migrations\\[0\\].actions"):
        write_migration_report(tmp_path, plan)

    assert not (tmp_path / ".sidecar" / "migrations" / "migration-report.json").exists()


def test_execute_migration_plan_validates_report_before_mutating_sidecar(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    policy = sidecar / "policy.yaml"
    policy.write_text("version: 1\nmode: proposal_only\n", encoding="utf-8")
    invalid_migrations = (
        SidecarMigration(
            migration_id="sidecar-v1-to-v2",
            from_version=1,
            to_version=2,
            description="invalid migration with no report actions",
            actions=(),
        ),
    )

    with pytest.raises(ArtifactValidationError, match="applied_migrations\\[0\\].actions"):
        execute_migration_plan(tmp_path, invalid_migrations)

    assert not (sidecar / "version.json").exists()
    assert yaml.safe_load(policy.read_text(encoding="utf-8")) == {
        "version": 1,
        "mode": "proposal_only",
    }
    assert not (sidecar / "migrations" / "migration-report.json").exists()


def test_execute_migration_plan_validates_policy_before_mutating_sidecar(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    policy = sidecar / "policy.yaml"
    policy.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ValueError, match=".sidecar/policy.yaml must contain a mapping"):
        execute_migration_plan(tmp_path)

    assert not (sidecar / "version.json").exists()
    assert policy.read_text(encoding="utf-8") == "- not\n- a\n- mapping\n"
    assert not (sidecar / "ops").exists()
    assert not (sidecar / "migrations" / "migration-report.json").exists()


def test_execute_migration_plan_restores_sidecar_when_report_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tugboat.ops import migrations

    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    legacy_marker = sidecar / "VERSION"
    version_json = sidecar / "version.json"
    policy = sidecar / "policy.yaml"
    observability_dir = sidecar / "ops" / "observability"
    legacy_marker.write_text("2\n", encoding="utf-8")
    version_json.write_text('{"schema_version": 2}\n', encoding="utf-8")
    policy.write_text("version: 2\nmode: proposal_only\n", encoding="utf-8")
    observability_dir.mkdir(parents=True)
    (observability_dir / ".keep").write_text("preserve\n", encoding="utf-8")

    def fail_report_write(repo: Path, plan: MigrationPlan) -> Path:
        raise OSError("simulated migration report failure")

    monkeypatch.setattr(migrations, "write_migration_report", fail_report_write)

    with pytest.raises(OSError, match="simulated migration report failure"):
        execute_migration_plan(tmp_path)

    assert legacy_marker.read_text(encoding="utf-8") == "2\n"
    assert version_json.read_text(encoding="utf-8") == '{"schema_version": 2}\n'
    assert policy.read_text(encoding="utf-8") == "version: 2\nmode: proposal_only\n"
    assert json.loads(
        (sidecar / "migrations" / "pre-migration-state.json").read_text(encoding="utf-8")
    )["captured_version"] == 2
    assert (observability_dir / ".keep").read_text(encoding="utf-8") == "preserve\n"
    assert not (sidecar / "migrations" / "migration-report.json").exists()


def test_execute_migration_plan_removes_new_schema_marker_when_restoring_v1_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tugboat.ops import migrations

    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    policy = sidecar / "policy.yaml"
    policy.write_text("version: 1\nmode: proposal_only\n", encoding="utf-8")

    def fail_report_write(repo: Path, plan: MigrationPlan) -> Path:
        raise OSError("simulated migration report failure")

    monkeypatch.setattr(migrations, "write_migration_report", fail_report_write)

    with pytest.raises(OSError, match="simulated migration report failure"):
        execute_migration_plan(tmp_path)

    assert policy.read_text(encoding="utf-8") == "version: 1\nmode: proposal_only\n"
    assert not (sidecar / "VERSION").exists()
    assert not (sidecar / "version.json").exists()
    assert not (sidecar / "ops").exists()
    assert not (sidecar / "migrations" / "migration-report.json").exists()


def test_restore_pre_migration_snapshot_rejects_unsupported_paths(
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / ".sidecar" / "migrations" / "pre-migration-state.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_kind": "sidecar_migration_snapshot",
                "captured_version": 2,
                "captured_files": [
                    {
                        "path": ".sidecar/not-allowed.txt",
                        "existed": False,
                        "content": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="sidecar migration snapshot has unsupported path: .sidecar/not-allowed.txt",
    ):
        restore_pre_migration_snapshot(tmp_path, snapshot_path)


def test_restore_pre_migration_snapshot_requires_content_for_existing_files(
    tmp_path: Path,
) -> None:
    snapshot_path = tmp_path / ".sidecar" / "migrations" / "pre-migration-state.json"
    snapshot_path.parent.mkdir(parents=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifact_kind": "sidecar_migration_snapshot",
                "captured_version": 2,
                "captured_files": [
                    {
                        "path": ".sidecar/version.json",
                        "existed": True,
                        "content": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="sidecar migration snapshot missing content for .sidecar/version.json",
    ):
        restore_pre_migration_snapshot(tmp_path, snapshot_path)
