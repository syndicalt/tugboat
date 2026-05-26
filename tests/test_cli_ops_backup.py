from __future__ import annotations

import json
from pathlib import Path

from tugboat.cli import main
from tugboat.paths import sidecar_dir


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
        "argv": ["tar", "-czf", str(archive.resolve()), ".sidecar"],
    }
    assert payload["bundle"]["commands"][1]["stdout_path"] == str(archive.resolve()) + ".sha256"
    assert not archive.exists()


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
