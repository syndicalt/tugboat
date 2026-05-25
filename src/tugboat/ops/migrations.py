from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SidecarMigration:
    migration_id: str
    from_version: int
    to_version: int
    description: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class MigrationStep:
    migration_id: str
    from_version: int
    to_version: int
    description: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class MigrationPlan:
    current_version: int
    target_version: int
    steps: tuple[MigrationStep, ...]


DEFAULT_MIGRATIONS: tuple[SidecarMigration, ...] = (
    SidecarMigration(
        migration_id="sidecar-v1-to-v2",
        from_version=1,
        to_version=2,
        description="introduce explicit sidecar schema marker",
        actions=(
            "read legacy policy and artifact layout",
            "write schema marker after migration execution",
        ),
    ),
    SidecarMigration(
        migration_id="sidecar-v2-to-v3",
        from_version=2,
        to_version=3,
        description="prepare operations artifact directories",
        actions=(
            "prepare ops observability summary artifact directory",
            "write schema marker after migration execution",
        ),
    ),
)


def current_sidecar_version(repo: Path) -> int:
    sidecar = repo / ".sidecar"
    if not sidecar.exists():
        return 0

    version_json = sidecar / "version.json"
    if version_json.exists():
        payload = json.loads(version_json.read_text(encoding="utf-8"))
        return int(payload["schema_version"])

    version_text = sidecar / "VERSION"
    if version_text.exists():
        return int(version_text.read_text(encoding="utf-8").strip())

    return 1


def ordered_migrations_after(
    current_version: int,
    migrations: tuple[SidecarMigration, ...] = DEFAULT_MIGRATIONS,
) -> tuple[SidecarMigration, ...]:
    pending = tuple(
        migration for migration in sorted(migrations, key=lambda item: item.from_version)
        if migration.from_version >= current_version
    )
    _validate_contiguous(current_version, pending)
    return pending


def dry_run_migration_plan(
    repo: Path,
    migrations: tuple[SidecarMigration, ...] = DEFAULT_MIGRATIONS,
) -> MigrationPlan:
    current_version = current_sidecar_version(repo)
    if current_version == 0:
        return MigrationPlan(current_version=0, target_version=0, steps=())

    pending = ordered_migrations_after(current_version, migrations)
    target_version = pending[-1].to_version if pending else current_version
    return MigrationPlan(
        current_version=current_version,
        target_version=target_version,
        steps=tuple(
            MigrationStep(
                migration_id=migration.migration_id,
                from_version=migration.from_version,
                to_version=migration.to_version,
                description=migration.description,
                actions=migration.actions,
            )
            for migration in pending
        ),
    )


def _validate_contiguous(
    current_version: int,
    migrations: tuple[SidecarMigration, ...],
) -> None:
    expected = current_version
    for migration in migrations:
        if migration.from_version != expected:
            raise ValueError(
                "migration chain is not contiguous: "
                f"expected v{expected}, got v{migration.from_version}"
            )
        expected = migration.to_version
