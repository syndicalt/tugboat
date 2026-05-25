from __future__ import annotations

import json
from pathlib import Path

from tugboat.ops.backup import (
    build_sidecar_backup_bundle,
    build_sidecar_restore_bundle,
)


def test_build_sidecar_backup_bundle_returns_required_commands_without_execution(
    tmp_path: Path,
) -> None:
    bundle = build_sidecar_backup_bundle(
        repo=tmp_path,
        archive_path=tmp_path / "sidecar-backup.tgz",
    )

    assert [command.label for command in bundle.commands] == [
        "create sidecar archive",
        "write archive checksum",
        "verify archive checksum",
        "check sidecar sqlite integrity",
        "record tugboat status",
    ]
    assert bundle.commands[0].argv == (
        "tar",
        "-czf",
        str(tmp_path / "sidecar-backup.tgz"),
        ".sidecar",
    )
    assert bundle.commands[1].argv == ("sha256sum", str(tmp_path / "sidecar-backup.tgz"))
    assert bundle.commands[1].stdout_path == str(tmp_path / "sidecar-backup.tgz.sha256")
    assert bundle.commands[3].argv == (
        "sqlite3",
        str(tmp_path / ".sidecar" / "db.sqlite"),
        "PRAGMA integrity_check;",
    )
    assert bundle.commands[4].argv == ("tugboat", "status", "--repo", str(tmp_path))
    assert not (tmp_path / "sidecar-backup.tgz").exists()
    json.dumps(bundle.to_dict())


def test_build_sidecar_restore_bundle_returns_staging_and_verification_commands(
    tmp_path: Path,
) -> None:
    bundle = build_sidecar_restore_bundle(
        repo=tmp_path,
        archive_path=tmp_path / "sidecar-backup.tgz",
        staging_path=tmp_path / "restore-check",
        pre_restore_path=tmp_path / ".sidecar.pre-restore",
    )

    assert [command.label for command in bundle.commands] == [
        "prepare restore staging directory",
        "extract archive into staging directory",
        "check staged sqlite integrity",
        "move current sidecar aside",
        "restore staged sidecar",
        "record tugboat status",
        "run harness check",
    ]
    assert bundle.commands[1].argv == (
        "tar",
        "-xzf",
        str(tmp_path / "sidecar-backup.tgz"),
        "-C",
        str(tmp_path / "restore-check"),
    )
    assert bundle.commands[2].argv == (
        "sqlite3",
        str(tmp_path / "restore-check" / ".sidecar" / "db.sqlite"),
        "PRAGMA integrity_check;",
    )
    assert bundle.commands[-1].argv == (
        "tugboat",
        "harness",
        "check",
        "--repo",
        str(tmp_path),
    )
    assert not (tmp_path / "restore-check").exists()
    json.dumps(bundle.to_dict())
