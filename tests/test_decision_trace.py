from __future__ import annotations

import json
from pathlib import Path

from tugboat.db import Store
from tugboat.policy.gate import CandidatePatch, SourceRef
from tugboat.report.decision_trace import write_decision_trace
from tugboat.traces.schema import TraceBundle, TraceEvent


def test_decision_trace_includes_audited_decision_inputs(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    codex.write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    snapshot_dir = run_dir / "instruction-snapshot"
    snapshot_dir.mkdir(parents=True)
    snapshot = snapshot_dir / "CODEX.md"
    snapshot.write_text(codex.read_text(encoding="utf-8"), encoding="utf-8")
    graph = run_dir / "instruction-graph.json"
    graph.write_text('{"nodes":[]}\n', encoding="utf-8")
    reflection = run_dir / "reflection-001.json"
    reflection.write_text('{"summary":"Prefer regression guidance."}\n', encoding="utf-8")
    diff_path = run_dir / "candidate.diff"
    diff = "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use regression tests.\n"
    diff_path.write_text(diff, encoding="utf-8")
    report_path = run_dir / "eval-report.json"
    report_path.write_text('{"passed":true}\n', encoding="utf-8")
    trace_path = repo / "trace.jsonl"
    trace_path.write_text('{"type":"user_request","text":"Fix bug"}\n', encoding="utf-8")

    with Store.open(repo / ".sidecar" / "db.sqlite") as store:
        episode_id = store.record_trace_episode(
            repo=repo,
            bundle=TraceBundle(
                trace_path=trace_path,
                events=(
                    TraceEvent(
                        evidence_id="ev-1",
                        event_type="user_request",
                        source_trust="user",
                        line_number=1,
                        payload={"text": "Fix bug"},
                    ),
                ),
            ),
        )
        store.insert_run(
            run_id="run-1",
            stage="proposal",
            manifest_hash="manifest-hash",
            status="completed",
            run_dir=run_dir,
            episode_id=episode_id,
        )
        store.record_instruction_snapshot(
            run_id="run-1",
            path="CODEX.md",
            artifact_path=snapshot,
        )
        store.record_instruction_graph(run_id="run-1", artifact_path=graph)
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.82,
            evidence_refs=["ev-1"],
            instruction_refs=["CODEX.md#Rules"],
        )
        candidate = CandidatePatch(
            audit_id=audit_id,
            base_file="CODEX.md",
            base_hash=CandidatePatch.hash_file(codex),
            diff=diff,
            risk_class="instruction_clarification",
            rationale="Clarify regression test guidance.",
            sources=(SourceRef("ev-1", trusted=True),),
        )
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=candidate,
            diff_path=diff_path,
            state="needs_review",
        )
        store.record_reflection(
            run_id="run-1",
            source_ref="ev-1",
            artifact_path=reflection,
        )
        edit_id = store.record_edit_operation(
            candidate_id=candidate_id,
            operator="add",
            target_path="CODEX.md",
            payload={
                "operator": "add",
                "file": "CODEX.md",
                "section": "Rules",
                "changed_lines": 1,
                "normative_changes": 0,
            },
        )
        store.record_candidate_edit(
            candidate_id=candidate_id,
            edit_operation_id=edit_id,
            target_path="CODEX.md",
            risk_class="instruction_clarification",
        )
        store.record_eval_case(
            suite_id="all",
            case_id="held-out:regression",
            case_hash="a" * 64,
        )
        store.record_validation_split(
            suite_id="all",
            split_name="held_out",
            case_ids=("held-out:regression",),
        )
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id="all",
            report_path=report_path,
            passed=True,
            metrics={"held_out_cases": 1},
        )
        decision_id = store.insert_decision(
            candidate_id=candidate_id,
            actor="tugboat",
            policy="optimization_acceptance_gate",
            decision="needs_review",
            reason="held_out_improved",
        )
        store.record_review_action(
            candidate_id=candidate_id,
            actor="operator",
            action="requested_review",
            reason="human approval required",
        )
        store.record_rollback(
            decision_id=str(decision_id),
            candidate_id=candidate_id,
            reason="operator requested rollback",
            revert_commit="abc123",
            post_rollback_eval_result={"passed": True},
            rollback_plan="git revert abc123",
            executed=False,
        )
        store.record_optimizer_memory(
            repo_path=str(repo),
            memory_type="rejected_edit",
            key="CODEX.md:add:Rules",
            payload={"reason": "prior rejection"},
        )

    trace = json.loads(write_decision_trace(repo, str(decision_id)).read_text(encoding="utf-8"))

    assert trace["run"]["run_id"] == "run-1"
    assert trace["run"]["episode_id"] == episode_id
    assert trace["run"]["event_hash"]
    assert trace["episode"]["episode_id"] == episode_id
    assert trace["episode"]["trace_path"] == "trace.jsonl"
    assert trace["instruction_snapshots"] == [
        {
            "snapshot_id": trace["instruction_snapshots"][0]["snapshot_id"],
            "run_id": "run-1",
            "path": "CODEX.md",
            "artifact_path": ".sidecar/runs/run-1/instruction-snapshot/CODEX.md",
            "content_hash": CandidatePatch.hash_file(snapshot),
            "audit_event_sequence": trace["instruction_snapshots"][0]["audit_event_sequence"],
            "event_hash": trace["instruction_snapshots"][0]["event_hash"],
        }
    ]
    assert trace["instruction_graphs"][0]["artifact_path"] == (
        ".sidecar/runs/run-1/instruction-graph.json"
    )
    assert trace["reflections"][0]["artifact_path"] == ".sidecar/runs/run-1/reflection-001.json"
    assert trace["edit_operations"][0]["payload"]["section"] == "Rules"
    assert trace["candidate_edits"][0]["edit_operation_id"] == edit_id
    assert trace["eval_runs"][0]["status"] == "passed"
    assert trace["eval_cases"][0]["case_id"] == "held-out:regression"
    assert trace["validation_splits"][0]["case_ids"] == ["held-out:regression"]
    assert trace["review_actions"][0]["action"] == "requested_review"
    assert trace["rollbacks"][0]["rollback_plan"] == "git revert abc123"
    assert trace["optimizer_memory"][0]["payload"] == {"reason": "prior rejection"}
    for section in (
        "instruction_snapshots",
        "instruction_graphs",
        "reflections",
        "edit_operations",
        "candidate_edits",
        "eval_runs",
        "eval_cases",
        "validation_splits",
        "review_actions",
        "rollbacks",
        "optimizer_memory",
    ):
        assert trace[section][0]["audit_event_sequence"]
        assert trace[section][0]["event_hash"]
