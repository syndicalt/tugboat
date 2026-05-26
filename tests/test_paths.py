import os
from pathlib import Path
from stat import S_IMODE

import pytest

from tugboat.paths import latest_run_dir, new_run_dir, runs_dir


def test_new_run_dir_creates_unique_repo_local_directory(tmp_path: Path):
    first = new_run_dir(tmp_path)
    second = new_run_dir(tmp_path)

    assert first.parent == runs_dir(tmp_path)
    assert second.parent == runs_dir(tmp_path)
    assert first != second
    assert first.is_dir()
    assert second.is_dir()


def test_new_run_dir_creates_private_sidecar_directories(tmp_path: Path):
    previous_umask = os.umask(0o022)
    try:
        run_dir = new_run_dir(tmp_path)
    finally:
        os.umask(previous_umask)

    assert S_IMODE((tmp_path / ".sidecar").stat().st_mode) == 0o700
    assert S_IMODE((tmp_path / ".sidecar" / "runs").stat().st_mode) == 0o700
    assert S_IMODE(run_dir.stat().st_mode) == 0o700


def test_latest_run_dir_returns_most_recent_directory(tmp_path: Path):
    first = new_run_dir(tmp_path)
    second = new_run_dir(tmp_path)

    assert latest_run_dir(tmp_path) == second
    assert first != second


def test_latest_run_dir_fails_when_no_runs_exist(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        latest_run_dir(tmp_path)
