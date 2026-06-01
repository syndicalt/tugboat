from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from stat import S_IMODE

import pytest

import tugboat.mcp.contracts as mcp_contracts
from tugboat.daemon.queue import DaemonQueue
from tugboat.daemon.service import DaemonRunConfig, run_daemon_once
from tugboat.db import Store
from tugboat.llmff.contracts import RunResult
from tugboat.mcp import (
    handle_jsonrpc_request,
    list_mcp_tools,
    run_stdio_server,
    tugboat_active_instructions,
    tugboat_auto_update_status,
    tugboat_candidate,
    tugboat_candidate_report,
    tugboat_decision_trace,
    tugboat_harness_findings,
    tugboat_harness_health,
    tugboat_index_summary,
    tugboat_latest_audit,
    tugboat_instruction_graph,
    tugboat_latest_failed_gates,
    tugboat_latest_runs,
    tugboat_record_episode,
    tugboat_request_audit,
    tugboat_request_eval,
    tugboat_request_optimization,
    tugboat_request_proposal,
    tugboat_recent_decisions,
    tugboat_run_report,
    tugboat_status,
)
from tugboat.paths import runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef


def _mode(path: Path) -> int:
    return S_IMODE(path.stat().st_mode)


def _write_fake_audit_llmff(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import hashlib
import sys
from pathlib import Path

args = sys.argv[1:]
if args[:3] == ["inspect", "--format", "json"]:
    print(json.dumps({
        "manifest": Path(args[3]).stem,
        "network_required": False,
        "providers": [],
        "external_calls": [],
    }))
    raise SystemExit(0)

if args[:1] == ["run"]:
    manifest = Path(args[1]).stem
    trace = Path(args[args.index("--trace") + 1])
    events = Path(args[args.index("--events") + 1])
    checkpoint = Path(args[args.index("--checkpoint") + 1])
    outputs = {}
    inputs = {}
    index = 0
    while index < len(args):
        if args[index] == "--input":
            inputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        if args[index] == "--output":
            outputs[args[index + 1]] = Path(args[index + 2])
            index += 3
            continue
        index += 1
    output_dir = next(iter(outputs.values())).parent if outputs else Path(".")
    canonical_episode = inputs.get("episode_trace", output_dir / "canonical-episode.json")
    evidence_id = "ev_mcp_daemon"
    if canonical_episode.exists():
        canonical = json.loads(canonical_episode.read_text(encoding="utf-8"))
        if canonical.get("events"):
            evidence_id = str(canonical["events"][0]["evidence_id"])
    trace.write_text('{"event":"step","name":"' + manifest + '"}\\n', encoding="utf-8")
    events.write_text('{"event":"run_completed"}\\n', encoding="utf-8")
    checkpoint.write_text(
        json.dumps({"manifest_hash": hashlib.sha256(Path(args[1]).read_bytes()).hexdigest()}) + "\\n",
        encoding="utf-8",
    )
    if manifest == "instruction-index":
        outputs["instruction_index"].write_text(json.dumps({
            "documents": [{
                "path": "CODEX.md",
                "obligations": ["Use tests."],
                "chunks": [{
                    "ref": "CODEX.md#rules",
                    "anchor": "rules",
                    "heading_path": ["Rules"],
                }],
            }]
        }) + "\\n", encoding="utf-8")
    elif manifest == "episode-audit":
        outputs["audit_report"].write_text(json.dumps({
            "edit_warranted": True,
            "failure_class": "instruction_conflict",
            "severity": "high",
            "confidence": 0.91,
            "evidence_refs": [evidence_id],
            "instruction_refs": ["CODEX.md#rules"],
        }) + "\\n", encoding="utf-8")
        outputs["evidence_ids"].write_text(json.dumps({
            "evidence_ids": [evidence_id],
        }) + "\\n", encoding="utf-8")
    elif manifest == "drift-detect":
        outputs["drift_clusters"].write_text(json.dumps({
            "clusters": [{"cluster_id": "drift-1", "evidence_refs": [evidence_id]}]
        }) + "\\n", encoding="utf-8")
        if "optimizer_notes" in outputs:
            outputs["optimizer_notes"].write_text(json.dumps({
                "notes": [
                    {
                        "summary": "Use daemon audit drift evidence for the proposal.",
                        "evidence_refs": [evidence_id],
                    }
                ]
            }) + "\\n", encoding="utf-8")
    elif manifest == "patch-propose":
        import hashlib
        repo = outputs["candidate_patch"].parents[3]
        base = repo / "CODEX.md"
        if "proposal_rationale" in outputs:
            outputs["proposal_rationale"].write_text(json.dumps({
                "rationale": "Patch proposal is grounded in daemon audit evidence.",
                "evidence_refs": [evidence_id],
                "style_constraints": ["Preserve concise instruction style."],
            }) + "\\n", encoding="utf-8")
        outputs["candidate_patch"].write_text(json.dumps({
            "base_file": "CODEX.md",
            "base_hash": hashlib.sha256(base.read_bytes()).hexdigest(),
            "diff": "--- a/CODEX.md\\n+++ b/CODEX.md\\n@@ -1,0 +1,1 @@\\n+Add daemon proposed guidance.\\n",
            "risk_class": "instruction_clarification",
            "rationale": "daemon proposal from audited evidence",
            "expected_behavior_change": "Agents add daemon-reviewed guidance before closing fixes.",
            "evals_required": ["governance-regression"],
            "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
            "sources": [{"source_id": evidence_id, "trusted": True}],
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
            "metrics": {
                "governance_regressions": 0,
                "held_out_cases": 3,
                "incident_replay_cases": 1,
            },
            "validation_splits": {
                "trigger": ["incident_replay:mcp-regression"],
                "held_out": ["held-out:mcp-no-regression"],
                "governance": ["governance:mcp-policy"],
            },
            "eval_cases": [
                {
                    "case_id": "incident_replay:mcp-regression",
                    "case_hash": "a" * 64,
                    "split_name": "trigger",
                },
                {
                    "case_id": "held-out:mcp-no-regression",
                    "case_hash": "b" * 64,
                    "split_name": "held_out",
                },
                {
                    "case_id": "governance:mcp-policy",
                    "case_hash": "c" * 64,
                    "split_name": "governance",
                },
            ],
        }) + "\\n", encoding="utf-8")
        outputs["policy_decision"].write_text(json.dumps({
            "allowed": True,
            "reasons": [],
        }) + "\\n", encoding="utf-8")
    elif manifest == "acceptance-summary":
        outputs["acceptance_summary"].write_text(json.dumps({
            "decision_recommendation": "needs_review",
            "reasons": ["held-out score improved"],
            "evidence": ["eval-report.json: held_out_score improved"],
            "reviewer_checklist": [
                "Review candidate diff and proposal rationale against trace evidence.",
                "Confirm risk classification matches the bounded edit.",
                "Verify source evidence supports the recommendation.",
                "Confirm expected behavior change is narrow and intentional.",
                "Confirm rollback command before applying.",
            ],
            "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
        }) + "\\n", encoding="utf-8")
    raise SystemExit(0)

raise SystemExit(64)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _seed_review_target(repo: Path) -> tuple[int, int]:
    codex = repo / "CODEX.md"
    if not codex.exists():
        codex.write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    run_dir = sidecar_dir(repo) / "runs" / "seed-review-target"
    run_dir.mkdir(parents=True, exist_ok=True)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id="seed-review-target",
            stage="proposal",
            manifest_hash="fixture-manifest",
            status="completed",
            run_dir=run_dir,
        )
        audit_id = store.insert_audit(
            run_id="seed-review-target",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=["event:1"],
            instruction_refs=["CODEX.md"],
        )
        candidate = CandidatePatch(
            audit_id=audit_id,
            base_file="CODEX.md",
            base_hash=CandidatePatch.hash_file(codex),
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,0 +1,1 @@\n+Use regression tests.\n",
            risk_class="instruction_clarification",
            rationale="seeded candidate",
            sources=(SourceRef("event:1", trusted=True),),
        )
        diff_path = run_dir / "candidate.diff"
        diff_path.write_text(candidate.diff, encoding="utf-8")
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=candidate,
            diff_path=diff_path,
            state="needs_review",
        )
    return audit_id, candidate_id


def _seed_decision_trace_target(repo: Path) -> tuple[int, int, int]:
    audit_id, candidate_id = _seed_review_target(repo)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        decision_id = store.insert_decision(
            candidate_id=candidate_id,
            actor="reviewer",
            policy="proposal_only",
            decision="needs_review",
            reason="policy passed",
        )
    return audit_id, candidate_id, decision_id


def _allow_mcp_repo(repo: Path) -> None:
    sidecar = repo / ".sidecar"
    sidecar.mkdir(exist_ok=True)
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
  tool_policy:
    tugboat_record_episode: allow
    tugboat_request_audit: allow
    tugboat_request_eval: allow
    tugboat_request_proposal: allow
""".lstrip(),
        encoding="utf-8",
    )


def _insert_fixture_run(store: Store, run_id: str, run_dir: Path) -> None:
    store.insert_run(
        run_id=run_id,
        stage="proposal",
        manifest_hash="fixture-manifest",
        status="completed",
        run_dir=run_dir,
    )


def _mcp_stdio_responses(
    requests: list[dict[str, object]],
    *,
    repo: Path | None = None,
    read_only: bool = False,
) -> list[dict[str, object]]:
    output = io.StringIO()
    run_stdio_server(
        io.StringIO("".join(json.dumps(request) + "\n" for request in requests)),
        output,
        repo=repo,
        read_only=read_only,
    )
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_mcp_write_intent_defaults_denied_without_explicit_tool_allow(tmp_path: Path):
    repo = tmp_path
    sidecar = repo / ".sidecar"
    sidecar.mkdir(exist_ok=True)
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
""".lstrip(),
        encoding="utf-8",
    )

    assert tugboat_status(repo)["mode"] == "proposal_only"
    with pytest.raises(ValueError, match="MCP write-intent tool requires explicit allow"):
        tugboat_record_episode(repo, '{"type":"user_request","content":"Fix bug"}\n')

    assert not (sidecar / "mcp" / "episodes").exists()
    assert not (sidecar / "mcp" / "requests").exists()
    assert not (sidecar / "daemon.sqlite").exists()
    events = _mcp_events(repo)
    assert events[-1]["tool"] == "tugboat_record_episode"
    assert events[-1]["status"] == "denied"


def test_status_returns_read_only_summary_and_audits_call(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
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
        "latest_llmff_job": None,
        "latest_llmff_exit_code": None,
        "latest_llmff_failure_kind": None,
        "pending_candidates": 0,
        "retention_candidates": 0,
        "retention_redaction_candidates": 0,
        "manifest_policy": "unrestricted",
    }
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_status"


def test_status_reports_latest_llmff_job_failure_and_retention_candidates(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _allow_mcp_repo(repo)
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    lifecycle_dir = run_dir / "patch-propose"
    lifecycle_dir.mkdir(parents=True)
    manifest = tmp_path / "patch-propose.yaml"
    manifest.write_text("name: patch-propose\n", encoding="utf-8")
    events = lifecycle_dir / "llmff-events.jsonl"
    events.write_text(
        '{"event":"run_failed","run_failed":{"failure_kind":"provider_error"}}\n',
        encoding="utf-8",
    )
    trace = lifecycle_dir / "llmff-trace.jsonl"
    trace.write_text('{"event":"step"}\n', encoding="utf-8")
    checkpoint = lifecycle_dir / "checkpoint.json"
    checkpoint.write_text('{"manifest_hash":"abc"}\n', encoding="utf-8")
    old = 0
    os.utime(events, (old, old))

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="propose",
            manifest_hash="abc",
            status="failed",
            run_dir=run_dir,
        )
        store.record_llmff_run(
            run_id="run-1",
            manifest_hash="abc",
            result=RunResult(
                manifest_path=manifest,
                exit_code=1,
                trace_path=trace,
                events_path=events,
                checkpoint_path=checkpoint,
                output_paths={},
                failure_kind="provider_error",
            ),
        )

    result = tugboat_status(repo)

    assert result["latest_run"] == {
        "run_id": "run-1",
        "stage": "propose",
        "status": "failed",
    }
    assert result["latest_llmff_job"] == {
        "manifest_name": "patch-propose.yaml",
        "status": "failed",
    }
    assert result["latest_llmff_exit_code"] == 1
    assert result["latest_llmff_failure_kind"] == "provider_error"
    assert result["retention_candidates"] == 1


def test_status_reports_retention_redaction_candidate_count(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _allow_mcp_repo(repo)
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    trace = run_dir / "trace-input.jsonl"
    trace.write_text('{"output":"OPENAI_API_KEY=sk-1234567890abcdefghijkl"}\n', encoding="utf-8")
    os.utime(trace, (0, 0))

    result = tugboat_status(repo)

    assert result["retention_candidates"] == 1
    assert result["retention_redaction_candidates"] == 1


def test_status_reports_retention_scan_budget_exceeded_without_raw_exception(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
retention:
  max_scan_files: 1
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = sidecar / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "trace-input.jsonl").write_text("{}\n", encoding="utf-8")
    (run_dir / "events.jsonl").write_text("{}\n", encoding="utf-8")

    result = tugboat_status(repo)

    assert result["retention_candidates"] is None
    assert result["retention_redaction_candidates"] is None
    assert result["retention_error"].startswith("scan budget exceeded:")
    event = _mcp_events(repo)[-1]
    assert event["tool"] == "tugboat_status"
    assert event["status"] == "completed"


def test_mcp_call_rows_are_reachable_from_append_only_audit_event(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)

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
    _allow_mcp_repo(repo)
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
    _allow_mcp_repo(repo)
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
    _allow_mcp_repo(repo)
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
    _allow_mcp_repo(repo)
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
    _allow_mcp_repo(repo)
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [Missing](docs/MISSING.md).\n", encoding="utf-8")

    result = tugboat_harness_findings(repo)

    assert result == {
        "passed": False,
        "findings": ["AGENTS.md references missing repo-local markdown file docs/MISSING.md."],
    }
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_harness_findings"


def test_harness_findings_redact_raw_instruction_rule_text(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
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


def test_harness_health_returns_sanitized_read_only_report(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\n"
        "See [Missing](docs/MISSING.md).\n"
        "MUST keep private customer prompt alpha internal.\n"
        "MUST keep private customer prompt alpha internal.\n",
        encoding="utf-8",
    )
    (sidecar_dir(repo) / "recurring-failures.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "failures": [
                    {
                        "failure_id": "fail-1",
                        "summary": "token sk-thissecretkeyvalue1234567890 leaked",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = tugboat_harness_health(repo)

    assert result["passed"] is False
    assert result["summary"] == {
        "missing_doc_count": 1,
        "stale_doc_count": 0,
        "orphaned_runbook_count": 0,
        "recurring_failure_without_doc_count": 1,
        "doc_gardening_task_count": 2,
    }
    assert result["knowledge_map"] == {"AGENTS.md": ["docs/MISSING.md"]}
    assert result["missing_docs"] == ["docs/MISSING.md"]
    assert result["recurring_failures_without_docs"] == [
        "fail-1: token [REDACTED:openai_api_key] leaked"
    ]
    assert result["token_metrics"]["instruction_corpus_estimated_tokens"] > 0
    serialized = json.dumps(result, sort_keys=True)
    assert "private customer prompt alpha" not in serialized
    assert "sk-thissecret" not in serialized
    assert not (sidecar_dir(repo) / "harness-report.json").exists()
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_harness_health"


def test_bound_read_only_mcp_stdio_includes_harness_health(tmp_path: Path):
    _allow_mcp_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [Runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: verified\n---\n\n# Runbook\n",
        encoding="utf-8",
    )

    responses = _mcp_stdio_responses(
        [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "tugboat_harness_health", "arguments": {}},
            }
        ],
        repo=tmp_path,
        read_only=True,
    )

    payload = responses[0]["result"]["content"][0]["json"]
    assert payload["knowledge_map"] == {"AGENTS.md": ["docs/runbook.md"]}
    assert "token_metrics" in payload


def test_latest_runs_limits_results_and_returns_artifact_refs(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
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
    _allow_mcp_repo(repo)
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
    _allow_mcp_repo(repo)
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


def test_latest_failed_gates_returns_sanitized_gate_failures(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        for run_id in ("run-1", "run-2", "run-3"):
            run_dir = runs_dir(repo) / run_id
            run_dir.mkdir(parents=True)
            store.insert_run(
                run_id=run_id,
                stage="eval",
                manifest_hash=f"hash-{run_id}",
                status="completed",
                run_dir=run_dir,
            )
        (runs_dir(repo) / "run-1" / "policy-gate.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed": False,
                    "reasons": ["blocked sk-thissecretkeyvalue1234567890"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (runs_dir(repo) / "run-1" / "eval-report.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "candidate_id": 7,
                    "suite_id": "all",
                    "passed": False,
                    "trigger_score": 0.8,
                    "held_out_score": 0.6,
                    "governance_passed": False,
                    "recommendation": "reject",
                    "metrics": {"raw_note": "sk-thissecretkeyvalue1234567890"},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (runs_dir(repo) / "run-2" / "auto-apply-preflight.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "run-2",
                    "candidate_id": 8,
                    "mode": "commit",
                    "target_files": ["CODEX.md"],
                    "branch_name": "tugboat/run-2/candidate-8/codex-md",
                    "eligible": False,
                    "would_apply": False,
                    "lane": "docs_hygiene",
                    "reasons": ["eval_report_rejected"],
                    "approval_bundle": None,
                    "checks": {
                        "policy_gate": {"allowed": True, "reasons": []},
                        "stored_policy_gate": {"allowed": True, "reasons": []},
                        "eval_report": {
                            "candidate_id_matches": True,
                            "passed": False,
                            "recommendation": "reject",
                            "suite_id": "all",
                        },
                        "vcs": {
                            "preflight_passed": True,
                            "worktree_clean": True,
                            "dirty_paths": [],
                            "target_files_clean": True,
                            "base_hashes_match": True,
                            "reasons": [],
                        },
                        "auto_apply": {},
                    },
                    "readiness_metrics": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (runs_dir(repo) / "run-3" / "policy-gate.json").write_text(
            '{"schema_version":1,"allowed":true,"reasons":[]}\n',
            encoding="utf-8",
        )

    result = tugboat_latest_failed_gates(repo, limit=2)

    assert [gate["gate"] for gate in result["failed_gates"]] == [
        "auto_apply_preflight",
        "policy_gate",
    ]
    preflight = result["failed_gates"][0]
    assert preflight["run_id"] == "run-2"
    assert preflight["stage"] == "eval"
    assert preflight["failed"] is True
    assert preflight["candidate_id"] == 8
    assert preflight["suite_id"] == "all"
    assert preflight["recommendation"] == "reject"
    assert preflight["lane"] == "docs_hygiene"
    assert preflight["reason_codes"] == ["eval_report_rejected"]
    assert preflight["summary"] == "auto-apply preflight rejected candidate"
    assert preflight["artifact_status"] == "valid"
    assert preflight["source"] == {
        "kind": "auto_apply_preflight",
        "path": ".sidecar/runs/run-2/auto-apply-preflight.json",
        "sha256": preflight["source"]["sha256"],
    }
    assert len(preflight["source"]["sha256"]) == 64
    policy_gate = result["failed_gates"][1]
    assert policy_gate["run_id"] == "run-1"
    assert policy_gate["failed"] is True
    assert policy_gate["reason_codes"] == ["blocked [REDACTED:openai_api_key]"]
    assert policy_gate["summary"] == "policy gate rejected candidate"
    assert policy_gate["artifact_status"] == "valid"
    assert policy_gate["source"] == {
        "kind": "policy_gate",
        "path": ".sidecar/runs/run-1/policy-gate.json",
        "sha256": policy_gate["source"]["sha256"],
    }
    serialized = json.dumps(result, sort_keys=True)
    assert "sk-thissecret" not in serialized
    assert "raw_note" not in serialized
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_latest_failed_gates"


def test_latest_failed_gates_reports_malformed_gate_artifact_without_raw_payload(
    tmp_path: Path,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    run_dir = runs_dir(repo) / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "policy-gate.json").write_text(
        '{"allowed": false, "reasons": ["sk-thissecretkeyvalue1234567890"]}\n',
        encoding="utf-8",
    )
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="eval",
            manifest_hash="hash-run-1",
            status="completed",
            run_dir=run_dir,
        )

    result = tugboat_latest_failed_gates(repo)

    assert result["failed_gates"] == [
        {
            "run_id": "run-1",
            "stage": "eval",
            "gate": "policy_gate",
            "failed": True,
            "candidate_id": None,
            "suite_id": None,
            "recommendation": None,
            "lane": None,
            "reason_codes": ["artifact_malformed"],
            "summary": "policy gate artifact is malformed",
            "observed_at": result["failed_gates"][0]["observed_at"],
            "artifact_status": "malformed",
            "source": {
                "kind": "policy_gate",
                "path": ".sidecar/runs/run-1/policy-gate.json",
                "sha256": result["failed_gates"][0]["source"]["sha256"],
            },
        }
    ]
    assert "sk-thissecret" not in json.dumps(result, sort_keys=True)


def test_latest_failed_gates_includes_sanitized_ci_check_failures(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    ci_dir = sidecar_dir(repo) / "ci"
    ci_dir.mkdir(parents=True)
    (ci_dir / "ci-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "ci_check",
                "auto_apply": False,
                "checks": {
                    "index": {"passed": True, "indexed_documents": 1},
                    "harness": {
                        "passed": True,
                        "findings": [],
                        "report_path": ".sidecar/harness-report.json",
                        "report_sha256": "a" * 64,
                        "doc_gardening_task_count": 0,
                    },
                    "manifest_contracts": {"passed": True, "findings": []},
                    "semantic_policy_lint": {
                        "passed": False,
                        "findings": ["found sk-thissecretkeyvalue1234567890"],
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = tugboat_latest_failed_gates(repo)

    assert result["failed_gates"] == [
        {
            "run_id": "ci",
            "stage": "ci",
            "gate": "ci_check",
            "failed": True,
            "candidate_id": None,
            "suite_id": None,
            "recommendation": None,
            "lane": None,
            "reason_codes": ["semantic_policy_lint"],
            "summary": "CI check failed: semantic_policy_lint",
            "observed_at": result["failed_gates"][0]["observed_at"],
            "artifact_status": "valid",
            "source": {
                "kind": "ci_report",
                "path": ".sidecar/ci/ci-report.json",
                "sha256": result["failed_gates"][0]["source"]["sha256"],
            },
        }
    ]
    assert "sk-thissecret" not in json.dumps(result, sort_keys=True)


def test_bound_read_only_mcp_stdio_includes_latest_failed_gates(tmp_path: Path):
    _allow_mcp_repo(tmp_path)

    responses = _mcp_stdio_responses(
        [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "tugboat_latest_failed_gates", "arguments": {"limit": 5}},
            }
        ],
        repo=tmp_path,
        read_only=True,
    )

    payload = responses[0]["result"]["content"][0]["json"]
    assert payload == {"failed_gates": []}


def test_run_report_summarizes_known_artifacts_without_raw_payloads(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
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
    _allow_mcp_repo(repo)
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
    _allow_mcp_repo(repo)
    run_dir = runs_dir(repo) / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n", encoding="utf-8")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        _insert_fixture_run(store, "run-1", run_dir)
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
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,0 +1,1 @@\n+sk-thissecretkeyvalue1234567890\n",
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
    _allow_mcp_repo(repo)
    run_dir = runs_dir(repo) / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n", encoding="utf-8")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        _insert_fixture_run(store, "run-1", run_dir)
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
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,0 +1,1 @@\n+sk-thissecretkeyvalue1234567890\n",
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
    _allow_mcp_repo(repo)
    run_dir = runs_dir(repo) / "run-1"
    run_dir.mkdir(parents=True)
    (repo / "CODEX.md").write_text("# Rules\n", encoding="utf-8")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        _insert_fixture_run(store, "run-1", run_dir)
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
            diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@ -1,0 +1,1 @@\n+Use tests.\n",
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


def test_decision_trace_returns_artifact_ref_without_raw_payloads(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    _, _, decision_id = _seed_decision_trace_target(repo)

    result = tugboat_decision_trace(repo, str(decision_id))

    assert result["decision_ref"] == str(decision_id)
    assert result["artifact"] == {
        "kind": "decision_trace",
        "path": ".sidecar/runs/seed-review-target/decision-trace.json",
        "sha256": result["artifact"]["sha256"],
    }
    assert len(result["artifact"]["sha256"]) == 64
    serialized = json.dumps(result, sort_keys=True)
    assert "trace_events" not in serialized
    assert "rationale" not in serialized
    assert (repo / ".sidecar" / "runs" / "seed-review-target" / "decision-trace.json").exists()
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_decision_trace"


def test_recent_decisions_returns_redacted_review_history(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    _, candidate_id = _seed_review_target(repo)
    eval_report = runs_dir(repo) / "seed-review-target" / "eval-report.json"
    eval_report.write_text('{"passed":true}\n', encoding="utf-8")
    trace_path = runs_dir(repo) / "seed-review-target" / "decision-trace.json"
    trace_path.write_text('{"schema_version":1}\n', encoding="utf-8")
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id="all",
            report_path=eval_report,
            passed=True,
            metrics={"held_out_score": 0.93},
        )
        older_id = store.insert_decision(
            candidate_id=candidate_id,
            actor="reviewer",
            policy="proposal_only",
            decision="needs_review",
            reason="older decision",
        )
        newer_id = store.insert_decision(
            candidate_id=candidate_id,
            actor="sk-thissecretkeyvalue1234567890",
            policy="proposal_only",
            decision="rejected",
            reason="contains sk-thissecretkeyvalue1234567890",
        )
        store.connection.execute(
            "UPDATE decisions SET created_at = ? WHERE id = ?",
            ("2026-05-30T10:00:00+00:00", older_id),
        )
        store.connection.execute(
            "UPDATE decisions SET created_at = ? WHERE id = ?",
            ("2026-05-31T10:00:00+00:00", newer_id),
        )
        store.connection.commit()

    result = tugboat_recent_decisions(repo, limit=1)

    assert result["decisions"] == [
        {
            "decision_id": newer_id,
            "created_at": "2026-05-31T10:00:00+00:00",
            "actor": "[REDACTED:openai_api_key]",
            "policy": "proposal_only",
            "decision": "rejected",
            "reason_summary": "contains [REDACTED:openai_api_key]",
            "candidate": {
                "candidate_id": candidate_id,
                "base_file": "CODEX.md",
                "risk_class": "instruction_clarification",
                "state": "needs_review",
            },
            "latest_eval": {
                "suite_id": "all",
                "passed": True,
                "artifact": {
                    "kind": "eval_report",
                    "path": ".sidecar/runs/seed-review-target/eval-report.json",
                },
            },
            "artifacts": [
                {
                    "kind": "candidate_diff",
                    "path": ".sidecar/runs/seed-review-target/candidate.diff",
                },
                {
                    "kind": "decision_trace",
                    "path": ".sidecar/runs/seed-review-target/decision-trace.json",
                },
            ],
        }
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert "seeded candidate" not in serialized
    assert "sk-thissecret" not in serialized
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_recent_decisions"


def test_bound_read_only_mcp_stdio_includes_recent_decisions(tmp_path: Path):
    _allow_mcp_repo(tmp_path)
    _, candidate_id, decision_id = _seed_decision_trace_target(tmp_path)

    responses = _mcp_stdio_responses(
        [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "tugboat_recent_decisions",
                    "arguments": {"limit": 5},
                },
            }
        ],
        repo=tmp_path,
        read_only=True,
    )

    payload = responses[0]["result"]["content"][0]["json"]
    assert payload["decisions"][0]["decision_id"] == decision_id
    assert payload["decisions"][0]["candidate"]["candidate_id"] == candidate_id


def test_auto_update_status_exposes_read_only_lane_observability(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.append_audit_event(
            "auto_apply.shadowed",
            {"candidate_id": "7", "lane": "docs_hygiene"},
        )
        store.append_audit_event(
            "auto_apply.decided",
            {
                "candidate_id": "7",
                "lane": "docs_hygiene",
                "eligible": True,
                "phase": "precheck",
            },
        )
        store.append_audit_event(
            "auto_apply.applied",
            {"candidate_id": "7", "approval_bundle": {"lane": "docs_hygiene"}},
        )
        store.append_audit_event(
            "rollback.applied",
            {"candidate_id": "7"},
        )

    result = tugboat_auto_update_status(repo)

    assert result["auto_apply_enabled"] is False
    assert result["kill_switch_enabled"] is False
    assert result["lanes"]["docs_hygiene"] == {
        "shadowed": 1,
        "eligible": 1,
        "rejected": 0,
        "staged": 1,
        "applied": 1,
        "rolled_back": 1,
        "paused": 0,
    }
    assert result["daemon_queue"]["jobs_by_state"] == {}
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_auto_update_status"


def test_bound_read_only_mcp_stdio_includes_auto_update_status(tmp_path: Path):
    _allow_mcp_repo(tmp_path)

    responses = _mcp_stdio_responses(
        [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "tugboat_auto_update_status", "arguments": {}},
            }
        ],
        repo=tmp_path,
        read_only=True,
    )

    payload = responses[0]["result"]["content"][0]["json"]
    assert payload["auto_apply_enabled"] is False
    assert set(payload["lanes"]) >= {"docs_hygiene", "skill_improvement"}


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


def test_mcp_jsonrpc_requires_explicit_repo_allowlist_before_invocation(tmp_path: Path):
    response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {
                "name": "tugboat_status",
                "arguments": {"repo": str(tmp_path)},
            },
        }
    )

    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 15
    assert response["error"]["code"] == -32000
    assert "MCP repo allowlist is required" in response["error"]["message"]
    event = _mcp_events(tmp_path)[-1]
    assert event["tool"] == "tugboat_status"
    assert event["status"] == "denied"


def test_mcp_direct_calls_require_explicit_repo_allowlist_and_audit_denial(tmp_path: Path):
    with pytest.raises(ValueError, match="MCP repo allowlist is required"):
        tugboat_status(tmp_path)

    event = _mcp_events(tmp_path)[-1]
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
    _allow_mcp_repo(repo)
    codex = repo / "CODEX.md"
    original = "# Rules\n\nUse tests.\n"
    codex.write_text(original, encoding="utf-8")

    episode = tugboat_record_episode(
        repo,
        '{"type":"user_request","text":"Fix bug"}\n'
        '{"type":"user_correction","text":"Need regression test"}\n',
    )
    audit_id, candidate_id = _seed_review_target(repo)
    audit_request = tugboat_request_audit(repo, episode["trace_id"])
    proposal_request = tugboat_request_proposal(repo, str(audit_id))
    eval_request = tugboat_request_eval(repo, str(candidate_id), "all")

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
    ] == str(candidate_id)
    assert codex.read_text(encoding="utf-8") == original
    assert [event["tool"] for event in _mcp_events(repo)[-4:]] == [
        "tugboat_record_episode",
        "tugboat_request_audit",
        "tugboat_request_proposal",
        "tugboat_request_eval",
    ]


def test_record_episode_persists_canonical_trace_events_for_mcp_capture(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)

    episode = tugboat_record_episode(
        repo,
        '{"type":"user_request","content":"Fix bug"}\n'
        '{"type":"tool_result","tool":"pytest","exit_code":1,"output":"failed"}\n'
        '{"type":"final_answer","content":"I fixed it."}\n',
    )

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        episode_row = store.connection.execute(
            """
            SELECT id, trace_path, outcome
            FROM episodes
            """
        ).fetchone()
        trace_rows = store.connection.execute(
            """
            SELECT t.event_type, t.source_trust, json_extract(t.payload_json, '$.content'),
                   a.event_type
            FROM trace_events t
            JOIN audit_events a ON a.sequence = t.audit_event_sequence
            WHERE t.event_type != 'instruction_snapshot'
            ORDER BY t.line_number
            """
        ).fetchall()

    assert episode["episode_id"] == episode_row[0]
    assert episode_row[1] == str(repo / episode["artifact_ref"])
    assert episode_row[2] == "captured"
    assert trace_rows == [
        ("user_request", "user", "Fix bug", "trace_event.recorded"),
        ("tool_result", "tool", None, "trace_event.recorded"),
        ("final_answer", "agent", "I fixed it.", "trace_event.recorded"),
    ]


def test_record_episode_enriches_capture_with_active_instruction_and_policy_context(
    tmp_path: Path,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    (repo / "CODEX.md").write_text("# Rules\n\nUse regression tests.\n", encoding="utf-8")

    tugboat_record_episode(repo, '{"type":"user_request","content":"Fix bug"}\n')

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        rows = store.connection.execute(
            """
            SELECT event_type, source_trust, payload_json
            FROM trace_events
            WHERE event_type = 'instruction_snapshot'
            ORDER BY line_number
            """
        ).fetchall()

    payloads = [json.loads(row[2]) for row in rows]
    assert [(row[0], row[1]) for row in rows] == [
        ("instruction_snapshot", "artifact"),
        ("instruction_snapshot", "artifact"),
    ]
    assert payloads[0] == {
        "kind": "active_instruction",
        "sha256": CandidatePatch.hash_file(repo / "CODEX.md"),
        "source": "CODEX.md",
        "text": "# Rules\n\nUse regression tests.\n",
        "type": "instruction_snapshot",
    }
    assert payloads[1]["kind"] == "policy_config"
    assert payloads[1]["source"] == ".sidecar/policy.yaml"
    assert payloads[1]["type"] == "instruction_snapshot"
    assert payloads[1]["sha256"] == CandidatePatch.hash_file(repo / ".sidecar" / "policy.yaml")


def test_record_episode_writes_private_episode_artifacts(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    previous_umask = os.umask(0o022)
    try:
        episode = tugboat_record_episode(
            repo,
            '{"type":"user_request","content":"Fix bug"}\n',
        )
    finally:
        os.umask(previous_umask)

    trace_path = repo / episode["artifact_ref"]
    metadata_path = trace_path.with_suffix(".json")
    assert _mode(sidecar_dir(repo)) == 0o700
    assert _mode(sidecar_dir(repo) / "mcp") == 0o700
    assert _mode(sidecar_dir(repo) / "mcp" / "episodes") == 0o700
    assert _mode(trace_path) == 0o600
    assert _mode(metadata_path) == 0o600


def test_record_episode_normalizes_mcp_live_events_for_mcp_capture(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)

    episode = tugboat_record_episode(
        repo,
        '{"event":"request","text":"Update docs"}\n'
        '{"event":"tool.started","tool":"apply_patch"}\n'
        '{"event":"tool.finished","tool":"apply_patch","exit_code":0}\n'
        '{"event":"agent.final","text":"Done"}\n',
    )

    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        rows = store.connection.execute(
            """
            SELECT event_type, source_trust, payload_json
            FROM trace_events
            WHERE event_type != 'instruction_snapshot'
            ORDER BY line_number
            """
        ).fetchall()

    assert episode["artifact_ref"].startswith(".sidecar/mcp/episodes/")
    assert [row[0] for row in rows] == [
        "user_request",
        "tool_call",
        "tool_result",
        "final_answer",
    ]
    assert [row[1] for row in rows] == ["user", "tool", "tool", "agent"]
    assert json.loads(rows[0][2])["content"] == "Update docs"
    assert json.loads(rows[2][2])["exit_code"] == 0


def test_record_episode_denied_when_read_only_kill_switch_enabled(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    sidecar = repo / ".sidecar"
    sidecar.mkdir(exist_ok=True)
    (sidecar / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    with pytest.raises(ValueError, match="read-only kill switch"):
        tugboat_record_episode(repo, '{"type":"user_request","content":"Fix bug"}\n')

    assert not (sidecar / "mcp" / "episodes").exists()
    assert not (sidecar / "daemon.sqlite").exists()
    events = _mcp_events(repo)
    assert events[-1]["tool"] == "tugboat_record_episode"
    assert events[-1]["status"] == "denied"


@pytest.mark.parametrize(
    ("request_fn", "expected_tool"),
    (
        (lambda repo, trace_id: tugboat_request_audit(repo, trace_id), "tugboat_request_audit"),
        (
            lambda repo, trace_id: tugboat_request_proposal(repo, "audit-7"),
            "tugboat_request_proposal",
        ),
        (
            lambda repo, trace_id: tugboat_request_eval(repo, "candidate-9", "all"),
            "tugboat_request_eval",
        ),
    ),
)
def test_mcp_write_intent_requests_denied_when_read_only_kill_switch_enabled(
    tmp_path: Path,
    request_fn: Callable[[Path, str], dict[str, object]],
    expected_tool: str,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    sidecar = repo / ".sidecar"
    episodes = sidecar / "mcp" / "episodes"
    episodes.mkdir(parents=True)
    trace_id = "mcp-trace-seeded"
    (episodes / f"{trace_id}.jsonl").write_text(
        '{"type":"user_request","content":"Fix bug"}\n',
        encoding="utf-8",
    )
    (sidecar / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    with pytest.raises(ValueError, match="read-only kill switch"):
        request_fn(repo, trace_id)

    assert not (sidecar / "mcp" / "requests").exists()
    assert not (sidecar / "daemon.sqlite").exists()
    with Store.open(sidecar / "db.sqlite") as store:
        daemon_jobs = store.connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0]
        daemon_job_events = store.connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'daemon_job.recorded'"
        ).fetchone()[0]
    assert daemon_jobs == 0
    assert daemon_job_events == 0
    events = _mcp_events(repo)
    assert events[-1]["tool"] == expected_tool
    assert events[-1]["status"] == "denied"


def test_request_audit_read_only_kill_switch_denial_precedes_missing_trace_validation(
    tmp_path: Path,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    sidecar = repo / ".sidecar"
    sidecar.mkdir(exist_ok=True)
    (sidecar / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    with pytest.raises(ValueError, match="read-only kill switch"):
        tugboat_request_audit(repo, "mcp-trace-missing")

    assert not (sidecar / "mcp" / "requests").exists()
    assert not (sidecar / "daemon.sqlite").exists()
    events = _mcp_events(repo)
    assert events[-1]["tool"] == "tugboat_request_audit"
    assert events[-1]["status"] == "denied"


def test_request_audit_enqueues_daemon_executable_trace_audit(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    fake_llmff = _write_fake_audit_llmff(repo / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
  tool_policy:
    tugboat_record_episode: allow
    tugboat_request_audit: allow
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
    canonical = json.loads((run_dir / "canonical-episode.json").read_text(encoding="utf-8"))
    assert audit["failure_class"] == "instruction_conflict"
    assert audit["evidence_refs"] == [canonical["events"][0]["evidence_id"]]
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


def test_request_audit_preserves_mcp_live_trace_format_through_daemon(
    tmp_path: Path,
):
    repo = tmp_path
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    fake_llmff = _write_fake_audit_llmff(repo / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
  tool_policy:
    tugboat_record_episode: allow
    tugboat_request_audit: allow
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )
    episode = tugboat_record_episode(
        repo,
        '{"event":"request","text":"Fix bug"}\n'
        '{"event":"tool.started","tool":"apply_patch"}\n'
        '{"event":"tool.finished","tool":"apply_patch","exit_code":0}\n'
        '{"event":"agent.final","text":"Done"}\n',
    )

    assert episode["trace_format"] == "mcp"
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
    canonical_episode = json.loads(
        (run_dir / "canonical-episode.json").read_text(encoding="utf-8")
    )
    assert canonical_episode["request"] == "Fix bug"
    assert [event["event_type"] for event in canonical_episode["events"]] == [
        "user_request",
        "tool_call",
        "tool_result",
        "final_answer",
    ]
    with Store.open(repo / ".sidecar" / "daemon.sqlite") as queue_store:
        queued_payload = json.loads(
            queue_store.connection.execute(
                "SELECT payload_json FROM daemon_jobs"
            ).fetchone()[0]
        )
    assert queued_payload["trace_format"] == "mcp"


def test_request_audit_records_daemon_job_in_audited_store_before_worker_runs(
    tmp_path: Path,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    episode = tugboat_record_episode(
        repo,
        '{"type":"user_request","content":"Fix bug"}\n'
        '{"type":"user_correction","content":"Add regression tests"}\n',
    )

    request = tugboat_request_audit(repo, episode["trace_id"])

    with Store.open(repo / ".sidecar" / "daemon.sqlite") as queue_store:
        queued = queue_store.connection.execute(
            "SELECT id, kind, payload_json, state FROM daemon_jobs"
        ).fetchone()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        recorded = store.connection.execute(
            """
            SELECT d.job_id, d.state, d.payload_json, d.audit_event_sequence, a.event_type
            FROM daemon_jobs d
            JOIN audit_events a ON a.sequence = d.audit_event_sequence
            """
        ).fetchone()

    assert tuple(queued[:2]) == (1, "trace_audit")
    assert queued[3] == "queued"
    queued_payload = json.loads(queued[2])
    assert queued_payload["request_id"] == request["request_id"]
    assert queued_payload["artifact_ref"] == request["artifact_ref"]
    assert queued_payload["trace_path"] == str(repo / episode["artifact_ref"])
    assert recorded is not None
    assert recorded[0] == "1"
    assert recorded[1] == "queued"
    assert json.loads(recorded[2]) == queued_payload
    assert recorded[3] is not None
    assert recorded[4] == "daemon_job.recorded"


def test_request_audit_keeps_daemon_job_invisible_until_audit_record_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    episode = tugboat_record_episode(
        repo,
        '{"type":"user_request","content":"Fix bug"}\n',
    )
    original_record_daemon_job = Store.record_daemon_job
    visible_counts: list[int] = []

    def record_and_probe(self: Store, **kwargs: object) -> int:
        queue_path = repo / ".sidecar" / "daemon.sqlite"
        connection = sqlite3.connect(
            f"file:{queue_path.as_posix()}?mode=ro",
            uri=True,
            timeout=0.1,
        )
        with closing(connection):
            visible_counts.append(
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM daemon_jobs"
                    ).fetchone()[0]
                )
            )
        return original_record_daemon_job(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Store, "record_daemon_job", record_and_probe)

    tugboat_request_audit(repo, episode["trace_id"])

    assert visible_counts == [0]
    with Store.open(repo / ".sidecar" / "daemon.sqlite") as queue_store:
        assert queue_store.connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0] == 1


def test_request_audit_validates_request_artifact_before_queue_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    episode = tugboat_record_episode(
        repo,
        '{"type":"user_request","content":"Fix bug"}\n'
        '{"type":"user_correction","content":"Add regression tests"}\n',
    )

    def fail_mcp_request_artifact(name, payload):
        if name == "mcp-request.json":
            raise ValueError("invalid mcp request artifact")

    monkeypatch.setattr(
        "tugboat.mcp.contracts.validate_json_artifact",
        fail_mcp_request_artifact,
    )

    with pytest.raises(ValueError, match="invalid mcp request artifact"):
        tugboat_request_audit(repo, episode["trace_id"])

    assert not (sidecar_dir(repo) / "mcp" / "requests").exists()
    assert not (sidecar_dir(repo) / "daemon.sqlite").exists()


def test_write_intent_request_removes_artifact_when_daemon_enqueue_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    audit_id, _ = _seed_review_target(repo)

    def fail_enqueue(self, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(DaemonQueue, "enqueue_uncommitted", fail_enqueue)

    with pytest.raises(RuntimeError, match="queue unavailable"):
        tugboat_request_proposal(repo, str(audit_id))

    request_dir = sidecar_dir(repo) / "mcp" / "requests"
    assert not request_dir.exists() or list(request_dir.glob("*.json")) == []


def test_write_intent_request_marks_store_job_failed_when_queue_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    audit_id, _ = _seed_review_target(repo)
    original_open_sidecar = DaemonQueue.open_sidecar

    class FailingCommitConnection:
        def __init__(self, connection: sqlite3.Connection):
            self._connection = connection

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

        def commit(self) -> None:
            raise RuntimeError("queue commit failed")

    def open_with_failing_commit(root: Path) -> DaemonQueue:
        queue = original_open_sidecar(root)
        queue.connection = FailingCommitConnection(queue.connection)  # type: ignore[assignment]
        return queue

    monkeypatch.setattr(DaemonQueue, "open_sidecar", open_with_failing_commit)

    with pytest.raises(RuntimeError, match="queue commit failed"):
        tugboat_request_proposal(repo, str(audit_id))

    request_dir = sidecar_dir(repo) / "mcp" / "requests"
    assert not request_dir.exists() or list(request_dir.glob("*.json")) == []
    with DaemonQueue.open(repo / ".sidecar" / "daemon.sqlite") as queue:
        assert queue.connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0] == 0
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        row = store.connection.execute(
            """
            SELECT d.job_id, d.state, d.payload_json, a.event_type
            FROM daemon_jobs d
            JOIN audit_events a ON a.sequence = d.audit_event_sequence
            """
        ).fetchone()

    assert row is not None
    assert row[0] == "1"
    assert row[1] == "failed"
    assert row[2] is not None
    assert row[3] == "daemon_job.state_changed"


def test_mcp_write_intent_request_writes_private_request_artifact(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    audit_id, _ = _seed_review_target(repo)
    previous_umask = os.umask(0o022)
    try:
        request = tugboat_request_proposal(repo, str(audit_id))
    finally:
        os.umask(previous_umask)

    request_path = repo / request["artifact_ref"]
    assert _mode(sidecar_dir(repo)) == 0o700
    assert _mode(sidecar_dir(repo) / "mcp") == 0o700
    assert _mode(sidecar_dir(repo) / "mcp" / "requests") == 0o700
    assert _mode(request_path) == 0o600


@pytest.mark.parametrize(
    ("request_fn", "expected_message"),
    (
        (lambda repo: tugboat_request_proposal(repo, "../audit-7"), "invalid audit_id"),
        (lambda repo: tugboat_request_proposal(repo, ".."), "invalid audit_id"),
        (
            lambda repo: tugboat_request_eval(repo, "../candidate-9", "all"),
            "invalid candidate_id",
        ),
        (lambda repo: tugboat_request_eval(repo, "..", "all"), "invalid candidate_id"),
        (lambda repo: tugboat_request_eval(repo, "9", ""), "invalid suite"),
        (lambda repo: tugboat_request_eval(repo, "9", "../all"), "invalid suite"),
        (lambda repo: tugboat_request_eval(repo, "9", "all/nightly"), "invalid suite"),
    ),
)
def test_direct_mcp_write_intent_calls_validate_artifact_ids_before_queueing(
    tmp_path: Path,
    request_fn: Callable[[Path], dict[str, object]],
    expected_message: str,
):
    repo = tmp_path
    _allow_mcp_repo(repo)

    with pytest.raises(ValueError, match=expected_message):
        request_fn(repo)

    assert not (sidecar_dir(repo) / "mcp" / "requests").exists()
    assert not (sidecar_dir(repo) / "daemon.sqlite").exists()
    assert _mcp_events(repo)[-1]["status"] == "failed"


@pytest.mark.parametrize(
    ("request_fn", "expected_message"),
    (
        (lambda repo: tugboat_request_proposal(repo, "7"), "unknown audit_id"),
        (lambda repo: tugboat_request_eval(repo, "9", "all"), "unknown candidate_id"),
    ),
)
def test_mcp_write_intent_requests_validate_target_entities_before_queueing(
    tmp_path: Path,
    request_fn: Callable[[Path], dict[str, object]],
    expected_message: str,
):
    repo = tmp_path
    _allow_mcp_repo(repo)

    with pytest.raises(ValueError, match=expected_message):
        request_fn(repo)

    assert not (sidecar_dir(repo) / "mcp" / "requests").exists()
    assert not (sidecar_dir(repo) / "daemon.sqlite").exists()
    assert _mcp_events(repo)[-1]["status"] == "failed"


def test_mcp_write_intent_tools_record_daemon_jobs_in_audited_store(
    tmp_path: Path,
):
    repo = tmp_path
    _allow_mcp_repo(repo)
    audit_id, _ = _seed_review_target(repo)
    request = tugboat_request_proposal(repo, str(audit_id))

    with Store.open(repo / ".sidecar" / "daemon.sqlite") as queue_store:
        queued = queue_store.connection.execute(
            "SELECT id, kind, payload_json, state FROM daemon_jobs"
        ).fetchone()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        recorded = store.connection.execute(
            """
            SELECT d.job_id, d.state, d.payload_json, a.event_type
            FROM daemon_jobs d
            JOIN audit_events a ON a.sequence = d.audit_event_sequence
            """
        ).fetchone()

    assert tuple(queued[:2]) == (1, "proposal")
    assert queued[3] == "queued"
    queued_payload = json.loads(queued[2])
    assert queued_payload == {
        "request_id": request["request_id"],
        "artifact_ref": request["artifact_ref"],
        "audit_id": str(audit_id),
    }
    assert recorded is not None
    assert recorded[0] == "1"
    assert recorded[1] == "queued"
    assert json.loads(recorded[2]) == queued_payload
    assert recorded[3] == "daemon_job.recorded"


def test_mcp_eval_request_records_daemon_job_in_audited_store(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    _, candidate_id = _seed_review_target(repo)
    request = tugboat_request_eval(repo, str(candidate_id), "all")

    with Store.open(repo / ".sidecar" / "daemon.sqlite") as queue_store:
        queued = queue_store.connection.execute(
            "SELECT id, kind, payload_json, state FROM daemon_jobs"
        ).fetchone()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        recorded = store.connection.execute(
            """
            SELECT d.job_id, d.state, d.payload_json, a.event_type
            FROM daemon_jobs d
            JOIN audit_events a ON a.sequence = d.audit_event_sequence
            """
        ).fetchone()

    assert tuple(queued[:2]) == (1, "eval")
    assert queued[3] == "queued"
    queued_payload = json.loads(queued[2])
    assert queued_payload == {
        "request_id": request["request_id"],
        "artifact_ref": request["artifact_ref"],
        "candidate_id": str(candidate_id),
        "suite": "all",
    }
    assert recorded is not None
    assert recorded[0] == "1"
    assert recorded[1] == "queued"
    assert json.loads(recorded[2]) == queued_payload
    assert recorded[3] == "daemon_job.recorded"


def test_request_proposal_enqueues_daemon_executable_patch_propose(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    original_codex = (repo / "CODEX.md").read_bytes()
    fake_llmff = _write_fake_audit_llmff(repo / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
  tool_policy:
    tugboat_record_episode: allow
    tugboat_request_audit: allow
    tugboat_request_proposal: allow
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
    assert (repo / "CODEX.md").read_bytes() == original_codex
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
    assert (repo / "CODEX.md").read_bytes() == original_codex
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


def test_request_optimization_enqueues_daemon_executable_skillopt_loop(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    fake_llmff = _write_fake_audit_llmff(repo / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
  tool_policy:
    tugboat_record_episode: allow
    tugboat_request_optimization: allow
llmff:
  binary: {fake_llmff}
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )
    train = tugboat_record_episode(
        repo,
        '{"event":"request","text":"Preserve concise verification"}\n'
        '{"event":"outcome.label","label":"success","trusted":true}\n',
    )
    trigger = tugboat_record_episode(
        repo,
        '{"event":"request","text":"Fix bug"}\n'
        '{"event":"user.correction","text":"Need regression tests before final answer"}\n'
        '{"event":"outcome.label","label":"failure","trusted":true}\n',
    )

    request = tugboat_request_optimization(
        repo,
        trigger["trace_id"],
        "held-out",
        train_trace_ids=[train["trace_id"]],
        held_out_episode_ids=["held-out-episode"],
        unseen_suites=["governance"],
    )
    result = run_daemon_once(
        repo,
        DaemonRunConfig(
            worker_id="mcp-worker",
            lease_duration=timedelta(seconds=30),
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )

    assert request["kind"] == "optimization"
    assert result["processed"] is True
    assert result["final_state"] == "waiting_review"
    run_dir = sorted(runs_dir(repo).iterdir())[-1]
    for artifact in (
        "optimization-batch.json",
        "batch-audit-reports.json",
        "reflection.json",
        "candidate.json",
        "eval-report.json",
        "acceptance-summary.raw.json",
        "optimization-summary.json",
    ):
        assert (run_dir / artifact).exists()
    summary = json.loads((run_dir / "optimization-summary.json").read_text(encoding="utf-8"))
    assert summary["decision"] == "needs_review"
    assert summary["suite_id"] == "held-out"
    assert summary["unseen_suite_results"][0]["suite_id"] == "governance"
    batch = json.loads((run_dir / "optimization-batch.json").read_text(encoding="utf-8"))
    assert batch["held_out_episodes"] == ["held-out-episode"]
    assert batch["unseen_suites"] == ["governance"]
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        jobs = store.connection.execute(
            """
            SELECT manifest_name, status
            FROM llmff_jobs
            WHERE run_id = ?
            ORDER BY id
            """,
            (run_dir.name,),
        ).fetchall()
    assert ("acceptance-summary.yaml", "completed") in [tuple(row) for row in jobs]


def test_request_optimization_rejects_missing_train_trace_before_queueing(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    (repo / ".sidecar" / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
  tool_policy:
    tugboat_record_episode: allow
    tugboat_request_optimization: allow
""".lstrip(),
        encoding="utf-8",
    )
    trigger = tugboat_record_episode(repo, '{"event":"request","text":"Fix bug"}\n')

    with pytest.raises(ValueError, match="unknown train_trace_id"):
        tugboat_request_optimization(
            repo,
            trigger["trace_id"],
            "held-out",
            train_trace_ids=["mcp-trace-missing"],
        )

    with DaemonQueue.open_sidecar(repo) as queue:
        assert queue.connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0] == 0


def test_request_optimization_rejects_unsafe_split_ids_before_queueing(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)

    with pytest.raises(ValueError, match="held_out_episode_id"):
        tugboat_request_optimization(
            repo,
            "mcp-trace-missing",
            "held-out",
            held_out_episode_ids=["../escape"],
        )

    assert not (sidecar_dir(repo) / "mcp" / "requests").exists()


def test_request_eval_enqueues_daemon_executable_patch_eval(tmp_path: Path):
    repo = tmp_path
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    original_codex = (repo / "CODEX.md").read_bytes()
    fake_llmff = _write_fake_audit_llmff(repo / "fake-llmff")
    policy_dir = repo / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {repo.resolve().as_posix()}
  tool_policy:
    tugboat_record_episode: allow
    tugboat_request_audit: allow
    tugboat_request_eval: allow
    tugboat_request_proposal: allow
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
    assert (repo / "CODEX.md").read_bytes() == original_codex
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
    assert (repo / "CODEX.md").read_bytes() == original_codex
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
    tugboat_record_episode: allow
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
        "trace_format": "generic-jsonl",
        "execution": {
            "kind": "trace_audit",
            "payload": {
                "trace_path": str(repo / episode["artifact_ref"]),
                "trace_artifact_ref": episode["artifact_ref"],
                "trace_format": "generic-jsonl",
            },
        },
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


def test_request_audit_rejects_trace_id_path_traversal_without_queueing(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    escaped_trace = repo / ".sidecar" / "mcp" / "policy.jsonl"
    (repo / ".sidecar" / "mcp" / "episodes").mkdir(parents=True)
    escaped_trace.write_text('{"type":"user_request","content":"do not use"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="invalid trace_id"):
        tugboat_request_audit(repo, "../policy")

    assert not (repo / ".sidecar" / "mcp" / "requests").exists()
    with Store.open(repo / ".sidecar" / "daemon.sqlite") as queue_store:
        assert queue_store.connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0] == 0


def test_request_audit_validates_trace_id_before_reading_trace_path(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    escaped_trace = repo / ".sidecar" / "mcp" / "policy.jsonl"
    (repo / ".sidecar" / "mcp" / "episodes").mkdir(parents=True)
    escaped_trace.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid trace_id"):
        tugboat_request_audit(repo, "../policy")

    assert not (repo / ".sidecar" / "mcp" / "requests").exists()
    assert not (repo / ".sidecar" / "daemon.sqlite").exists()


def test_write_intent_episode_rejects_secret_payloads(tmp_path: Path):
    _allow_mcp_repo(tmp_path)
    with pytest.raises(ValueError, match="secret"):
        tugboat_record_episode(
            tmp_path,
            '{"type":"user_request","text":"sk-thissecretkeyvalue1234567890"}\n',
        )


def test_mcp_jsonrpc_lists_and_invokes_tools(tmp_path: Path):
    repo = tmp_path
    _allow_mcp_repo(repo)
    tools = list_mcp_tools()

    by_name = {tool["name"]: tool for tool in tools}
    assert "tugboat_active_instructions" in by_name
    assert "tugboat_candidate_report" in by_name
    assert "tugboat_index_summary" in by_name
    assert "tugboat_latest_audit" in by_name
    assert "tugboat_status" in by_name
    assert "tugboat_request_audit" in by_name
    repo_schema = {
        "additionalProperties": False,
        "properties": {"repo": {"type": "string"}},
        "required": ["repo"],
        "type": "object",
    }
    assert by_name["tugboat_active_instructions"] == {
        "inputSchema": repo_schema,
        "name": "tugboat_active_instructions",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_status"] == {
        "inputSchema": repo_schema,
        "name": "tugboat_status",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_index_summary"] == {
        "inputSchema": repo_schema,
        "name": "tugboat_index_summary",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_candidate_report"] == {
        "inputSchema": {
            "additionalProperties": False,
            "properties": {
                "repo": {"type": "string"},
                "candidate_id": {"type": "integer"},
            },
            "required": ["repo", "candidate_id"],
            "type": "object",
        },
        "name": "tugboat_candidate_report",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_latest_audit"] == {
        "inputSchema": repo_schema,
        "name": "tugboat_latest_audit",
        "mutates_instructions": False,
        "write_intent": False,
    }
    assert by_name["tugboat_request_audit"] == {
        "inputSchema": {
            "additionalProperties": False,
            "properties": {
                "repo": {"type": "string"},
                "trace_id": {"pattern": "^(?!\\.\\.?$)[A-Za-z0-9_.-]+$", "type": "string"},
            },
            "required": ["repo", "trace_id"],
            "type": "object",
        },
        "name": "tugboat_request_audit",
        "mutates_instructions": False,
        "write_intent": True,
    }
    assert by_name["tugboat_request_proposal"] == {
        "inputSchema": {
            "additionalProperties": False,
            "properties": {
                "repo": {"type": "string"},
                "audit_id": {"pattern": "^[0-9]+$", "type": "string"},
            },
            "required": ["repo", "audit_id"],
            "type": "object",
        },
        "name": "tugboat_request_proposal",
        "mutates_instructions": False,
        "write_intent": True,
    }
    assert by_name["tugboat_request_eval"] == {
        "inputSchema": {
            "additionalProperties": False,
                "properties": {
                    "repo": {"type": "string"},
                    "candidate_id": {"pattern": "^[0-9]+$", "type": "string"},
                    "suite": {"pattern": "^[A-Za-z0-9_.-]{1,64}$", "type": "string"},
                },
            "required": ["repo", "candidate_id", "suite"],
            "type": "object",
        },
        "name": "tugboat_request_eval",
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


def test_mcp_jsonrpc_denies_write_intent_when_read_only_kill_switch_enabled(
    tmp_path: Path,
):
    _allow_mcp_repo(tmp_path)
    sidecar = tmp_path / ".sidecar"
    episodes = sidecar / "mcp" / "episodes"
    episodes.mkdir(parents=True)
    trace_id = "mcp-trace-seeded"
    (episodes / f"{trace_id}.jsonl").write_text(
        '{"type":"user_request","content":"Fix bug"}\n',
        encoding="utf-8",
    )
    (sidecar / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {
                "name": "tugboat_request_audit",
                "arguments": {"repo": str(tmp_path), "trace_id": trace_id},
            },
        }
    )

    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 12
    assert response["error"]["code"] == -32000
    assert "read-only kill switch" in response["error"]["message"]
    assert not (sidecar / "mcp" / "requests").exists()
    assert not (sidecar / "daemon.sqlite").exists()


def test_mcp_jsonrpc_denies_write_intent_without_explicit_tool_allow(tmp_path: Path):
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir(exist_ok=True)
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
mcp:
  allowed_repositories:
    - {tmp_path.resolve().as_posix()}
""".lstrip(),
        encoding="utf-8",
    )

    response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": "tugboat_record_episode",
                "arguments": {
                    "repo": str(tmp_path),
                    "trace_jsonl": '{"type":"user_request","content":"Fix bug"}\n',
                },
            },
        }
    )

    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 13
    assert response["error"]["code"] == -32000
    assert "MCP write-intent tool requires explicit allow" in response["error"]["message"]
    assert not (sidecar / "mcp" / "episodes").exists()
    assert not (sidecar / "mcp" / "requests").exists()
    event = _mcp_events(tmp_path)[-1]
    assert event["tool"] == "tugboat_record_episode"
    assert event["status"] == "denied"


def test_mcp_jsonrpc_validates_tool_arguments_before_invocation(tmp_path: Path):
    _allow_mcp_repo(tmp_path)
    missing_repo = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "tugboat_status",
                "arguments": {},
            },
        }
    )
    wrong_candidate_type = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "tugboat_candidate_report",
                "arguments": {"repo": str(tmp_path), "candidate_id": "7"},
            },
        }
    )
    unknown_argument = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "tugboat_status",
                "arguments": {"repo": str(tmp_path), "extra": "ignored?"},
            },
        }
    )
    limit_below_minimum = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "tugboat_latest_runs",
                "arguments": {"repo": str(tmp_path), "limit": 0},
            },
        }
    )
    non_object_arguments = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "tugboat_status",
                "arguments": [],
            },
        }
    )
    unsafe_trace_id = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {
                "name": "tugboat_request_audit",
                "arguments": {"repo": str(tmp_path), "trace_id": "../policy"},
            },
        }
    )
    dotdot_trace_id = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "tugboat_request_audit",
                "arguments": {"repo": str(tmp_path), "trace_id": ".."},
            },
        }
    )

    assert missing_repo == {
        "jsonrpc": "2.0",
        "id": 5,
        "error": {"code": -32602, "message": "invalid params: missing required argument: repo"},
    }
    assert wrong_candidate_type == {
        "jsonrpc": "2.0",
        "id": 6,
        "error": {
            "code": -32602,
            "message": "invalid params: candidate_id must be integer",
        },
    }
    assert unknown_argument == {
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32602, "message": "invalid params: unknown argument: extra"},
    }
    assert limit_below_minimum == {
        "jsonrpc": "2.0",
        "id": 8,
        "error": {"code": -32602, "message": "invalid params: limit must be >= 1"},
    }
    assert non_object_arguments == {
        "jsonrpc": "2.0",
        "id": 9,
        "error": {"code": -32602, "message": "invalid params: arguments must be an object"},
    }
    assert unsafe_trace_id == {
        "jsonrpc": "2.0",
        "id": 10,
        "error": {"code": -32602, "message": "invalid params: trace_id has invalid format"},
    }
    assert dotdot_trace_id == {
        "jsonrpc": "2.0",
        "id": 11,
        "error": {"code": -32602, "message": "invalid params: trace_id has invalid format"},
    }
    assert _mcp_events(tmp_path) == [
        {
            "tool": "tugboat_candidate_report",
            "repo": tmp_path.as_posix(),
            "arguments": {"repo": str(tmp_path), "candidate_id": "7"},
            "status": "failed",
            "reason": "candidate_id must be integer",
        },
        {
            "tool": "tugboat_status",
            "repo": tmp_path.as_posix(),
            "arguments": {"repo": str(tmp_path), "extra": "ignored?"},
            "status": "failed",
            "reason": "unknown argument: extra",
        },
        {
            "tool": "tugboat_latest_runs",
            "repo": tmp_path.as_posix(),
            "arguments": {"repo": str(tmp_path), "limit": 0},
            "status": "failed",
            "reason": "limit must be >= 1",
        },
        {
            "tool": "tugboat_request_audit",
            "repo": tmp_path.as_posix(),
            "arguments": {"repo": str(tmp_path), "trace_id": "../policy"},
            "status": "failed",
            "reason": "trace_id has invalid format",
        },
        {
            "tool": "tugboat_request_audit",
            "repo": tmp_path.as_posix(),
            "arguments": {"repo": str(tmp_path), "trace_id": ".."},
            "status": "failed",
            "reason": "trace_id has invalid format",
        },
    ]


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_message"),
    (
        (
            "tugboat_request_proposal",
            {"audit_id": "audit-7"},
            "invalid params: audit_id has invalid format",
        ),
        (
            "tugboat_request_eval",
            {"candidate_id": "candidate-9", "suite": "all"},
            "invalid params: candidate_id has invalid format",
        ),
    ),
)
def test_mcp_jsonrpc_rejects_non_decimal_write_intent_target_ids_before_invocation(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, str],
    expected_message: str,
):
    response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"repo": str(tmp_path), **arguments},
            },
        }
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 13,
        "error": {"code": -32602, "message": expected_message},
    }
    assert not (sidecar_dir(tmp_path) / "daemon.sqlite").exists()
    events = _mcp_events(tmp_path)
    assert events == [
        {
            "tool": tool_name,
            "repo": tmp_path.as_posix(),
            "arguments": {"repo": str(tmp_path), **arguments},
            "status": "failed",
            "reason": expected_message.removeprefix("invalid params: "),
        }
    ]


@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected_message"),
    (
        (
            "tugboat_request_proposal",
            {"audit_id": "7"},
            "unknown audit_id: 7",
        ),
        (
            "tugboat_request_eval",
            {"candidate_id": "9", "suite": "all"},
            "unknown candidate_id: 9",
        ),
    ),
)
def test_mcp_jsonrpc_rejects_unknown_write_intent_targets_before_queueing(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, str],
    expected_message: str,
):
    _allow_mcp_repo(tmp_path)
    response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {"repo": str(tmp_path), **arguments},
            },
        }
    )

    assert response == {
        "jsonrpc": "2.0",
        "id": 14,
        "error": {"code": -32000, "message": expected_message},
    }
    assert not (sidecar_dir(tmp_path) / "mcp" / "requests").exists()
    assert not (sidecar_dir(tmp_path) / "daemon.sqlite").exists()
    events = _mcp_events(tmp_path)
    assert events[-1]["tool"] == tool_name
    assert events[-1]["status"] == "failed"


def test_mcp_jsonrpc_redacts_secret_bearing_error_messages(tmp_path: Path):
    _allow_mcp_repo(tmp_path)
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


def test_mcp_jsonrpc_redacts_secret_bearing_tool_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _allow_mcp_repo(tmp_path)

    def fake_status(repo: str) -> dict[str, object]:
        return {
            "mode": "proposal_only",
            "provider_token": "sk-thissecretkeyvalue1234567890",
            "nested": {"log": "using sk-thissecretkeyvalue1234567890"},
        }

    monkeypatch.setitem(mcp_contracts.MCP_TOOLS, "tugboat_status", fake_status)

    response = handle_jsonrpc_request(
        {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {
                "name": "tugboat_status",
                "arguments": {"repo": str(tmp_path)},
            },
        }
    )

    serialized = json.dumps(response)
    assert response["result"]["content"][0]["json"]["mode"] == "proposal_only"
    assert "sk-thissecret" not in serialized
    assert serialized.count("[REDACTED:openai_api_key]") == 2


def test_mcp_stdio_supports_initialize_handshake_and_initialized_notification():
    output = io.StringIO()
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "codex", "version": "test"},
        },
    }
    initialized = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

    assert (
        run_stdio_server(
            io.StringIO(
                json.dumps(initialize)
                + "\n"
                + json.dumps(initialized)
                + "\n"
                + json.dumps(tools_list)
                + "\n"
            ),
            output,
        )
        == 0
    )

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "tugboat", "version": "0.1.0"},
            },
        },
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": list_mcp_tools()}},
    ]


def test_bound_read_only_mcp_stdio_lists_only_read_tools(tmp_path: Path):
    _allow_mcp_repo(tmp_path)

    responses = _mcp_stdio_responses(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}],
        repo=tmp_path,
        read_only=True,
    )

    tools = responses[0]["result"]["tools"]
    tool_names = {tool["name"] for tool in tools}
    assert "tugboat_status" in tool_names
    assert "tugboat_index_summary" in tool_names
    assert "tugboat_decision_trace" not in tool_names
    assert not (tool_names & mcp_contracts.WRITE_INTENT_TOOLS)
    assert all(tool["write_intent"] is False for tool in tools)
    assert all(tool["mutates_instructions"] is False for tool in tools)
    assert all("repo" not in tool["inputSchema"]["required"] for tool in tools)


def test_mcp_tool_registry_exposes_only_approved_non_mutating_tools():
    approved_read_tools = {
        "tugboat_active_instructions",
        "tugboat_auto_update_status",
        "tugboat_candidate",
        "tugboat_candidate_report",
        "tugboat_daemon_status",
        "tugboat_decision_trace",
        "tugboat_harness_findings",
        "tugboat_harness_health",
        "tugboat_index_summary",
        "tugboat_instruction_graph",
        "tugboat_latest_audit",
        "tugboat_latest_failed_gates",
        "tugboat_latest_runs",
        "tugboat_recent_decisions",
        "tugboat_run_report",
        "tugboat_status",
    }
    approved_write_intent_tools = {
        "tugboat_record_episode",
        "tugboat_request_audit",
        "tugboat_request_eval",
        "tugboat_request_optimization",
        "tugboat_request_proposal",
    }
    deferred_authority_terms = {
        "apply",
        "rollback",
        "policy",
        "credential",
        "provider",
        "daemon_start",
        "daemon_stop",
        "daemon_control",
    }

    assert set(mcp_contracts.WRITE_INTENT_TOOLS) == approved_write_intent_tools
    assert set(mcp_contracts.MCP_TOOLS) == approved_read_tools | approved_write_intent_tools
    assert set(mcp_contracts.MCP_TOOL_INPUT_SCHEMAS) == set(mcp_contracts.MCP_TOOLS)

    for tool_name in mcp_contracts.MCP_TOOLS:
        assert not any(term in tool_name for term in deferred_authority_terms)

    tools = list_mcp_tools()
    assert {tool["name"] for tool in tools} == set(mcp_contracts.MCP_TOOLS)
    assert all(tool["mutates_instructions"] is False for tool in tools)
    assert {
        tool["name"] for tool in tools if tool["write_intent"]
    } == approved_write_intent_tools


def test_bound_read_only_mcp_stdio_rejects_decision_trace_without_writing_artifact(
    tmp_path: Path,
):
    _allow_mcp_repo(tmp_path)
    _, _, decision_id = _seed_decision_trace_target(tmp_path)
    trace_path = sidecar_dir(tmp_path) / "runs" / "seed-review-target" / "decision-trace.json"

    responses = _mcp_stdio_responses(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "tugboat_decision_trace",
                    "arguments": {"decision": str(decision_id)},
                },
            }
        ],
        repo=tmp_path,
        read_only=True,
    )

    assert "read-only MCP session" in responses[0]["error"]["message"]
    assert not trace_path.exists()


def test_bound_read_only_mcp_stdio_injects_repo_for_read_tool_calls(tmp_path: Path):
    _allow_mcp_repo(tmp_path)

    responses = _mcp_stdio_responses(
        [
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "tugboat_status", "arguments": {}},
            }
        ],
        repo=tmp_path,
        read_only=True,
    )

    assert responses[0]["id"] == 2
    assert responses[0]["result"]["content"][0]["json"]["mode"] == "proposal_only"
    assert _mcp_events(tmp_path)[-1]["tool"] == "tugboat_status"


def test_bound_read_only_mcp_stdio_rejects_repo_override(tmp_path: Path):
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    _allow_mcp_repo(repo)

    responses = _mcp_stdio_responses(
        [
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "tugboat_status",
                    "arguments": {"repo": str(other)},
                },
            }
        ],
        repo=repo,
        read_only=True,
    )

    assert responses[0]["id"] == 3
    assert responses[0]["error"]["code"] == -32000
    assert "bound MCP session does not allow repo override" in responses[0]["error"]["message"]
    event = _mcp_events(repo)[-1]
    assert event["tool"] == "tugboat_status"
    assert event["status"] == "failed"


def test_mcp_stdio_returns_parse_error_for_malformed_json():
    output = io.StringIO()

    assert run_stdio_server(io.StringIO("{not-json\nNaN\n"), output) == 0

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "parse error"},
        },
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32700, "message": "parse error"},
        },
    ]


def test_mcp_stdio_returns_invalid_request_for_valid_json_non_object():
    output = io.StringIO()

    assert run_stdio_server(io.StringIO("7\n"), output) == 0

    assert json.loads(output.getvalue()) == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "request must be an object"},
    }


def _mcp_events(repo: Path) -> list[dict[str, object]]:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        rows = store.connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = 'mcp.tool_called' ORDER BY sequence"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]
