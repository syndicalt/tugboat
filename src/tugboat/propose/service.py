from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact
from tugboat.patches import apply_unified_diff
from tugboat.paths import runs_dir
from tugboat.policy.gate import CandidatePatch
from tugboat.security.secrets import scan_text


@dataclass(frozen=True)
class CandidateArtifacts:
    diff_path: Path
    json_path: Path
    preview_path: Path
    preview_manifest_path: Path


def write_candidate(repo: Path, run_id: str, candidate: CandidatePatch) -> CandidateArtifacts:
    run_dir = _repo_local_run_dir(repo, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    diff_path = run_dir / "candidate.diff"
    json_path = run_dir / "candidate.json"
    preview_path = run_dir / "candidate-preview" / candidate.base_file
    preview_manifest_path = run_dir / "candidate-preview.json"
    try:
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
        preview_path, preview_manifest_path = _write_candidate_preview(repo, run_dir, candidate)
    except Exception:
        _remove_candidate_artifacts(
            diff_path=diff_path,
            json_path=json_path,
            preview_path=preview_path,
            preview_manifest_path=preview_manifest_path,
        )
        raise
    return CandidateArtifacts(
        diff_path=diff_path,
        json_path=json_path,
        preview_path=preview_path,
        preview_manifest_path=preview_manifest_path,
    )


def _repo_local_run_dir(repo: Path, run_id: str) -> Path:
    run_dir = runs_dir(repo) / run_id
    if not run_dir.resolve().is_relative_to(repo.resolve()):
        raise ValueError("run_id must resolve inside repo")
    return run_dir


def _remove_candidate_artifacts(
    *,
    diff_path: Path,
    json_path: Path,
    preview_path: Path,
    preview_manifest_path: Path,
) -> None:
    diff_path.unlink(missing_ok=True)
    json_path.unlink(missing_ok=True)
    preview_manifest_path.unlink(missing_ok=True)
    preview_root = preview_path.parents[0]
    while preview_root.name != "candidate-preview":
        preview_root = preview_root.parent
    shutil.rmtree(preview_root, ignore_errors=True)


def _write_candidate_preview(
    repo: Path,
    run_dir: Path,
    candidate: CandidatePatch,
) -> tuple[Path, Path]:
    base_path = (repo / candidate.base_file).resolve()
    if not base_path.is_relative_to(repo.resolve()):
        raise ValueError("candidate base_file must resolve inside repo")
    if not base_path.exists():
        raise ValueError("candidate base_file does not exist")
    if CandidatePatch.hash_file(base_path) != candidate.base_hash:
        raise ValueError("candidate base_hash does not match current file")

    preview_text = apply_unified_diff(base_path.read_text(encoding="utf-8"), candidate.diff)
    if preview_text is None:
        raise ValueError("candidate diff cannot be applied to base file")
    preview_path = (run_dir / "candidate-preview" / candidate.base_file).resolve()
    if not preview_path.is_relative_to((run_dir / "candidate-preview").resolve()):
        raise ValueError("candidate preview path must resolve inside preview directory")
    findings = scan_text(preview_path.as_posix(), preview_text)
    if findings:
        from tugboat.security.secrets import SecretScanError

        raise SecretScanError(findings)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.write_text(preview_text, encoding="utf-8")

    preview_manifest_path = run_dir / "candidate-preview.json"
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "base_file": candidate.base_file,
        "base_hash": candidate.base_hash,
        "diff_hash": candidate.diff_hash,
        "preview_path": preview_path.relative_to(repo).as_posix(),
        "preview_hash": CandidatePatch.hash_file(preview_path),
    }
    validate_json_artifact("candidate-preview.json", manifest)
    preview_manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return preview_path, preview_manifest_path
