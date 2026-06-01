from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact, write_json_artifact
from tugboat.config import as_positive_version


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
    report_path: Path | None = None


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


def supported_sidecar_version(
    migrations: tuple[SidecarMigration, ...] = DEFAULT_MIGRATIONS,
) -> int:
    if not migrations:
        return 1
    return max(migration.to_version for migration in migrations)


def assert_supported_sidecar_marker(
    repo: Path,
    migrations: tuple[SidecarMigration, ...] = DEFAULT_MIGRATIONS,
) -> None:
    sidecar = repo / ".sidecar"
    if not sidecar.exists():
        return
    if not (sidecar / "version.json").exists() and not (sidecar / "VERSION").exists():
        return
    current_version = current_sidecar_version(repo)
    supported_version = supported_sidecar_version(migrations)
    if current_version > supported_version:
        raise ValueError(
            f"sidecar schema version {current_version} is newer than supported "
            f"version {supported_version}; upgrade Tugboat before using this sidecar"
        )


def current_sidecar_version(repo: Path) -> int:
    sidecar = repo / ".sidecar"
    if not sidecar.exists():
        return 0

    version_json = sidecar / "version.json"
    if version_json.exists():
        payload = json.loads(version_json.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(".sidecar/version.json must contain a JSON object")
        if "schema_version" not in payload:
            raise ValueError(".sidecar/version.json missing schema_version")
        return as_positive_version(
            payload["schema_version"],
            ".sidecar/version.json schema_version",
        )

    version_text = sidecar / "VERSION"
    if version_text.exists():
        return as_positive_version(
            version_text.read_text(encoding="utf-8").strip(),
            ".sidecar/VERSION",
        )

    policy_yaml = sidecar / "policy.yaml"
    if policy_yaml.exists():
        try:
            payload = yaml.safe_load(policy_yaml.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            raise ValueError(f".sidecar/policy.yaml invalid YAML: {error}") from error
        if not isinstance(payload, dict):
            raise ValueError(".sidecar/policy.yaml must contain a mapping")
        if "version" in payload:
            return as_positive_version(payload["version"], ".sidecar/policy.yaml version")

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
    supported_version = supported_sidecar_version(migrations)
    if current_version > supported_version:
        raise ValueError(
            f"sidecar schema version {current_version} is newer than supported "
            f"version {supported_version}; upgrade Tugboat before using this sidecar"
        )

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


def execute_migration_plan(
    repo: Path,
    migrations: tuple[SidecarMigration, ...] = DEFAULT_MIGRATIONS,
) -> MigrationPlan:
    plan = dry_run_migration_plan(repo, migrations)
    if plan.current_version == 0:
        return plan
    validate_migration_report(plan)
    write_pre_migration_snapshot(repo, plan)

    sidecar = repo / ".sidecar"
    policy_path = sidecar / "policy.yaml"
    policy_payload = None
    if policy_path.exists():
        policy_payload = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        if not isinstance(policy_payload, dict):
            raise ValueError(".sidecar/policy.yaml must contain a mapping")
        policy_payload["version"] = plan.target_version

    version_json = sidecar / "version.json"
    version_json.write_text(
        json.dumps({"schema_version": plan.target_version}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if any(step.migration_id == "sidecar-v2-to-v3" for step in plan.steps):
        (sidecar / "ops" / "observability").mkdir(parents=True, exist_ok=True)

    if policy_payload is not None:
        policy_path.write_text(yaml.safe_dump(policy_payload, sort_keys=True), encoding="utf-8")

    report_path = write_migration_report(repo, plan)
    return MigrationPlan(
        current_version=plan.current_version,
        target_version=plan.target_version,
        steps=plan.steps,
        report_path=report_path,
    )


def write_migration_report(repo: Path, plan: MigrationPlan) -> Path:
    payload = migration_report_payload(plan)
    validate_json_artifact("sidecar-migration-report.json", payload)
    migrations_dir = repo / ".sidecar" / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    report_path = migrations_dir / "migration-report.json"
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report_path


def write_pre_migration_snapshot(repo: Path, plan: MigrationPlan) -> Path:
    payload = migration_snapshot_payload(repo, plan.current_version)
    validate_json_artifact("sidecar-migration-snapshot.json", payload)
    migrations_dir = repo / ".sidecar" / "migrations"
    snapshot_path = migrations_dir / "pre-migration-state.json"
    return write_json_artifact(snapshot_path, payload)


def validate_migration_report(plan: MigrationPlan) -> None:
    validate_json_artifact("sidecar-migration-report.json", migration_report_payload(plan))


def migration_report_payload(plan: MigrationPlan) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "sidecar_migration_report",
        "current_version": plan.current_version,
        "target_version": plan.target_version,
        "applied_migrations": [
            {
                "migration_id": step.migration_id,
                "from_version": step.from_version,
                "to_version": step.to_version,
                "description": step.description,
                "actions": list(step.actions),
            }
            for step in plan.steps
        ],
        "version_marker": ".sidecar/version.json",
        "pre_migration_snapshot": ".sidecar/migrations/pre-migration-state.json",
    }


def migration_snapshot_payload(repo: Path, current_version: int) -> dict[str, object]:
    captured_files = []
    for relative_path in (
        ".sidecar/VERSION",
        ".sidecar/policy.yaml",
        ".sidecar/version.json",
    ):
        path = repo / relative_path
        exists = path.exists()
        captured_files.append(
            {
                "path": relative_path,
                "existed": exists,
                "content": path.read_text(encoding="utf-8") if exists else None,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "sidecar_migration_snapshot",
        "captured_version": current_version,
        "captured_files": captured_files,
    }


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
