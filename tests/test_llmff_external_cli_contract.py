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
    }[manifest]
    expected_outputs = {
        "instruction-index": ["instruction_index"],
        "episode-audit": ["audit_report", "evidence_ids"],
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
