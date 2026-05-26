from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tugboat.daemon.service import DaemonRunConfig, run_daemon_once
from tugboat.db import Store
from tugboat.mcp import (
    handle_jsonrpc_request,
    list_mcp_tools,
    tugboat_active_instructions,
    tugboat_candidate,
    tugboat_candidate_report,
    tugboat_harness_findings,
    tugboat_index_summary,
    tugboat_latest_audit,
    tugboat_instruction_graph,
    tugboat_latest_runs,
    tugboat_record_episode,
    tugboat_request_audit,
    tugboat_request_eval,
    tugboat_request_proposal,
    tugboat_run_report,
    tugboat_status,
)
from tugboat.paths import runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef


def _write_fake_audit_llmff(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:3] == ["inspect", "--format", "json"]:
    print(json.dumps({"manifest": Path(args[3]).stem, "network_required": False}))
    raise SystemExit(0)

if args[:1] == ["run"]:
    manifest = Path(args[1]).stem
    trace = Path(args[args.index("--trace") + 1])
    events = Path(args[args.index("--events") + 1])
    checkpoint = Path(args[args.index("--checkpoint") + 1])
    outputs = {}
    index = 0
    while index < len(args):
        if args[index] == "--output":
            outputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        index += 1
    trace.write_text('{"event":"step","name":"' + manifest + '"}\\n', encoding="utf-8")
    events.write_text('{"event":"run_completed"}\\n', encoding="utf-8")
    checkpoint.write_text('{"manifest_hash":"fake"}\\n', encoding="utf-8")
    if manifest == "instruction-index":
        outputs["instruction_index"].write_text(json.dumps({
            "documents": [{"path": "CODEX.md", "obligations": ["Use tests."]}]
        }) + "\\n", encoding="utf-8")
    elif manifest == "episode-audit":
        outputs["audit_report"].write_text(json.dumps({
            "edit_warranted": True,
            "failure_class": "instruction_conflict",
            "severity": "high",
            "confidence": 0.91,
            "evidence_refs": ["ev_mcp_daemon"],
        }) + "\\n", encoding="utf-8")
    elif manifest == "drift-detect":
        outputs["drift_clusters"].write_text(json.dumps({
            "clusters": [{"cluster_id": "drift-1", "evidence_refs": ["ev_mcp_daemon"]}]
        }) + "\\n", encoding="utf-8")
    elif manifest == "patch-propose":
        import hashlib
        repo = outputs["candidate_patch"].parents[3]
        base = repo / "CODEX.md"
        outputs["candidate_patch"].write_text(json.dumps({
            "base_file": "CODEX.md",
            "base_hash": hashlib.sha256(base.read_bytes()).hexdigest(),
            "diff": "--- a/CODEX.md\\n+++ b/CODEX.md\\n@@\\n+Add daemon proposed guidance.\\n",
            "risk_class": "instruction_clarification",
            "rationale": "daemon proposal from audited evidence",
            "expected_behavior_change": "Agents add daemon-reviewed guidance before closing fixes.",
            "evals_required": ["governance-regression"],
            "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
            "sources": [{"source_id": "ev_mcp_daemon", "trusted": True}],
            "bounded_edit_metadata": [{
                "operator": "add",
                "file": "CODEX.md",
                "section": "Rules",
                "changed_lines": 1,
                "normative_changes": 0
            }],
        }) + "\\n", encoding="utf-8")
    elif manifest == "patch-eval":
        outputs["eval_report"].write_text(json.dumps({
            "passed": True,
            "trigger_score": 0.72,
            "held_out_score": 0.91,
            "governance_passed": True,
            "recommendation": "accept",
            "metrics": {"governance_regressions": 0, "held_out_cases": 3},
        }) + "\\n", encoding="utf-8")
        outputs["policy_decision"].write_text(json.dumps({
            "allowed": True,
            "reasons": [],
        }) + "\\n", encoding="utf-8")
    raise SystemExit(0)

raise SystemExit(64)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_status_returns_read_only_summary_and_audits_call(tmp_path: Path):
    repo = tmp_path
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="audit",
            manifest_hash="manifest-hash",
            status="completed",
            run_dir=runs_dir(repo) / "run-1",
        )

    result = tugboat_status(repo)

    assert result == {
        "mode": "proposal_only",
        "auto_apply": "disabled",
        "indexed_documents": 0,
        "latest_run": {"run_id": "run-1", "stage": "audit", "status": "completed"},
        "pending_candidates": 0,
    }
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_status"


def test_mcp_call_rows_are_reachable_from_append_only_audit_event(tmp_path: Path):
    repo = tmp_path

    tugboat_status(repo)

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        row = store.connection.execute(
            """
            SELECT m.tool_name, m.status, a.event_type, a.payload_json
            FROM mcp_calls m
            JOIN audit_events a ON a.sequence = m.audit_event_sequence
            """
        ).fetchone()

    assert row is not None
    assert row[0] == "tugboat_status"
    assert row[1] == "completed"
    assert row[2] == "mcp.tool_called"
    assert json.loads(row[3])["tool"] == "tugboat_status"


def test_instruction_graph_returns_metadata_not_instruction_text(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text(
        "# Rules\n\nUse sk-thissecretkeyvalue1234567890 carefully.\n",
        encoding="utf-8",
    )

    result = tugboat_instruction_graph(repo)

    assert result["documents"] == [
        {
            "path": "CODEX.md",
            "kind": "agent_policy",
            "precedence": 70,
            "protected": True,
            "hash": result["documents"][0]["hash"],
            "parser_version": "markdown-heading-v1",
            "chunk_count": 1,
            "chunks": [
                {
                    "heading_path": ["Rules"],
                    "anchor": "rules",
                    "byte_start": result["documents"][0]["chunks"][0]["byte_start"],
                    "byte_end": result["documents"][0]["chunks"][0]["byte_end"],
                    "text_hash": result["documents"][0]["chunks"][0]["text_hash"],
                }
            ],
        }
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert "sk-thissecret" not in serialized
    assert "Use " not in serialized


def test_active_instructions_returns_ordered_refs_without_raw_text_and_audits(tmp_path: Path):
    repo = tmp_path
    (repo / "AGENTS.md").write_text(
        "# Repo Policy\n\nMUST keep private customer prompt alpha internal.\n",
        encoding="utf-8",
    )
    (repo / "CODEX.md").write_text(
        "# Agent Policy\n\nUse sk-thissecretkeyvalue1234567890 carefully.\n",
        encoding="utf-8",
    )

    result = tugboat_active_instructions(repo)

    agent_ref = result["documents"][0]["refs"][0]
    codex_ref = result["documents"][1]["refs"][0]
    assert [document["path"] for document in result["documents"]] == ["AGENTS.md", "CODEX.md"]
    assert result["documents"][0] == {
        "path": "AGENTS.md",
        "kind": "repo_policy",
        "precedence": 80,
        "protected": True,
        "active": True,
        "hash": result["documents"][0]["hash"],
        "chunk_count": 1,
        "refs": [agent_ref],
    }
    assert agent_ref.startswith("AGENTS.md#bytes-")
    assert result["documents"][1] == {
        "path": "CODEX.md",
        "kind": "agent_policy",
        "precedence": 70,
        "protected": True,
        "active": True,
        "hash": result["documents"][1]["hash"],
        "chunk_count": 1,
        "refs": [codex_ref],
    }
    assert codex_ref.startswith("CODEX.md#bytes-")
    serialized = json.dumps(result, sort_keys=True)
    assert "private customer prompt alpha" not in serialized
    assert "sk-thissecret" not in serialized
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_active_instructions"


def test_active_instructions_refs_do_not_leak_sensitive_heading_text(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text(
        "# Use sk-thissecretkeyvalue1234567890 carefully\n\nMUST test changes.\n",
        encoding="utf-8",
    )

    result = tugboat_active_instructions(repo)

    serialized = json.dumps(result, sort_keys=True)
    assert result["documents"][0]["refs"][0].startswith("CODEX.md#bytes-")
    assert "sk-thissecret" not in serialized
    assert "use-sk-thissecret" not in serialized


def test_index_summary_returns_counts_and_refs_without_instruction_text(tmp_path: Path):
    repo = tmp_path
    (repo / "AGENTS.md").write_text(
        "# Repo Policy\n\nMUST keep private customer prompt alpha internal.\n",
        encoding="utf-8",
    )
    (repo / "CODEX.md").write_text(
        "# Use sk-thissecretkeyvalue1234567890 carefully\n\nMUST test changes.\n",
        encoding="utf-8",
    )

    result = tugboat_index_summary(repo)

    assert result == {
        "indexed_documents": 2,
        "indexed_chunks": 2,
        "protected_documents": 2,
        "documents": [
            {
                "path": "AGENTS.md",
                "kind": "repo_policy",
                "precedence": 80,
                "protected": True,
                "hash": result["documents"][0]["hash"],
                "chunk_count": 1,
                "refs": [result["documents"][0]["refs"][0]],
            },
            {
                "path": "CODEX.md",
                "kind": "agent_policy",
                "precedence": 70,
                "protected": True,
                "hash": result["documents"][1]["hash"],
                "chunk_count": 1,
                "refs": [result["documents"][1]["refs"][0]],
            },
        ],
    }
    assert result["documents"][0]["refs"][0].startswith("AGENTS.md#bytes-")
    assert result["documents"][1]["refs"][0].startswith("CODEX.md#bytes-")
    serialized = json.dumps(result, sort_keys=True)
    assert "private customer prompt alpha" not in serialized
    assert "sk-thissecret" not in serialized
    assert "use-sk-thissecret" not in serialized
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_index_summary"


def test_harness_findings_are_plain_contract_and_audited(tmp_path: Path):
    repo = tmp_path
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [Missing](docs/MISSING.md).\n", encoding="utf-8")

    result = tugboat_harness_findings(repo)

    assert result == {
        "passed": False,
        "findings": ["AGENTS.md references missing repo-local markdown file docs/MISSING.md."],
    }
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_harness_findings"


def test_harness_findings_redact_raw_instruction_rule_text(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text(
        "# Rules\n\n"
        "MUST keep private customer prompt alpha internal.\n"
        "MUST keep private customer prompt alpha internal.\n"
        "See [Runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("# Runbook\n\nCurrent.\n", encoding="utf-8")

    result = tugboat_harness_findings(repo)

    serialized = json.dumps(result, sort_keys=True)
    assert result["passed"] is False
    assert "private customer prompt alpha" not in serialized
    assert "Duplicate instruction rule appears 2 times" in serialized
    assert "[REDACTED:harness_rule_text]" in serialized


def test_latest_runs_limits_results_and_returns_artifact_refs(tmp_path: Path):
    repo = tmp_path
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        for run_id in ("run-1", "run-2", "run-3"):
            run_dir = runs_dir(repo) / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "audit.json").write_text("{}\n", encoding="utf-8")
            if run_id == "run-3":
                (run_dir / "optimization-summary.json").write_text("{}\n", encoding="utf-8")
            store.insert_run(
                run_id=run_id,
                stage="audit",
                manifest_hash=f"hash-{run_id}",
                status="completed",
                run_dir=run_dir,
            )

    result = tugboat_latest_runs(repo, limit=2)

    assert [run["run_id"] for run in result["runs"]] == ["run-3", "run-2"]
    assert result["runs"][0]["artifacts"] == [
        {"kind": "audit", "path": ".sidecar/runs/run-3/audit.json"},
        {"kind": "optimization_summary", "path": ".sidecar/runs/run-3/optimization-summary.json"},
    ]


def test_latest_audit_returns_sanitized_newest_audit_summary_and_audits(tmp_path: Path):
    repo = tmp_path
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        for run_id, stage in (("run-1", "audit"), ("run-2", "audit"), ("run-3", "optimize")):
            run_dir = runs_dir(repo) / run_id
            run_dir.mkdir(parents=True)
            if stage == "audit":
                (run_dir / "audit.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "audit_id": 17 if run_id == "run-2" else 7,
                            "edit_warranted": True,
                            "evidence_refs": ["event:1", "sk-thissecretkeyvalue1234567890"],
                            "failure_class": "instruction_missing",
                            "severity": "medium",
                            "confidence": 0.75,
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                (run_dir / "audit.raw.json").write_text(
                    '{"model_payload":"sk-thissecretkeyvalue1234567890"}\n',
                    encoding="utf-8",
                )
            store.insert_run(
                run_id=run_id,
                stage=stage,
                manifest_hash=f"hash-{run_id}",
                status="completed",
                run_dir=run_dir,
            )

    result = tugboat_latest_audit(repo)

    assert result == {
        "audit": {
            "run": {"run_id": "run-2", "status": "completed"},
            "artifacts": [{"kind": "audit", "path": ".sidecar/runs/run-2/audit.json"}],
            "summary": {
                "audit_id": 17,
                "edit_warranted": True,
                "failure_class": "instruction_missing",
                "severity": "medium",
                "confidence": 0.75,
                "evidence_ref_count": 2,
            },
        }
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "audit.raw" not in serialized
    assert "sk-thissecret" not in serialized
    assert "run-3" not in serialized
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_latest_audit"


def test_latest_audit_tie_breaks_equal_created_at_by_updated_at(tmp_path: Path):
    repo = tmp_path
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        for run_id in ("run-1", "run-2"):
            run_dir = runs_dir(repo) / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "audit.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "audit_id": 1 if run_id == "run-1" else 2,
                        "edit_warranted": True,
                        "evidence_refs": [],
                        "failure_class": "instruction_missing",
                        "severity": "medium",
                        "confidence": 0.75,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            store.insert_run(
                run_id=run_id,
                stage="audit",
                manifest_hash=f"hash-{run_id}",
                status="completed",
                run_dir=run_dir,
            )
        store.connection.execute(
            "UPDATE runs SET created_at = '2026-05-26T00:00:00+00:00' WHERE id IN ('run-1', 'run-2')"
        )
        store.connection.execute(
            "UPDATE runs SET updated_at = '2026-05-26T00:00:02+00:00' WHERE id = 'run-1'"
        )
        store.connection.execute(
            "UPDATE runs SET updated_at = '2026-05-26T00:00:01+00:00' WHERE id = 'run-2'"
        )
        store.connection.commit()

    result = tugboat_latest_audit(repo)

    assert result["audit"]["run"]["run_id"] == "run-1"
    assert result["audit"]["summary"]["audit_id"] == 1


def test_run_report_summarizes_known_artifacts_without_raw_payloads(tmp_path: Path):
    repo = tmp_path
    run_dir = runs_dir(repo) / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "trace-input.jsonl").write_text('{"prompt":"sk-thissecretkeyvalue1234567890"}\n', encoding="utf-8")
    (run_dir / "audit.raw.json").write_text('{"model_payload":"sk-thissecretkeyvalue1234567890"}\n', encoding="utf-8")
    (run_dir / "audit.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audit_id": 7,
                "edit_warranted": True,
                "evidence_refs": ["event:1"],
                "failure_class": "instruction_missing",
                "severity": "medium",
                "confidence": 0.75,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="audit",
            manifest_hash="hash",
            status="completed",
            run_dir=run_dir,
        )

    result = tugboat_run_report(repo, "run-1")

    assert result["run"] == {"run_id": "run-1", "stage": "audit", "status": "completed"}
    assert result["artifacts"] == [{"kind": "audit", "path": ".sidecar/runs/run-1/audit.json"}]
    assert result["audit"] == {
        "audit_id": 7,
        "edit_warranted": True,
        "failure_class": "instruction_missing",
        "severity": "medium",
        "confidence": 0.75,
        "evidence_ref_count": 1,
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "trace-input" not in serialized
    assert "audit.raw" not in serialized
    assert "sk-thissecret" not in serialized


def test_run_report_exposes_optimization_summary_artifact_ref(tmp_path: Path):
    repo = tmp_path
    run_dir = runs_dir(repo) / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "optimization-summary.json").write_text(
        json.dumps(
            {
                "audit_run": "run-1",
                "candidate_id": 1,
                "decision": "needs_review",
                "held_out_score": 0.9,
                "recommendation": "accept",
                "suite_id": "all",
                "trigger_score": 0.5,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="optimize",
            manifest_hash="hash",
            status="completed",
            run_dir=run_dir,
        )

    result = tugboat_run_report(repo, "run-1")

    assert result["artifacts"] == [
        {"kind": "optimization_summary", "path": ".sidecar/runs/run-1/optimization-summary.json"}
    ]


def test_candidate_returns_summary_and_diff_ref_without_raw_diff(tmp_path: Path):
    repo = tmp_path
    run_dir = runs_dir(repo) / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n", encoding="utf-8")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=["event:1"],
            instruction_refs=["CODEX.md"],
        )
        candidate = CandidatePatch(
            audit_id=audit_id,
            base_file="CODEX.md",
            base_hash=CandidatePatch.hash_file(repo / "CODEX.md"),
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+sk-thissecretkeyvalue1234567890\n",
            risk_class="instruction_clarification",
            rationale="mentions sk-thissecretkeyvalue1234567890",
            sources=(SourceRef("audit:latest", trusted=True),),
        )
        diff_path = run_dir / "candidate.diff"
        diff_path.write_text(candidate.diff, encoding="utf-8")
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=candidate,
            diff_path=diff_path,
            state="needs_review",
        )

    result = tugboat_candidate(repo, candidate_id)

    assert result == {
        "candidate_id": candidate_id,
        "audit_id": audit_id,
        "base_file": "CODEX.md",
        "risk_class": "instruction_clarification",
        "state": "needs_review",
        "rationale_summary": "mentions [REDACTED:openai_api_key]",
        "artifacts": [{"kind": "candidate_diff", "path": ".sidecar/runs/run-1/candidate.diff"}],
    }
    assert "sk-thissecret" not in json.dumps(result, sort_keys=True)


def test_candidate_report_returns_eval_and_decision_refs_without_raw_payloads(tmp_path: Path):
    repo = tmp_path
    run_dir = runs_dir(repo) / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n", encoding="utf-8")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=["event:1"],
            instruction_refs=["CODEX.md"],
        )
        candidate = CandidatePatch(
            audit_id=audit_id,
            base_file="CODEX.md",
            base_hash=CandidatePatch.hash_file(repo / "CODEX.md"),
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+sk-thissecretkeyvalue1234567890\n",
            risk_class="instruction_clarification",
            rationale="mentions sk-thissecretkeyvalue1234567890",
            sources=(SourceRef("audit:latest", trusted=True),),
        )
        diff_path = run_dir / "candidate.diff"
        diff_path.write_text(candidate.diff, encoding="utf-8")
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=candidate,
            diff_path=diff_path,
            state="needs_review",
        )
        eval_report = run_dir / "eval-report.json"
        eval_report.write_text(
            '{"model_payload":"sk-thissecretkeyvalue1234567890","passed":true}\n',
            encoding="utf-8",
        )
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id="all",
            report_path=eval_report,
            passed=True,
            metrics={"held_out_score": 0.91, "secret_note": "sk-thissecretkeyvalue1234567890"},
        )
        store.insert_decision(
            candidate_id=candidate_id,
            actor="reviewer",
            policy="proposal_only",
            decision="needs_review",
            reason="policy passed for sk-thissecretkeyvalue1234567890",
        )

    result = tugboat_candidate_report(repo, candidate_id)

    assert result == {
        "candidate": {
            "candidate_id": candidate_id,
            "audit_id": audit_id,
            "base_file": "CODEX.md",
            "risk_class": "instruction_clarification",
            "state": "needs_review",
            "rationale_summary": "mentions [REDACTED:openai_api_key]",
        },
        "latest_eval": {
            "suite_id": "all",
            "passed": True,
            "artifact": {"kind": "eval_report", "path": ".sidecar/runs/run-1/eval-report.json"},
        },
        "latest_decision": {
            "actor": "reviewer",
            "policy": "proposal_only",
            "decision": "needs_review",
            "reason_summary": "policy passed for [REDACTED:openai_api_key]",
        },
        "artifacts": [{"kind": "candidate_diff", "path": ".sidecar/runs/run-1/candidate.diff"}],
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "model_payload" not in serialized
    assert "secret_note" not in serialized
    assert "sk-thissecret" not in serialized
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_candidate_report"


def test_candidate_report_uses_latest_eval_and_decision_rows(tmp_path: Path):
    repo = tmp_path
    run_dir = runs_dir(repo) / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n", encoding="utf-8")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=["event:1"],
            instruction_refs=["CODEX.md"],
        )
        candidate = CandidatePatch(
            audit_id=audit_id,
            base_file="CODEX.md",
            base_hash=CandidatePatch.hash_file(repo / "CODEX.md"),
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use tests.\n",
            risk_class="instruction_clarification",
            rationale="clarify testing",
            sources=(SourceRef("audit:latest", trusted=True),),
        )
        diff_path = run_dir / "candidate.diff"
        diff_path.write_text(candidate.diff, encoding="utf-8")
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=candidate,
            diff_path=diff_path,
            state="needs_review",
        )
        old_eval = run_dir / "eval-old.json"
        new_eval = run_dir / "eval-new.json"
        old_eval.write_text("{}\n", encoding="utf-8")
        new_eval.write_text("{}\n", encoding="utf-8")
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id="smoke",
            report_path=old_eval,
            passed=False,
            metrics={},
        )
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id="all",
            report_path=new_eval,
            passed=True,
            metrics={},
        )
        store.insert_decision(
            candidate_id=candidate_id,
            actor="old-reviewer",
            policy="proposal_only",
            decision="rejected",
            reason="old decision",
        )
        store.insert_decision(
            candidate_id=candidate_id,
            actor="new-reviewer",
            policy="proposal_only",
            decision="needs_review",
            reason="new decision",
        )

    result = tugboat_candidate_report(repo, candidate_id)

    assert result["latest_eval"] == {
        "suite_id": "all",
        "passed": True,
        "artifact": {"kind": "eval_report", "path": ".sidecar/runs/run-1/eval-new.json"},
    }
    assert result["latest_decision"] == {
        "actor": "new-reviewer",
        "policy": "proposal_only",
        "decision": "needs_review",
        "reason_summary": "new decision",
    }


def test_repo_must_be_local_path():
    with pytest.raises(ValueError, match="local repo path"):
        tugboat_status("https://example.com/repo.git")


def test_mcp_repo_allowlist_blocks_unlisted_repo_and_audits_denial(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
mcp:
  allowed_repositories:
    - /some/other/repo
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not allowed for MCP"):
        tugboat_status(repo)

    event = _mcp_events(repo)[-1]
    assert event["tool"] == "tugboat_status"
    assert event["status"] == "denied"


def test_mcp_per_tool_policy_blocks_denied_tool_and_audits_denial(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {tmp_path.resolve().as_posix()}
  tool_policy:
    tugboat_status: deny
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="MCP tool denied by policy"):
        tugboat_status(tmp_path)

    event = _mcp_events(tmp_path)[-1]
    assert event["tool"] == "tugboat_status"
    assert event["status"] == "denied"


def test_write_intent_tools_create_request_artifacts_without_mutating_instructions(tmp_path: Path):
    repo = tmp_path
    codex = repo / "CODEX.md"
    original = "# Rules\n\nUse tests.\n"
    codex.write_text(original, encoding="utf-8")

    episode = tugboat_record_episode(
        repo,
        '{"type":"user_request","text":"Fix bug"}\n'
        '{"type":"user_correction","text":"Need regression test"}\n',
    )
    audit_request = tugboat_request_audit(repo, episode["trace_id"])
    proposal_request = tugboat_request_proposal(repo, "audit-7")
    eval_request = tugboat_request_eval(repo, "candidate-9", "all")

    assert episode["trace_id"].startswith("mcp-trace-")
    assert episode["artifact_ref"].startswith(".sidecar/mcp/episodes/")
    assert audit_request == {
        "request_id": audit_request["request_id"],
        "kind": "audit",
        "state": "queued",
        "write_intent": True,
        "repo_policy": audit_request["repo_policy"],
        "artifact_ref": audit_request["artifact_ref"],
    }
    assert proposal_request["kind"] == "proposal"
    assert eval_request["kind"] == "eval"
    assert json.loads((repo / eval_request["artifact_ref"]).read_text(encoding="utf-8"))[
        "candidate_id"
    ] == "candidate-9"
    assert codex.read_text(encoding="utf-8") == original
    assert [event["tool"] for event in _mcp_events(repo)[-4:]] == [
        "tugboat_record_episode",
        "tugboat_request_audit",
        "tugboat_request_proposal",
        "tugboat_request_eval",
    ]


def test_request_audit_enqueues_daemon_executable_trace_audit(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    fake_llmff = _write_fake_audit_llmff(repo / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )
    episode = tugboat_record_episode(
        repo,
        '{"type":"user_request","content":"Fix bug"}\n'
        '{"type":"user_correction","content":"Add regression tests"}\n',
    )

    request = tugboat_request_audit(repo, episode["trace_id"])
    result = run_daemon_once(
        repo,
        DaemonRunConfig(
            worker_id="mcp-worker",
            lease_duration=timedelta(seconds=30),
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )

    assert request["kind"] == "audit"
    assert result["processed"] is True
    assert result["final_state"] == "waiting_review"
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "instruction_conflict"
    assert audit["evidence_refs"] == ["ev_mcp_daemon"]
    assert (run_dir / "audit.raw.json").exists()
    with Store.open(repo / ".sidecar" / "daemon.sqlite") as queue_store:
        queued = queue_store.connection.execute(
            "SELECT kind, payload_json FROM daemon_jobs"
        ).fetchone()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        jobs = store.connection.execute(
            """
            SELECT manifest_name, status
            FROM llmff_jobs
            ORDER BY id
            """
        ).fetchall()
    assert queued[0] == "trace_audit"
    assert jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
    ]
    queued_payload = json.loads(queued[1])
    assert queued_payload["trace_path"] == str(repo / episode["artifact_ref"])
    assert queued_payload["artifact_ref"].startswith(".sidecar/mcp/requests/")


def test_request_proposal_enqueues_daemon_executable_patch_propose(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    fake_llmff = _write_fake_audit_llmff(repo / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )
    episode = tugboat_record_episode(
        repo,
        '{"type":"user_request","content":"Fix bug"}\n'
        '{"type":"user_correction","content":"Add regression tests"}\n',
    )
    tugboat_request_audit(repo, episode["trace_id"])
    audit_result = run_daemon_once(
        repo,
        DaemonRunConfig(
            worker_id="mcp-worker",
            lease_duration=timedelta(seconds=30),
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )
    assert audit_result["final_state"] == "waiting_review"
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        audit_id = int(store.connection.execute("SELECT id FROM audits").fetchone()[0])

    request = tugboat_request_proposal(repo, str(audit_id))
    proposal_result = run_daemon_once(
        repo,
        DaemonRunConfig(
            worker_id="mcp-worker",
            lease_duration=timedelta(seconds=30),
            now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        ),
    )

    assert request["kind"] == "proposal"
    assert proposal_result["processed"] is True
    assert proposal_result["final_state"] == "waiting_review"
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    assert candidate["audit_id"] == audit_id
    assert candidate["rationale"] == "daemon proposal from audited evidence"
    assert (run_dir / "candidate.raw.json").exists()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        jobs = store.connection.execute(
            """
            SELECT manifest_name, status
            FROM llmff_jobs
            ORDER BY id
            """
        ).fetchall()
    assert jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
        ("drift-detect.yaml", "completed"),
        ("patch-propose.yaml", "completed"),
    ]


def test_request_eval_enqueues_daemon_executable_patch_eval(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    fake_llmff = _write_fake_audit_llmff(repo / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )
    episode = tugboat_record_episode(
        repo,
        '{"type":"user_request","content":"Fix bug"}\n'
        '{"type":"user_correction","content":"Add regression tests"}\n',
    )
    tugboat_request_audit(repo, episode["trace_id"])
    assert run_daemon_once(
        repo,
        DaemonRunConfig(
            worker_id="mcp-worker",
            lease_duration=timedelta(seconds=30),
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )["final_state"] == "waiting_review"
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        audit_id = int(store.connection.execute("SELECT id FROM audits").fetchone()[0])
    tugboat_request_proposal(repo, str(audit_id))
    assert run_daemon_once(
        repo,
        DaemonRunConfig(
            worker_id="mcp-worker",
            lease_duration=timedelta(seconds=30),
            now=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
        ),
    )["final_state"] == "waiting_review"
    run_dir = sorted((repo / ".sidecar" / "runs").iterdir())[-1]
    candidate_id = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))[
        "candidate_id"
    ]

    request = tugboat_request_eval(repo, str(candidate_id), "all")
    eval_result = run_daemon_once(
        repo,
        DaemonRunConfig(
            worker_id="mcp-worker",
            lease_duration=timedelta(seconds=30),
            now=datetime(2026, 1, 1, 0, 2, tzinfo=timezone.utc),
        ),
    )

    assert request["kind"] == "eval"
    assert eval_result["processed"] is True
    assert eval_result["final_state"] == "waiting_review"
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    policy_gate = json.loads((run_dir / "policy-gate.json").read_text(encoding="utf-8"))
    assert eval_report["candidate_id"] == candidate_id
    assert eval_report["suite_id"] == "all"
    assert eval_report["passed"] is True
    assert eval_report["held_out_score"] == 0.91
    assert policy_gate == {"schema_version": 1, "allowed": True, "reasons": []}
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        jobs = store.connection.execute(
            """
            SELECT manifest_name, status
            FROM llmff_jobs
            ORDER BY id
            """
        ).fetchall()
        eval_run = store.connection.execute(
            "SELECT candidate_id, suite_id, status FROM eval_runs"
        ).fetchone()
    assert jobs == [
        ("instruction-index.yaml", "completed"),
        ("episode-audit.yaml", "completed"),
        ("drift-detect.yaml", "completed"),
        ("patch-propose.yaml", "completed"),
        ("patch-eval.yaml", "completed"),
    ]
    assert eval_run == (candidate_id, "all", "passed")


def test_request_audit_records_policy_tied_write_intent_without_mutating_instructions(
    tmp_path: Path,
):
    repo = tmp_path
    codex = repo / "CODEX.md"
    original = "# Rules\n\nUse tests before changing instructions.\n"
    codex.write_text(original, encoding="utf-8")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    policy_text = f"""
version: 7
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
  tool_policy:
    tugboat_request_audit: allow
    """.lstrip()
    (policy_dir / "policy.yaml").write_text(policy_text, encoding="utf-8")

    episode = tugboat_record_episode(repo, '{"type":"user_request","content":"Fix"}\n')
    result = tugboat_request_audit(repo, episode["trace_id"])

    artifact = json.loads((repo / result["artifact_ref"]).read_text(encoding="utf-8"))
    assert result["state"] == "queued"
    assert artifact == {
        "request_id": result["request_id"],
        "kind": "audit",
        "state": "queued",
        "write_intent": True,
        "trace_id": episode["trace_id"],
        "repo_policy": {
            "path": ".sidecar/policy.yaml",
            "version": 7,
            "hash": hashlib.sha256(policy_text.encode("utf-8")).hexdigest(),
        },
    }
    assert codex.read_text(encoding="utf-8") == original

    event = _mcp_events(repo)[-1]
    assert event["tool"] == "tugboat_request_audit"
    assert event["status"] == "completed"
    assert event["write_intent"] is True
    assert event["request"] == {
        "request_id": result["request_id"],
        "kind": "audit",
        "state": "queued",
        "artifact_ref": result["artifact_ref"],
        "repo_policy": artifact["repo_policy"],
    }


def test_write_intent_episode_rejects_secret_payloads(tmp_path: Path):
    with pytest.raises(ValueError, match="secret"):
        tugboat_record_episode(
            tmp_path,
            '{"type":"user_request","text":"sk-thissecretkeyvalue1234567890"}\n',
        )


def test_mcp_jsonrpc_lists_and_invokes_tools(tmp_path: Path):
    repo = tmp_path
    tools = list_mcp_tools()

    by_name = {tool["name"]: tool for tool in tools}
    assert "tugboat_active_instructions" in by_name
    assert "tugboat_candidate_report" in by_name
    assert "tugboat_index_summary" in by_name
    assert "tugboat_latest_audit" in by_name
    assert "tugboat_status" in by_name
    assert "tugboat_request_audit" in by_name
    assert by_name["tugboat_active_instructions"] == {
        "name": "tugboat_active_instructions",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_status"] == {
        "name": "tugboat_status",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_index_summary"] == {
        "name": "tugboat_index_summary",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_candidate_report"] == {
        "name": "tugboat_candidate_report",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_latest_audit"] == {
        "name": "tugboat_latest_audit",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_request_audit"] == {
        "name": "tugboat_request_audit",
        "mutates_instructions": False,
        "write_intent": True,
    }
    assert by_name["tugboat_record_episode"]["write_intent"] is True
    assert all(tool["mutates_instructions"] is False for tool in tools)
    assert handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {},
        }
    ) == {"jsonrpc": "2.0", "id": 1, "result": {"tools": tools}}
    response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "tugboat_status",
                "arguments": {"repo": str(repo)},
            },
        }
    )

    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 2
    assert response["result"]["content"][0]["type"] == "json"
    assert response["result"]["content"][0]["json"]["mode"] == "proposal_only"

    latest_audit_response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "tugboat_latest_audit",
                "arguments": {"repo": str(repo)},
            },
        }
    )

    assert latest_audit_response == {
        "jsonrpc": "2.0",
        "id": 3,
        "result": {"content": [{"type": "json", "json": {"audit": None}}]},
    }

    index_summary_response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "tugboat_index_summary",
                "arguments": {"repo": str(repo)},
            },
        }
    )

    assert index_summary_response["jsonrpc"] == "2.0"
    assert index_summary_response["id"] == 4
    assert index_summary_response["result"]["content"][0]["json"]["indexed_documents"] == 0


def test_mcp_jsonrpc_rejects_unknown_or_apply_tools(tmp_path: Path):
    response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "tugboat_apply", "arguments": {"repo": str(tmp_path)}},
        }
    )

    assert response["error"]["code"] == -32601
    assert "unknown MCP tool" in response["error"]["message"]


def test_mcp_jsonrpc_redacts_secret_bearing_error_messages(tmp_path: Path):
    response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "tugboat_request_audit",
                "arguments": {
                    "repo": str(tmp_path),
                    "trace_id": "sk-thissecretkeyvalue1234567890",
                },
            },
        }
    )

    assert response["error"]["code"] == -32000
    assert "sk-thissecret" not in response["error"]["message"]
    assert "[REDACTED:openai_api_key]" in response["error"]["message"]


def _mcp_events(repo: Path) -> list[dict[str, object]]:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        rows = store.connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = 'mcp.tool_called' ORDER BY sequence"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]
