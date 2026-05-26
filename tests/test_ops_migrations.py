from __future__ import annotations

import json
from pathlib import Path

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
    write_migration_report,
)


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


def test_current_sidecar_version_reads_policy_yaml_version_without_marker(
    tmp_path: Path,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: 2\n", encoding="utf-8")

    assert current_sidecar_version(tmp_path) == 2


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


def test_execute_migration_plan_updates_older_policy_yaml_to_current_version(
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
        "version": DEFAULT_MIGRATIONS[-1].to_version,
        "mode": "proposal_only",
        "retention": {"raw_traces_days": 14},
    }


def test_execute_migration_plan_persists_audit_report(tmp_path: Path) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: 1\n", encoding="utf-8")

    result = _execute_migration_plan(tmp_path)

    assert result.report_path == sidecar / "migrations" / "migration-report.json"
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
    }


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
