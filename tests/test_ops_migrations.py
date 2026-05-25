from __future__ import annotations

import json
from pathlib import Path

from tugboat.ops.migrations import (
    DEFAULT_MIGRATIONS,
    current_sidecar_version,
    dry_run_migration_plan,
    ordered_migrations_after,
)


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
