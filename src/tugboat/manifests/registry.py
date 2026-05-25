from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from tugboat.models import Policy
from tugboat.paths import sidecar_dir

REQUIRED_MANIFEST_NAMES = (
    "instruction-index.yaml",
    "episode-audit.yaml",
    "drift-detect.yaml",
    "patch-propose.yaml",
    "patch-eval.yaml",
    "acceptance-summary.yaml",
)


@dataclass(frozen=True)
class ManifestRecord:
    name: str
    path: Path
    sha256: str


def _template_bytes(name: str) -> bytes:
    return (
        files("tugboat.manifests")
        .joinpath("templates")
        .joinpath(name)
        .read_bytes()
    )


def _file_record(path: Path) -> ManifestRecord:
    return ManifestRecord(name=path.name, path=path, sha256=sha256(path.read_bytes()).hexdigest())


def materialize_manifests(repo: Path, *, overwrite: bool = False) -> tuple[ManifestRecord, ...]:
    manifest_dir = sidecar_dir(repo) / "manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    records: list[ManifestRecord] = []
    for name in REQUIRED_MANIFEST_NAMES:
        path = manifest_dir / name
        if overwrite or not path.exists():
            path.write_bytes(_template_bytes(name))
        records.append(_file_record(path))
    return tuple(records)


def manifests_are_allowed_by_policy(records: tuple[ManifestRecord, ...], policy: Policy) -> bool:
    allowlist = set(getattr(policy, "allowed_manifest_hashes", ()))
    if not allowlist:
        return True
    return all(record.sha256 in allowlist for record in records)
