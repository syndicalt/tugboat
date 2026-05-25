from __future__ import annotations

import json
from pathlib import Path

import pytest

from tugboat.db import Store
from tugboat.mcp import (
    tugboat_candidate,
    tugboat_harness_findings,
    tugboat_instruction_graph,
    tugboat_latest_runs,
    tugboat_run_report,
    tugboat_status,
)
from tugboat.paths import runs_dir, sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef


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


def test_harness_findings_are_plain_contract_and_audited(tmp_path: Path):
    repo = tmp_path
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [Missing](docs/MISSING.md).\n", encoding="utf-8")

    result = tugboat_harness_findings(repo)

    assert result == {
        "passed": False,
        "findings": ["AGENTS.md references missing repo-local markdown file docs/MISSING.md."],
    }
    assert _mcp_events(repo)[-1]["tool"] == "tugboat_harness_findings"


def test_latest_runs_limits_results_and_returns_artifact_refs(tmp_path: Path):
    repo = tmp_path
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        for run_id in ("run-1", "run-2", "run-3"):
            run_dir = runs_dir(repo) / run_id
            run_dir.mkdir(parents=True)
            (run_dir / "audit.json").write_text("{}\n", encoding="utf-8")
            store.insert_run(
                run_id=run_id,
                stage="audit",
                manifest_hash=f"hash-{run_id}",
                status="completed",
                run_dir=run_dir,
            )

    result = tugboat_latest_runs(repo, limit=2)

    assert [run["run_id"] for run in result["runs"]] == ["run-3", "run-2"]
    assert result["runs"][0]["artifacts"] == [{"kind": "audit", "path": ".sidecar/runs/run-3/audit.json"}]


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


def test_repo_must_be_local_path():
    with pytest.raises(ValueError, match="local repo path"):
        tugboat_status("https://example.com/repo.git")


def _mcp_events(repo: Path) -> list[dict[str, object]]:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        rows = store.connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = 'mcp.tool_called' ORDER BY sequence"
        ).fetchall()
    return [json.loads(row[0]) for row in rows]
