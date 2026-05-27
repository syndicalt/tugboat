from __future__ import annotations

import json
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

from tugboat.ops.backup import (
    build_sidecar_backup_bundle,
    build_sidecar_restore_bundle,
    execute_sidecar_backup,
    execute_sidecar_restore,
)


def _create_sidecar_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE item(id INTEGER)")
        connection.commit()


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
        "-C",
        str(tmp_path),
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


def test_execute_sidecar_backup_requires_existing_sidecar_and_database(tmp_path: Path) -> None:
    try:
        execute_sidecar_backup(repo=tmp_path, archive_path=tmp_path / "backup.tgz")
    except ValueError as error:
        assert str(error) == ".sidecar directory is required for backup"
    else:
        raise AssertionError("backup without .sidecar should fail")

    (tmp_path / ".sidecar").mkdir()
    try:
        execute_sidecar_backup(repo=tmp_path, archive_path=tmp_path / "backup.tgz")
    except ValueError as error:
        assert str(error) == "sidecar sqlite database is required"
    else:
        raise AssertionError("backup without sqlite database should fail")


def test_execute_sidecar_restore_rejects_unsafe_existing_paths(tmp_path: Path) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    _create_sidecar_db(sidecar / "db.sqlite")
    archive = execute_sidecar_backup(repo=tmp_path, archive_path=tmp_path / "backup.tgz")
    (tmp_path / ".sidecar.pre-restore").mkdir()

    try:
        execute_sidecar_restore(
            repo=tmp_path,
            archive_path=archive,
            staging_path=tmp_path / "restore-check",
            pre_restore_path=tmp_path / ".sidecar.pre-restore",
        )
    except ValueError as error:
        assert str(error) == "pre-restore path already exists"
    else:
        raise AssertionError("restore with existing pre-restore path should fail")

    (tmp_path / ".sidecar.pre-restore").rmdir()
    (tmp_path / "restore-check").mkdir()
    try:
        execute_sidecar_restore(
            repo=tmp_path,
            archive_path=archive,
            staging_path=tmp_path / "restore-check",
            pre_restore_path=tmp_path / ".sidecar.pre-restore",
        )
    except ValueError as error:
        assert str(error) == "staging path already exists"
    else:
        raise AssertionError("restore with existing staging path should fail")


def test_execute_sidecar_restore_rejects_archive_without_sidecar(tmp_path: Path) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    _create_sidecar_db(sidecar / "db.sqlite")
    archive = tmp_path / "bad.tgz"
    payload = tmp_path / "payload.txt"
    payload.write_text("not a sidecar\n", encoding="utf-8")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="payload.txt")

    try:
        execute_sidecar_restore(
            repo=tmp_path,
            archive_path=archive,
            staging_path=tmp_path / "restore-check",
            pre_restore_path=tmp_path / ".sidecar.pre-restore",
        )
    except ValueError as error:
        assert str(error) == "archive does not contain .sidecar"
    else:
        raise AssertionError("restore without .sidecar in archive should fail")
    assert not (tmp_path / "restore-check").exists()


def test_execute_sidecar_restore_rejects_bad_checksum(tmp_path: Path) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    _create_sidecar_db(sidecar / "db.sqlite")
    archive = execute_sidecar_backup(repo=tmp_path, archive_path=tmp_path / "backup.tgz")
    Path(f"{archive}.sha256").write_text("0" * 64 + f"  {archive}\n", encoding="utf-8")

    try:
        execute_sidecar_restore(
            repo=tmp_path,
            archive_path=archive,
            staging_path=tmp_path / "restore-check",
            pre_restore_path=tmp_path / ".sidecar.pre-restore",
        )
    except ValueError as error:
        assert str(error) == "backup checksum verification failed"
    else:
        raise AssertionError("restore with checksum mismatch should fail")
