from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
from tugboat.paths import runs_dir


def write_eval_report(
    repo: Path,
    run_id: str,
    *,
    candidate_id: int,
    suite_id: str,
    passed: bool,
    metrics: dict[str, Any],
) -> Path:
    run_dir = _repo_local_run_dir(repo, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    report_path = run_dir / "eval-report.json"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate_id,
        "metrics": metrics,
        "passed": passed,
        "suite_id": suite_id,
    }
    validate_json_artifact("eval-report.json", payload)
    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report_path


def _repo_local_run_dir(repo: Path, run_id: str) -> Path:
    run_dir = runs_dir(repo) / run_id
    if not run_dir.resolve().is_relative_to(repo.resolve()):
        raise ValueError("run_id must resolve inside repo")
    return run_dir
