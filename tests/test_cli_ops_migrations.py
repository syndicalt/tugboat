from __future__ import annotations

import json
from pathlib import Path

from tugboat.cli import main
from tugboat.ops.migrations import DEFAULT_MIGRATIONS


def test_ops_migrate_dry_run_reports_pending_steps_without_mutating(
    tmp_path: Path,
    capsys,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: 1\n", encoding="utf-8")

    assert main(["ops", "migrate", "--repo", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "migration_mode: dry-run" in output
    assert "current_version: 1" in output
    assert f"target_version: {DEFAULT_MIGRATIONS[-1].to_version}" in output
    assert "step: sidecar-v1-to-v2 1->2" in output
    assert "step: sidecar-v2-to-v3 2->3" in output
    assert not (sidecar / "version.json").exists()
    assert not (sidecar / "migrations" / "migration-report.json").exists()


def test_ops_migrate_apply_executes_pending_steps_and_reports_artifact(
    tmp_path: Path,
    capsys,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: 1\n", encoding="utf-8")

    assert main(["ops", "migrate", "--repo", str(tmp_path), "--apply"]) == 0

    output = capsys.readouterr().out
    report_path = sidecar / "migrations" / "migration-report.json"
    assert "migration_mode: apply" in output
    assert f"migration_report: {report_path}" in output
    assert json.loads((sidecar / "version.json").read_text(encoding="utf-8")) == {
        "schema_version": DEFAULT_MIGRATIONS[-1].to_version,
    }
    assert json.loads(report_path.read_text(encoding="utf-8"))["applied_migrations"] == [
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
    ]


def test_ops_migrate_apply_is_blocked_by_read_only_kill_switch(
    tmp_path: Path,
    capsys,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text("version: 1\n", encoding="utf-8")
    (sidecar / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    assert main(["ops", "migrate", "--repo", str(tmp_path), "--apply"]) == 1

    assert "migration blocked: read-only kill switch is enabled" in capsys.readouterr().out
    assert not (sidecar / "version.json").exists()
    assert not (sidecar / "migrations" / "migration-report.json").exists()
