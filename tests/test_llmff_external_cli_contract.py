from __future__ import annotations

import json
from pathlib import Path

from tugboat.cli import main


def test_audit_uses_strict_external_llmff_cli_contract(
    tmp_path: Path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = repo / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    log_path = tmp_path / "llmff-contract-log.jsonl"
    strict_llmff = _write_strict_llmff(tmp_path / "strict-llmff")
    monkeypatch.setenv("TUGBOAT_CONTRACT_LOG", str(log_path))
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {strict_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [(record["command"], record["manifest"]) for record in records] == [
        ("inspect", "instruction-index"),
        ("run", "instruction-index"),
        ("inspect", "episode-audit"),
        ("run", "episode-audit"),
    ]
    for inspect_record in (records[0], records[2]):
        assert inspect_record["argv"][1:3] == ["inspect", "--format"]
        assert inspect_record["argv"][3] == "json"
        assert inspect_record["network_required"] is False
        assert inspect_record["providers"] == []
        assert inspect_record["external_calls"] == []
    assert records[1]["inputs"] == {
        "instruction_corpus": f"{run_dir}/instruction-snapshot",
        "policy": f"{repo}/.sidecar/policy.yaml",
    }
    assert records[1]["outputs"] == {
        "instruction_index": f"{run_dir}/instruction-index.raw.json",
    }
    assert records[3]["inputs"] == {
        "episode_trace": f"{run_dir}/canonical-episode.json",
        "instruction_index": f"{run_dir}/instruction-index.raw.json",
        "policy": f"{repo}/.sidecar/policy.yaml",
    }
    assert records[3]["outputs"] == {
        "audit_report": f"{run_dir}/audit.raw.json",
        "evidence_ids": f"{run_dir}/evidence-ids.raw.json",
    }
    assert records[1]["trace_path"] == f"{run_dir}/instruction-index/llmff-trace.jsonl"
    assert records[1]["events_path"] == f"{run_dir}/instruction-index/llmff-events.jsonl"
    assert records[1]["checkpoint_path"] == f"{run_dir}/instruction-index/checkpoint.json"
    assert records[3]["trace_path"] == f"{run_dir}/episode-audit/llmff-trace.jsonl"
    assert records[3]["events_path"] == f"{run_dir}/episode-audit/llmff-events.jsonl"
    assert records[3]["checkpoint_path"] == f"{run_dir}/episode-audit/checkpoint.json"


def test_propose_uses_strict_external_llmff_cli_contract(
    tmp_path: Path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    trace = repo / "trace.jsonl"
    trace.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")
    log_path = tmp_path / "llmff-contract-log.jsonl"
    strict_llmff = _write_strict_llmff(tmp_path / "strict-llmff")
    monkeypatch.setenv("TUGBOAT_CONTRACT_LOG", str(log_path))
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {strict_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["audit", "--repo", str(repo), "--trace", str(trace)]) == 0
    assert main(["propose", "--repo", str(repo), "--audit", "latest"]) == 0

    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    records = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [(record["command"], record["manifest"]) for record in records] == [
        ("inspect", "instruction-index"),
        ("run", "instruction-index"),
        ("inspect", "episode-audit"),
        ("run", "episode-audit"),
        ("inspect", "drift-detect"),
        ("run", "drift-detect"),
        ("inspect", "patch-propose"),
        ("run", "patch-propose"),
    ]
    assert records[5]["inputs"] == {
        "audit_reports": f"{run_dir}/audit.raw.json",
        "instruction_index": f"{run_dir}/instruction-snapshot",
        "instruction_index_artifact": f"{run_dir}/instruction-index.raw.json",
        "policy": f"{repo}/.sidecar/policy.yaml",
    }
    assert records[5]["outputs"] == {
        "drift_clusters": f"{run_dir}/drift.raw.json",
        "optimizer_notes": f"{run_dir}/optimizer-notes.raw.json",
    }
    assert records[7]["inputs"] == {
        "instruction_index": f"{run_dir}/instruction-snapshot",
        "instruction_index_artifact": f"{run_dir}/instruction-index.raw.json",
        "drift_clusters": f"{run_dir}/drift.raw.json",
        "optimizer_notes": f"{run_dir}/optimizer-notes.raw.json",
        "optimizer_memory": f"{run_dir}/optimizer-memory.json",
        "policy": f"{repo}/.sidecar/policy.yaml",
    }
    assert records[7]["outputs"] == {
        "candidate_patch": f"{run_dir}/candidate.raw.json",
        "proposal_rationale": f"{run_dir}/proposal-rationale.raw.json",
    }
    assert records[5]["trace_path"] == f"{run_dir}/drift-detect/llmff-trace.jsonl"
    assert records[5]["events_path"] == f"{run_dir}/drift-detect/llmff-events.jsonl"
    assert records[5]["checkpoint_path"] == f"{run_dir}/drift-detect/checkpoint.json"
    assert records[7]["trace_path"] == f"{run_dir}/patch-propose/llmff-trace.jsonl"
    assert records[7]["events_path"] == f"{run_dir}/patch-propose/llmff-events.jsonl"
    assert records[7]["checkpoint_path"] == f"{run_dir}/patch-propose/checkpoint.json"

    raw_candidate = json.loads((run_dir / "candidate.raw.json").read_text(encoding="utf-8"))
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    rationale = json.loads((run_dir / "proposal-rationale.raw.json").read_text(encoding="utf-8"))
    assert raw_candidate["rationale"] == "strict external llmff proposed this from audited evidence"
    assert candidate["rationale"] == raw_candidate["rationale"]
    assert raw_candidate["bounded_edit_metadata"] == [
        {
            "operator": "add",
            "file": "CODEX.md",
            "section": "Rules",
            "changed_lines": 1,
            "normative_changes": 0,
        }
    ]
    assert rationale["evidence_refs"] == [raw_candidate["sources"][0]["source_id"]]


def _write_strict_llmff(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import os
import sys
from pathlib import Path


def log(record):
    log_path = Path(os.environ["TUGBOAT_CONTRACT_LOG"])
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\\n")


def fail(message):
    sys.stderr.write(message + "\\n")
    raise SystemExit(64)


argv = sys.argv
args = argv[1:]
if len(args) == 4 and args[:3] == ["inspect", "--format", "json"]:
    manifest_path = Path(args[3])
    manifest = manifest_path.stem
    payload = {
        "manifest": manifest,
        "network_required": False,
        "providers": [],
        "external_calls": [],
    }
    log({"argv": argv, "command": "inspect", **payload})
    print(json.dumps(payload, sort_keys=True))
    raise SystemExit(0)

if len(args) >= 14 and args[0] == "run":
    manifest_path = Path(args[1])
    manifest = manifest_path.stem
    expected_flags = [
        "--trace",
        "--events",
        "--checkpoint",
        "--timeout-ms",
        "--retry-attempts",
        "--retry-backoff-ms",
    ]
    fixed = args[2:14]
    if fixed[0::2] != expected_flags:
        fail("unexpected fixed run flag order: " + repr(fixed))
    trace_path = Path(fixed[1])
    events_path = Path(fixed[3])
    checkpoint_path = Path(fixed[5])
    inputs = {}
    outputs = {}
    index = 14
    while index < len(args):
        flag = args[index]
        if flag == "--input":
            if index + 2 >= len(args):
                fail("malformed --input")
            inputs[args[index + 1]] = args[index + 2]
            index += 3
            continue
        if flag == "--output":
            if index + 2 >= len(args):
                fail("malformed --output")
            outputs[args[index + 1]] = args[index + 2]
            index += 3
            continue
        fail("unexpected run flag: " + flag)
    expected_inputs = {
        "instruction-index": ["instruction_corpus", "policy"],
        "episode-audit": ["episode_trace", "instruction_index", "policy"],
        "drift-detect": ["audit_reports", "instruction_index", "instruction_index_artifact", "policy"],
        "patch-propose": [
            "instruction_index",
            "instruction_index_artifact",
            "drift_clusters",
            "optimizer_notes",
            "optimizer_memory",
            "policy",
        ],
    }[manifest]
    expected_outputs = {
        "instruction-index": ["instruction_index"],
        "episode-audit": ["audit_report", "evidence_ids"],
        "drift-detect": ["drift_clusters", "optimizer_notes"],
        "patch-propose": ["candidate_patch", "proposal_rationale"],
    }[manifest]
    if list(inputs) != expected_inputs:
        fail(f"{manifest} inputs out of contract: {list(inputs)}")
    if list(outputs) != expected_outputs:
        fail(f"{manifest} outputs out of contract: {list(outputs)}")
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(json.dumps({"event": "step", "manifest": manifest}) + "\\n", encoding="utf-8")
    events_path.write_text(json.dumps({"event": "run_completed", "manifest": manifest}) + "\\n", encoding="utf-8")
    checkpoint_path.write_text(
        json.dumps({"manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest()}, sort_keys=True) + "\\n",
        encoding="utf-8",
    )
    if manifest == "instruction-index":
        Path(outputs["instruction_index"]).write_text(
            json.dumps({
                "documents": [{
                    "path": "CODEX.md",
                    "obligations": ["Use tests."],
                    "chunks": [{
                        "ref": "CODEX.md#rules",
                        "anchor": "rules",
                        "heading_path": ["Rules"],
                    }],
                }],
            }, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
    elif manifest == "episode-audit":
        episode = json.loads(Path(inputs["episode_trace"]).read_text(encoding="utf-8"))
        evidence_id = str(episode["events"][0]["evidence_id"])
        Path(outputs["audit_report"]).write_text(
            json.dumps({
                "edit_warranted": True,
                "failure_class": "instruction_missing",
                "severity": "medium",
                "confidence": 0.82,
                "evidence_refs": [evidence_id],
                "instruction_refs": ["CODEX.md#rules"],
            }, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
        Path(outputs["evidence_ids"]).write_text(
            json.dumps({"evidence_ids": [evidence_id]}, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
    elif manifest == "drift-detect":
        audit = json.loads(Path(inputs["audit_reports"]).read_text(encoding="utf-8"))
        evidence_refs = [str(ref) for ref in audit["evidence_refs"]]
        Path(outputs["drift_clusters"]).write_text(
            json.dumps({
                "clusters": [{
                    "cluster_id": "strict-drift-1",
                    "evidence_refs": evidence_refs,
                }],
            }, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
        Path(outputs["optimizer_notes"]).write_text(
            json.dumps({
                "notes": [{
                    "summary": "Strict subprocess proposal should use audited evidence.",
                    "evidence_refs": evidence_refs,
                }],
            }, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
    elif manifest == "patch-propose":
        drift = json.loads(Path(inputs["drift_clusters"]).read_text(encoding="utf-8"))
        evidence_id = str(drift["clusters"][0]["evidence_refs"][0])
        base = Path(outputs["candidate_patch"]).parents[3] / "CODEX.md"
        Path(outputs["proposal_rationale"]).write_text(
            json.dumps({
                "rationale": "Strict proposal is grounded in audited drift evidence.",
                "evidence_refs": [evidence_id],
                "style_constraints": ["Preserve concise instruction style."],
            }, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
        Path(outputs["candidate_patch"]).write_text(
            json.dumps({
                "base_file": "CODEX.md",
                "base_hash": hashlib.sha256(base.read_bytes()).hexdigest(),
                "diff": "--- a/CODEX.md\\n+++ b/CODEX.md\\n@@ -2,0 +3,1 @@\\n+Prefer regression tests for bug fixes.\\n",
                "risk_class": "instruction_clarification",
                "rationale": "strict external llmff proposed this from audited evidence",
                "expected_behavior_change": "Agents add regression tests before closing similar fixes.",
                "evals_required": ["governance-regression"],
                "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
                "sources": [{"source_id": evidence_id, "trusted": True}],
                "reflections": [{
                    "source_ref": evidence_id,
                    "summary": "Bug fix work lacked explicit regression-test guidance.",
                    "recurring_failure_patterns": ["Fixes closed without regression tests."],
                    "preserved_success_patterns": ["Keep short repo-local rules."],
                    "affected_instruction_chunks": ["CODEX.md#rules"],
                    "proposed_root_cause": "Regression-test expectations were implicit.",
                }],
                "bounded_edit_metadata": [{
                    "operator": "add",
                    "file": "CODEX.md",
                    "section": "Rules",
                    "changed_lines": 1,
                    "normative_changes": 0,
                }],
            }, sort_keys=True) + "\\n",
            encoding="utf-8",
        )
    log({
        "argv": argv,
        "command": "run",
        "manifest": manifest,
        "trace_path": str(trace_path),
        "events_path": str(events_path),
        "checkpoint_path": str(checkpoint_path),
        "inputs": inputs,
        "outputs": outputs,
    })
    raise SystemExit(0)

fail("unsupported argv: " + repr(argv))
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o755)
    return path
