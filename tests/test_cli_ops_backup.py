from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path

from tugboat.artifacts import ArtifactValidationError
from tugboat.cli import _write_ops_command_bundle, main
from tugboat.paths import sidecar_dir


def _create_sidecar_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE item(id INTEGER)")
        connection.commit()


def test_ops_backup_writes_non_executing_sidecar_backup_plan(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    archive = repo / "sidecar-backup.tgz"
    sidecar_dir(repo).mkdir(parents=True)

    assert main(["ops", "backup", "--repo", str(repo), "--archive", str(archive)]) == 0

    output_path = sidecar_dir(repo) / "ops" / "backup-plan.json"
    output = capsys.readouterr().out
    assert f"backup plan: {output_path}" in output
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["bundle"]["name"] == "sidecar-backup"
    assert payload["bundle"]["commands"][0] == {
        "label": "create sidecar archive",
        "argv": ["tar", "-czf", str(archive.resolve()), "-C", str(repo.resolve()), ".sidecar"],
    }
    assert payload["bundle"]["commands"][1]["stdout_path"] == str(archive.resolve()) + ".sha256"
    assert not archive.exists()


def test_ops_backup_execute_creates_verified_archive_and_checksum(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    archive = repo / "sidecar-backup.tgz"
    sidecar = sidecar_dir(repo)
    sidecar.mkdir(parents=True)
    _create_sidecar_db(sidecar / "db.sqlite")
    (sidecar / "policy.yaml").write_text("version: 1\n", encoding="utf-8")

    assert (
        main(["ops", "backup", "--repo", str(repo), "--archive", str(archive), "--execute"])
        == 0
    )

    output = capsys.readouterr().out
    assert f"backup archive: {archive.resolve()}" in output
    assert archive.exists()
    checksum_path = Path(f"{archive.resolve()}.sha256")
    assert checksum_path.exists()
    digest, filename = checksum_path.read_text(encoding="utf-8").strip().split("  ")
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert filename == str(archive.resolve())
    with tarfile.open(archive, "r:gz") as tar:
        names = set(tar.getnames())
    assert ".sidecar/db.sqlite" in names
    assert ".sidecar/policy.yaml" in names


def test_ops_backup_blocks_archive_inside_sidecar_without_writing_plan(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    sidecar = sidecar_dir(repo)
    sidecar.mkdir(parents=True)

    assert (
        main(
            [
                "ops",
                "backup",
                "--repo",
                str(repo),
                "--archive",
                str(sidecar / "sidecar-backup.tgz"),
            ]
        )
        == 1
    )

    assert "backup plan blocked: archive must resolve outside .sidecar" in capsys.readouterr().out
    assert not (sidecar / "ops" / "backup-plan.json").exists()


def test_ops_restore_writes_non_executing_sidecar_restore_plan(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    archive = repo / "sidecar-backup.tgz"
    staging = repo / "restore-check"
    pre_restore = repo / ".sidecar.pre-restore"
    sidecar_dir(repo).mkdir(parents=True)

    assert (
        main(
            [
                "ops",
                "restore",
                "--repo",
                str(repo),
                "--archive",
                str(archive),
                "--staging",
                str(staging),
                "--pre-restore",
                str(pre_restore),
            ]
        )
        == 0
    )

    output_path = sidecar_dir(repo) / "ops" / "restore-plan.json"
    output = capsys.readouterr().out
    assert f"restore plan: {output_path}" in output
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["bundle"]["name"] == "sidecar-restore"
    assert payload["bundle"]["commands"][1] == {
        "label": "extract archive into staging directory",
        "argv": ["tar", "-xzf", str(archive.resolve()), "-C", str(staging.resolve())],
    }
    assert payload["bundle"]["commands"][-1]["argv"] == [
        "tugboat",
        "harness",
        "check",
        "--repo",
        str(repo.resolve()),
    ]
    assert not staging.exists()


def test_ops_restore_execute_verifies_archive_and_replaces_sidecar(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    archive = repo / "sidecar-backup.tgz"
    staging = repo / "restore-check"
    pre_restore = repo / ".sidecar.pre-restore"
    sidecar = sidecar_dir(repo)
    sidecar.mkdir(parents=True)
    _create_sidecar_db(sidecar / "db.sqlite")
    (sidecar / "policy.yaml").write_text("version: 1\n", encoding="utf-8")
    assert (
        main(["ops", "backup", "--repo", str(repo), "--archive", str(archive), "--execute"])
        == 0
    )
    (sidecar / "policy.yaml").write_text("version: 2\n", encoding="utf-8")

    assert (
        main(
            [
                "ops",
                "restore",
                "--repo",
                str(repo),
                "--archive",
                str(archive),
                "--staging",
                str(staging),
                "--pre-restore",
                str(pre_restore),
                "--execute",
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert f"restored sidecar: {sidecar.resolve()}" in output
    assert (sidecar / "policy.yaml").read_text(encoding="utf-8") == "version: 1\n"
    assert (pre_restore / "policy.yaml").read_text(encoding="utf-8") == "version: 2\n"
    assert not staging.exists()


def test_ops_restore_execute_is_blocked_by_read_only_kill_switch(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    archive = repo / "sidecar-backup.tgz"
    staging = repo / "restore-check"
    pre_restore = repo / ".sidecar.pre-restore"
    sidecar = sidecar_dir(repo)
    sidecar.mkdir(parents=True)
    _create_sidecar_db(sidecar / "db.sqlite")
    (sidecar / "policy.yaml").write_text("version: 1\n", encoding="utf-8")
    assert (
        main(["ops", "backup", "--repo", str(repo), "--archive", str(archive), "--execute"])
        == 0
    )
    (sidecar / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    assert (
        main(
            [
                "ops",
                "restore",
                "--repo",
                str(repo),
                "--archive",
                str(archive),
                "--staging",
                str(staging),
                "--pre-restore",
                str(pre_restore),
                "--execute",
            ]
        )
        == 1
    )

    assert "restore blocked: read-only kill switch is enabled" in capsys.readouterr().out
    assert (sidecar / "read-only.kill").exists()
    assert not pre_restore.exists()


def test_ops_restore_blocks_staging_and_pre_restore_inside_sidecar_without_writing_plan(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path
    sidecar = sidecar_dir(repo)
    sidecar.mkdir(parents=True)
    archive = repo / "sidecar-backup.tgz"

    assert (
        main(
            [
                "ops",
                "restore",
                "--repo",
                str(repo),
                "--archive",
                str(archive),
                "--staging",
                str(sidecar / "restore-check"),
                "--pre-restore",
                str(repo / ".sidecar.pre-restore"),
            ]
        )
        == 1
    )
    assert "restore plan blocked: staging must resolve outside .sidecar" in capsys.readouterr().out
    assert not (sidecar / "ops" / "restore-plan.json").exists()

    assert (
        main(
            [
                "ops",
                "restore",
                "--repo",
                str(repo),
                "--archive",
                str(archive),
                "--staging",
                str(repo / "restore-check"),
                "--pre-restore",
                str(sidecar / "pre-restore"),
            ]
        )
        == 1
    )
    assert "restore plan blocked: pre-restore path must resolve outside .sidecar" in capsys.readouterr().out
    assert not (sidecar / "ops" / "restore-plan.json").exists()


def test_write_ops_command_bundle_validates_payload_before_writing(tmp_path: Path) -> None:
    output_path = sidecar_dir(tmp_path) / "ops" / "backup-plan.json"

    try:
        _write_ops_command_bundle(
            output_path,
            {
                "name": "sidecar-backup",
                "commands": [
                    {
                        "label": "create sidecar archive",
                    }
                ],
            },
        )
    except ArtifactValidationError as error:
        assert "bundle.commands[0].argv" in str(error)
    else:
        raise AssertionError("invalid ops command bundle should be rejected")

    assert not output_path.exists()
