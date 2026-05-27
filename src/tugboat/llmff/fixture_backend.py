from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tugboat-fixture-llmff")
    subcommands = parser.add_subparsers(dest="command", required=True)
    inspect = subcommands.add_parser("inspect")
    inspect.add_argument("--format", choices=("json",), required=True)
    inspect.add_argument("manifest")
    run = subcommands.add_parser("run")
    run.add_argument("manifest")
    run.add_argument("--trace", required=True)
    run.add_argument("--events", required=True)
    run.add_argument("--checkpoint", required=True)
    run.add_argument("--timeout-ms", required=True)
    run.add_argument("--retry-attempts", required=True)
    run.add_argument("--retry-backoff-ms", required=True)
    run.add_argument("--input", nargs=2, action="append", default=[])
    run.add_argument("--output", nargs=2, action="append", default=[])
    args = parser.parse_args(argv)

    if args.command == "inspect":
        print(
            json.dumps(
                {
                    "manifest": Path(args.manifest).stem,
                    "network_required": False,
                    "providers": [],
                    "external_calls": [],
                    "fixture_backend": "tugboat",
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "run":
        return _run_manifest(args)
    return 64


def _run_manifest(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    manifest = manifest_path.stem
    trace_path = Path(args.trace)
    events_path = Path(args.events)
    checkpoint_path = Path(args.checkpoint)
    inputs = {name: Path(path) for name, path in args.input}
    outputs = {name: Path(path) for name, path in args.output}
    _write_text(trace_path, json.dumps({"event": "step", "manifest": manifest}) + "\n")
    _write_text(events_path, json.dumps({"event": "run_completed", "manifest": manifest}) + "\n")
    _write_json(
        checkpoint_path,
        {"manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest()},
    )

    if manifest == "instruction-index":
        _write_json(
            outputs["instruction_index"],
            {
                "documents": [
                    {
                        "path": "CODEX.md",
                        "obligations": ["Use tests."],
                        "chunks": [
                            {
                                "ref": "CODEX.md#rules",
                                "anchor": "rules",
                                "heading_path": ["Rules"],
                            }
                        ],
                    }
                ]
            },
        )
    elif manifest == "episode-audit":
        episode = _read_json_object(inputs["episode_trace"])
        evidence_id = _episode_evidence_id(episode)
        _write_json(
            outputs["audit_report"],
            {
                "edit_warranted": True,
                "failure_class": "instruction_missing",
                "severity": "medium",
                "confidence": 0.82,
                "evidence_refs": [evidence_id],
                "instruction_refs": ["CODEX.md#rules"],
            },
        )
        _write_json(outputs["evidence_ids"], {"evidence_ids": [evidence_id]})
    elif manifest == "drift-detect":
        audit = _read_json_object(inputs["audit_reports"])
        evidence_refs = audit["evidence_refs"]
        _write_json(
            outputs["drift_clusters"],
            {"clusters": [{"cluster_id": "drift-1", "evidence_refs": evidence_refs}]},
        )
        if "optimizer_notes" in outputs:
            _write_json(
                outputs["optimizer_notes"],
                {
                    "notes": [
                        {
                            "summary": "Use drift evidence for the proposal.",
                            "evidence_refs": evidence_refs,
                        }
                    ]
                },
            )
    elif manifest == "patch-propose":
        repo = outputs["candidate_patch"].parents[3]
        base = repo / "CODEX.md"
        drift = _read_json_object(inputs["drift_clusters"])
        evidence_refs = drift["clusters"][0]["evidence_refs"]
        if "proposal_rationale" in outputs:
            _write_json(
                outputs["proposal_rationale"],
                {
                    "rationale": "Patch proposal is grounded in local fixture evidence.",
                    "evidence_refs": evidence_refs,
                    "style_constraints": ["Preserve concise instruction style."],
                },
            )
        _write_json(
            outputs["candidate_patch"],
            {
                "base_file": "CODEX.md",
                "base_hash": hashlib.sha256(base.read_bytes()).hexdigest(),
                "diff": (
                    "--- a/CODEX.md\n"
                    "+++ b/CODEX.md\n"
                    "@@ -1,0 +1,1 @@\n"
                    "+Add regression-test guidance.\n"
                ),
                "risk_class": "instruction_clarification",
                "rationale": "fixture backend proposed this from audited evidence",
                "expected_behavior_change": (
                    "Agents add regression-test guidance before closing fixes."
                ),
                "evals_required": ["governance-regression"],
                "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
                "sources": [{"source_id": evidence_refs[0], "trusted": True}],
                "bounded_edit_metadata": [
                    {
                        "operator": "add",
                        "file": "CODEX.md",
                        "section": "Rules",
                        "changed_lines": 1,
                        "normative_changes": 0,
                    }
                ],
            },
        )
    elif manifest == "patch-eval":
        _write_json(
            outputs["eval_report"],
            {
                "passed": True,
                "trigger_score": 0.75,
                "held_out_score": 0.88,
                "governance_passed": True,
                "recommendation": "accept",
                "metrics": {"governance_regressions": 0, "held_out_cases": 3},
                "validation_splits": {
                    "trigger": ["trigger:fixture-regression"],
                    "held_out": ["held-out:fixture-no-regression"],
                    "governance": ["governance:fixture-policy"],
                },
            },
        )
        _write_json(outputs["policy_decision"], {"allowed": True, "reasons": []})
    elif manifest == "acceptance-summary":
        _write_json(
            outputs["acceptance_summary"],
            {
                "decision_recommendation": "needs_review",
                "reasons": ["policy gate and eval report passed"],
                "evidence": ["audit:1"],
                "reviewer_checklist": ["Review candidate diff", "Confirm rollback command"],
                "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
            },
        )
    else:
        return 64
    return 0


def _episode_evidence_id(episode: dict[str, Any]) -> str:
    events = episode.get("events", [])
    if not isinstance(events, list) or not events:
        return "ev_fixture"
    for event in events:
        if isinstance(event, dict) and event.get("event_type") == "user_correction":
            evidence_id = event.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id:
                return evidence_id
    first_event = events[0]
    if isinstance(first_event, dict) and isinstance(first_event.get("evidence_id"), str):
        return str(first_event["evidence_id"])
    return "ev_fixture"


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"fixture input must be a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_text(path, json.dumps(payload, sort_keys=True) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def console_main() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    console_main()
