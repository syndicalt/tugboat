from __future__ import annotations

import json
import subprocess
import sys
import venv
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

from tugboat.manifests import REQUIRED_MANIFEST_NAMES


RELEASE_VERSION = "1.0.0"


def test_built_wheel_runs_fresh_repo_proposal_loop(tmp_path: Path):
    wheel = _build_wheel(tmp_path)
    assert wheel.name == f"tugboat-{RELEASE_VERSION}-py3-none-any.whl"
    venv_python = _install_wheel_in_venv(tmp_path, wheel)
    bin_dir = venv_python.parent
    tugboat = bin_dir / "tugboat"
    fixture_llmff = bin_dir / "tugboat-fixture-llmff"
    repo = tmp_path / "fresh"
    repo.mkdir()
    docs_dir = repo / "docs"
    docs_dir.mkdir()
    original = "# Rules\n\nUse tests. See [runbook](docs/runbook.md).\n"
    (repo / "CODEX.md").write_text(original, encoding="utf-8")
    (docs_dir / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n# Runbook\n\nUse tests.\n",
        encoding="utf-8",
    )
    trace = repo / "traces" / "episode.jsonl"
    trace.parent.mkdir()
    trace.write_text(
        '{"type":"user_request","text":"Fix bug"}\n'
        '{"type":"user_correction","text":"You skipped the regression test"}\n',
        encoding="utf-8",
    )

    assert tugboat.exists()
    assert fixture_llmff.exists()
    doctor_output = _run_installed(tugboat, "doctor", "--repo", str(repo))
    init_output = _run_installed(tugboat, "init", "--repo", str(repo))
    index_output = _run_installed(tugboat, "index", "--repo", str(repo), "--check")
    harness_output = _run_installed(tugboat, "harness", "check", "--repo", str(repo))
    optimize_output = _run_installed(
        tugboat, "optimize", "--repo", str(repo), "--trace", str(trace), "--suite", "all"
    )

    assert "tugboat: ok" in doctor_output
    assert "initialized:" in init_output
    assert "indexed documents: 1" in index_output
    assert "harness: ok" in harness_output
    assert "optimization: needs_review" in optimize_output
    assert "report:" in optimize_output

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    for artifact_name in (
        "audit.json",
        "candidate.json",
        "eval-report.json",
        "optimization-summary.json",
        "report.md",
    ):
        assert (run_dir / artifact_name).exists(), artifact_name
    assert (run_dir / "report.md").exists()
    inspect = json.loads(
        (run_dir / "patch-propose" / "llmff-inspect.json").read_text(encoding="utf-8")
    )
    assert inspect["inspect"]["fixture_backend"] == "tugboat"
    optimization_summary = json.loads(
        (run_dir / "optimization-summary.json").read_text(encoding="utf-8")
    )
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert optimization_summary["decision"] == "needs_review"
    assert eval_report["suite_id"] == "all"


def test_wheel_contains_runtime_manifest_templates(tmp_path: Path):
    wheel = _build_wheel(tmp_path)

    with ZipFile(wheel) as archive:
        names = set(archive.namelist())

    for manifest in REQUIRED_MANIFEST_NAMES:
        assert f"tugboat/manifests/templates/{manifest}" in names


def test_built_wheel_metadata_uses_v1_release_version(tmp_path: Path):
    wheel = _build_wheel(tmp_path)

    with ZipFile(wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))

    assert metadata["Name"] == "tugboat"
    assert metadata["Version"] == RELEASE_VERSION


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


def _run_installed(tugboat: Path, *args: str) -> str:
    result = subprocess.run(
        [str(tugboat), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=tugboat.parent,
    )
    return result.stdout
