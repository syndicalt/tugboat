from __future__ import annotations

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
                ("tar", "-czf", str(archive_path), ".sidecar"),
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


def _require_outside_sidecar(repo: Path, path: Path, label: str) -> None:
    sidecar = (repo / ".sidecar").resolve()
    if path == sidecar or path.is_relative_to(sidecar):
        raise ValueError(f"{label} must resolve outside .sidecar")
