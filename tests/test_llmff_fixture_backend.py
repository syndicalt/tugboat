from __future__ import annotations

import json
from pathlib import Path

from tugboat.llmff.fixture_backend import _episode_evidence_id, _read_json_object, main


def test_fixture_backend_inspect_declares_local_no_network(capsys):
    assert main(["inspect", "--format", "json", "patch-propose.yaml"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "external_calls": [],
        "fixture_backend": "tugboat",
        "manifest": "patch-propose",
        "network_required": False,
        "providers": [],
    }


def test_fixture_backend_runs_all_manifest_outputs(tmp_path: Path):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    manifest_dir = repo / ".sidecar" / "manifests"
    manifest_dir.mkdir()

    instruction_index_manifest = _manifest(manifest_dir, "instruction-index")
    instruction_index = run_dir / "instruction-index.raw.json"
    assert _run_fixture(
        instruction_index_manifest,
        run_dir,
        outputs={"instruction_index": instruction_index},
    ) == 0
    assert json.loads(instruction_index.read_text(encoding="utf-8"))["documents"][0]["path"] == (
        "CODEX.md"
    )

    episode_trace = run_dir / "canonical-episode.json"
    episode_trace.write_text(
        json.dumps(
            {
                "events": [
                    {
                        "event_type": "user_request",
                        "evidence_id": "ev-request",
                    },
                    {
                        "event_type": "user_correction",
                        "evidence_id": "ev-correction",
                    },
                ]
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    audit_manifest = _manifest(manifest_dir, "episode-audit")
    audit_report = run_dir / "audit.raw.json"
    evidence_ids = run_dir / "evidence-ids.raw.json"
    assert _run_fixture(
        audit_manifest,
        run_dir,
        inputs={"episode_trace": episode_trace},
        outputs={"audit_report": audit_report, "evidence_ids": evidence_ids},
    ) == 0
    assert json.loads(audit_report.read_text(encoding="utf-8"))["evidence_refs"] == [
        "ev-correction"
    ]

    drift_manifest = _manifest(manifest_dir, "drift-detect")
    drift_clusters = run_dir / "drift-clusters.raw.json"
    optimizer_notes = run_dir / "optimizer-notes.raw.json"
    assert _run_fixture(
        drift_manifest,
        run_dir,
        inputs={"audit_reports": audit_report},
        outputs={"drift_clusters": drift_clusters, "optimizer_notes": optimizer_notes},
    ) == 0
    assert json.loads(optimizer_notes.read_text(encoding="utf-8"))["notes"][0][
        "evidence_refs"
    ] == ["ev-correction"]

    propose_manifest = _manifest(manifest_dir, "patch-propose")
    candidate_patch = run_dir / "candidate.raw.json"
    proposal_rationale = run_dir / "proposal-rationale.raw.json"
    assert _run_fixture(
        propose_manifest,
        run_dir,
        inputs={"drift_clusters": drift_clusters},
        outputs={"candidate_patch": candidate_patch, "proposal_rationale": proposal_rationale},
    ) == 0
    candidate = json.loads(candidate_patch.read_text(encoding="utf-8"))
    assert candidate["base_file"] == "CODEX.md"
    assert candidate["bounded_edit_metadata"][0]["operator"] == "add"

    eval_manifest = _manifest(manifest_dir, "patch-eval")
    eval_report = run_dir / "eval-report.raw.json"
    policy_decision = run_dir / "policy-decision.raw.json"
    assert _run_fixture(
        eval_manifest,
        run_dir,
        outputs={"eval_report": eval_report, "policy_decision": policy_decision},
    ) == 0
    assert json.loads(eval_report.read_text(encoding="utf-8"))["recommendation"] == "accept"
    assert json.loads(policy_decision.read_text(encoding="utf-8"))["allowed"] is True

    acceptance_manifest = _manifest(manifest_dir, "acceptance-summary")
    acceptance_summary = run_dir / "acceptance-summary.raw.json"
    assert _run_fixture(
        acceptance_manifest,
        run_dir,
        outputs={"acceptance_summary": acceptance_summary},
    ) == 0
    assert json.loads(acceptance_summary.read_text(encoding="utf-8"))[
        "decision_recommendation"
    ] == "needs_review"


def test_fixture_backend_rejects_unknown_manifest(tmp_path: Path):
    manifest = _manifest(tmp_path, "unknown")

    assert _run_fixture(manifest, tmp_path / "run") == 64


def test_fixture_backend_supports_optional_outputs_and_evidence_fallbacks(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    manifest_dir = repo / ".sidecar" / "manifests"
    manifest_dir.mkdir()
    audit_report = run_dir / "audit.raw.json"
    audit_report.write_text(
        json.dumps({"evidence_refs": ["ev-fallback"]}) + "\n",
        encoding="utf-8",
    )
    drift_clusters = run_dir / "drift-clusters.raw.json"

    assert _run_fixture(
        _manifest(manifest_dir, "drift-detect"),
        run_dir,
        inputs={"audit_reports": audit_report},
        outputs={"drift_clusters": drift_clusters},
    ) == 0
    assert drift_clusters.exists()

    assert _run_fixture(
        _manifest(manifest_dir, "patch-propose"),
        run_dir,
        inputs={"drift_clusters": drift_clusters},
        outputs={"candidate_patch": run_dir / "candidate.raw.json"},
    ) == 0
    assert not (run_dir / "proposal-rationale.raw.json").exists()
    assert _episode_evidence_id({}) == "ev_fixture"
    assert _episode_evidence_id({"events": [{"event_type": "tool", "evidence_id": "ev-tool"}]}) == (
        "ev-tool"
    )
    assert _episode_evidence_id({"events": [{}]}) == "ev_fixture"


def test_fixture_backend_rejects_non_object_json_input(tmp_path: Path):
    payload = tmp_path / "array.json"
    payload.write_text("[]\n", encoding="utf-8")

    try:
        _read_json_object(payload)
    except ValueError as error:
        assert "fixture input must be a JSON object" in str(error)
    else:
        raise AssertionError("non-object fixture JSON should be rejected")


def _manifest(directory: Path, name: str) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(f"name: {name}\n", encoding="utf-8")
    return path


def _run_fixture(
    manifest: Path,
    run_dir: Path,
    *,
    inputs: dict[str, Path] | None = None,
    outputs: dict[str, Path] | None = None,
) -> int:
    args = [
        "run",
        str(manifest),
        "--trace",
        str(run_dir / manifest.stem / "llmff-trace.jsonl"),
        "--events",
        str(run_dir / manifest.stem / "llmff-events.jsonl"),
        "--checkpoint",
        str(run_dir / manifest.stem / "checkpoint.json"),
        "--timeout-ms",
        "60000",
        "--retry-attempts",
        "0",
        "--retry-backoff-ms",
        "0",
    ]
    for name, path in (inputs or {}).items():
        args.extend(["--input", name, str(path)])
    for name, path in (outputs or {}).items():
        args.extend(["--output", name, str(path)])
    return main(args)
