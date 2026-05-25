from pathlib import Path

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


def test_latest_run_dir_returns_most_recent_directory(tmp_path: Path):
    first = new_run_dir(tmp_path)
    second = new_run_dir(tmp_path)

    assert latest_run_dir(tmp_path) == second
    assert first != second


def test_latest_run_dir_fails_when_no_runs_exist(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        latest_run_dir(tmp_path)
