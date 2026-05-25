from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import sleep


def sidecar_dir(repo: Path) -> Path:
    return repo / ".sidecar"


def runs_dir(repo: Path) -> Path:
    return sidecar_dir(repo) / "runs"


def new_run_dir(repo: Path) -> Path:
    runs = runs_dir(repo)
    runs.mkdir(parents=True, exist_ok=True)
    while True:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = runs / stamp
        try:
            path.mkdir()
            return path
        except FileExistsError:
            sleep(0.001)


def latest_run_dir(repo: Path) -> Path:
    runs = runs_dir(repo)
    if not runs.exists():
        raise FileNotFoundError("no tugboat run directories exist")
    candidates = sorted(path for path in runs.iterdir() if path.is_dir())
    if not candidates:
        raise FileNotFoundError("no tugboat run directories exist")
    return candidates[-1]
