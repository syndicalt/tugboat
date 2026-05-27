from __future__ import annotations

import json
import subprocess
import sys
import venv
from pathlib import Path
from zipfile import ZipFile

from tugboat.manifests import REQUIRED_MANIFEST_NAMES


def test_built_wheel_runs_fresh_repo_proposal_loop(tmp_path: Path):
    wheel = _build_wheel(tmp_path)
    venv_python = _install_wheel_in_venv(tmp_path, wheel)
    bin_dir = venv_python.parent
    tugboat = bin_dir / "tugboat"
    fixture_llmff = bin_dir / "tugboat-fixture-llmff"
    repo = tmp_path / "fresh"
    repo.mkdir()
    original = "# Rules\n\nUse tests.\n"
    (repo / "CODEX.md").write_text(original, encoding="utf-8")
    trace = repo / "traces" / "episode.jsonl"
    trace.parent.mkdir()
    trace.write_text(
        '{"type":"user_request","text":"Fix bug"}\n'
        '{"type":"user_correction","text":"You skipped the regression test"}\n',
        encoding="utf-8",
    )

    assert tugboat.exists()
    assert fixture_llmff.exists()
    _run_installed(tugboat, "init", "--repo", str(repo))
    _run_installed(tugboat, "index", "--repo", str(repo))
    _run_installed(tugboat, "audit", "--repo", str(repo), "--trace", str(trace))
    _run_installed(tugboat, "propose", "--repo", str(repo), "--audit", "latest")
    _run_installed(tugboat, "eval", "--repo", str(repo), "--candidate", "latest", "--suite", "all")
    _run_installed(tugboat, "report", "--repo", str(repo), "--run", "latest")

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    assert (run_dir / "report.md").exists()
    inspect = json.loads(
        (run_dir / "patch-propose" / "llmff-inspect.json").read_text(encoding="utf-8")
    )
    assert inspect["inspect"]["fixture_backend"] == "tugboat"


def test_wheel_contains_runtime_manifest_templates(tmp_path: Path):
    wheel = _build_wheel(tmp_path)

    with ZipFile(wheel) as archive:
        names = set(archive.namelist())

    for manifest in REQUIRED_MANIFEST_NAMES:
        assert f"tugboat/manifests/templates/{manifest}" in names


def _build_wheel(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "dist"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(dist_dir),
        ],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    wheels = sorted(dist_dir.glob("tugboat-*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _install_wheel_in_venv(tmp_path: Path, wheel: Path) -> Path:
    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", str(wheel)],
        check=True,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return python


def _run_installed(tugboat: Path, *args: str) -> None:
    subprocess.run(
        [str(tugboat), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=tugboat.parent,
    )
