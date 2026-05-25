from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
from tugboat.paths import runs_dir
from tugboat.policy.gate import CandidatePatch
from tugboat.security.secrets import scan_text


@dataclass(frozen=True)
class CandidateArtifacts:
    diff_path: Path
    json_path: Path


def write_candidate(repo: Path, run_id: str, candidate: CandidatePatch) -> CandidateArtifacts:
    run_dir = _repo_local_run_dir(repo, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    diff_path = run_dir / "candidate.diff"
    json_path = run_dir / "candidate.json"
    findings = scan_text(diff_path.as_posix(), candidate.diff)
    if findings:
        from tugboat.security.secrets import SecretScanError

        raise SecretScanError(findings)
    diff_path.write_text(candidate.diff, encoding="utf-8")
    artifact = {"schema_version": SCHEMA_VERSION, **candidate.to_json_dict()}
    validate_json_artifact("candidate.json", artifact)
    json_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return CandidateArtifacts(diff_path=diff_path, json_path=json_path)


def _repo_local_run_dir(repo: Path, run_id: str) -> Path:
    run_dir = runs_dir(repo) / run_id
    if not run_dir.resolve().is_relative_to(repo.resolve()):
        raise ValueError("run_id must resolve inside repo")
    return run_dir
