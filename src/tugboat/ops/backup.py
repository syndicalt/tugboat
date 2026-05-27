from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tarfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OpsCommand:
    label: str
    argv: tuple[str, ...]
    stdout_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"label": self.label, "argv": list(self.argv)}
        if self.stdout_path is not None:
            payload["stdout_path"] = self.stdout_path
        return payload


@dataclass(frozen=True)
class OpsCommandBundle:
    name: str
    commands: tuple[OpsCommand, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "commands": [command.to_dict() for command in self.commands],
        }


def build_sidecar_backup_bundle(*, repo: Path, archive_path: Path) -> OpsCommandBundle:
    repo = repo.resolve()
    archive_path = archive_path.resolve()
    _require_outside_sidecar(repo, archive_path, "archive")
    checksum_path = Path(f"{archive_path}.sha256")
    sidecar_db = repo / ".sidecar" / "db.sqlite"

    return OpsCommandBundle(
        name="sidecar-backup",
        commands=(
            OpsCommand(
                "create sidecar archive",
                ("tar", "-czf", str(archive_path), "-C", str(repo), ".sidecar"),
            ),
            OpsCommand(
                "write archive checksum",
                ("sha256sum", str(archive_path)),
                stdout_path=str(checksum_path),
            ),
            OpsCommand(
                "verify archive checksum",
                ("sha256sum", "-c", str(checksum_path)),
            ),
            OpsCommand(
                "check sidecar sqlite integrity",
                ("sqlite3", str(sidecar_db), "PRAGMA integrity_check;"),
            ),
            OpsCommand(
                "record tugboat status",
                ("tugboat", "status", "--repo", str(repo)),
            ),
        ),
    )


def execute_sidecar_backup(*, repo: Path, archive_path: Path) -> Path:
    repo = repo.resolve()
    archive_path = archive_path.resolve()
    _require_outside_sidecar(repo, archive_path, "archive")
    sidecar = repo / ".sidecar"
    if not sidecar.is_dir():
        raise ValueError(".sidecar directory is required for backup")
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    _check_sqlite_integrity(sidecar / "db.sqlite")
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(sidecar, arcname=".sidecar")
    checksum_path = Path(f"{archive_path}.sha256")
    digest = _sha256_file(archive_path)
    checksum_path.write_text(f"{digest}  {archive_path}\n", encoding="utf-8")
    _verify_checksum_file(archive_path, checksum_path)
    return archive_path


def build_sidecar_restore_bundle(
    *,
    repo: Path,
    archive_path: Path,
    staging_path: Path,
    pre_restore_path: Path,
) -> OpsCommandBundle:
    repo = repo.resolve()
    archive_path = archive_path.resolve()
    staging_path = staging_path.resolve()
    pre_restore_path = pre_restore_path.resolve()
    _require_outside_sidecar(repo, archive_path, "archive")
    _require_outside_sidecar(repo, staging_path, "staging")
    _require_outside_sidecar(repo, pre_restore_path, "pre-restore path")

    staged_sidecar = staging_path / ".sidecar"

    return OpsCommandBundle(
        name="sidecar-restore",
        commands=(
            OpsCommand(
                "prepare restore staging directory",
                ("mkdir", "-p", str(staging_path)),
            ),
            OpsCommand(
                "extract archive into staging directory",
                ("tar", "-xzf", str(archive_path), "-C", str(staging_path)),
            ),
            OpsCommand(
                "check staged sqlite integrity",
                ("sqlite3", str(staged_sidecar / "db.sqlite"), "PRAGMA integrity_check;"),
            ),
            OpsCommand(
                "move current sidecar aside",
                ("mv", str(repo / ".sidecar"), str(pre_restore_path)),
            ),
            OpsCommand(
                "restore staged sidecar",
                ("mv", str(staged_sidecar), str(repo / ".sidecar")),
            ),
            OpsCommand(
                "record tugboat status",
                ("tugboat", "status", "--repo", str(repo)),
            ),
            OpsCommand(
                "run harness check",
                ("tugboat", "harness", "check", "--repo", str(repo)),
            ),
        ),
    )


def execute_sidecar_restore(
    *,
    repo: Path,
    archive_path: Path,
    staging_path: Path,
    pre_restore_path: Path,
) -> Path:
    repo = repo.resolve()
    archive_path = archive_path.resolve()
    staging_path = staging_path.resolve()
    pre_restore_path = pre_restore_path.resolve()
    _require_outside_sidecar(repo, archive_path, "archive")
    _require_outside_sidecar(repo, staging_path, "staging")
    _require_outside_sidecar(repo, pre_restore_path, "pre-restore path")
    sidecar = repo / ".sidecar"
    if not sidecar.is_dir():
        raise ValueError(".sidecar directory is required before restore")
    if pre_restore_path.exists():
        raise ValueError("pre-restore path already exists")
    if staging_path.exists():
        raise ValueError("staging path already exists")
    checksum_path = Path(f"{archive_path}.sha256")
    if checksum_path.exists():
        _verify_checksum_file(archive_path, checksum_path)
    staging_path.mkdir(parents=True)
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(staging_path, filter="data")
        staged_sidecar = staging_path / ".sidecar"
        if not staged_sidecar.is_dir():
            raise ValueError("archive does not contain .sidecar")
        _check_sqlite_integrity(staged_sidecar / "db.sqlite")
        shutil.move(str(sidecar), str(pre_restore_path))
        shutil.move(str(staged_sidecar), str(sidecar))
    finally:
        if staging_path.exists():
            shutil.rmtree(staging_path)
    return sidecar


def _require_outside_sidecar(repo: Path, path: Path, label: str) -> None:
    sidecar = (repo / ".sidecar").resolve()
    if path == sidecar or path.is_relative_to(sidecar):
        raise ValueError(f"{label} must resolve outside .sidecar")


def _check_sqlite_integrity(path: Path) -> None:
    if not path.is_file():
        raise ValueError("sidecar sqlite database is required")
    with closing(sqlite3.connect(path)) as connection:
        row = connection.execute("PRAGMA integrity_check;").fetchone()
    if row is None or row[0] != "ok":
        raise ValueError("sidecar sqlite integrity check failed")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_checksum_file(archive_path: Path, checksum_path: Path) -> None:
    raw = checksum_path.read_text(encoding="utf-8").strip().split()
    if len(raw) < 2:
        raise ValueError("backup checksum file is malformed")
    expected_digest, expected_path = raw[0], raw[1]
    if Path(expected_path).resolve() != archive_path.resolve():
        raise ValueError("backup checksum path does not match archive")
    if _sha256_file(archive_path) != expected_digest:
        raise ValueError("backup checksum verification failed")
