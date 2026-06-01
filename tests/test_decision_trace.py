from __future__ import annotations

import json
from pathlib import Path

from tugboat.cli import main
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
                        payload={
                            "model_payload": "sk-thissecretkeyvalue1234567890",
                            "text": "Fix bug. " + ("More detail. " * 80),
                        },
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
    assert trace["trace_events"][0]["payload_truncated"] is True
    assert len(trace["trace_events"][0]["payload_snippet"]) == 512
    assert "sk-thissecretkeyvalue1234567890" not in trace["trace_events"][0]["payload_snippet"]
    assert "[REDACTED:openai_api_key]" in trace["trace_events"][0]["payload_snippet"]
    assert '"text":"Fix bug.' in trace["trace_events"][0]["payload_snippet"]
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


def test_inspect_decision_compares_candidate_metadata_without_payloads(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    agents = repo / "AGENTS.md"
    codex.write_text("# Rules\n\nUse regression tests.\n", encoding="utf-8")
    agents.write_text("# Agents\n\nPrefer small changes.\n", encoding="utf-8")
    trace_path = repo / "trace.jsonl"
    trace_path.write_text('{"type":"user_request","text":"Secret payload"}\n', encoding="utf-8")

    run_1 = repo / ".sidecar" / "runs" / "run-1"
    run_2 = repo / ".sidecar" / "runs" / "run-2"
    run_1.mkdir(parents=True)
    run_2.mkdir(parents=True)
    diff_1 = run_1 / "candidate.diff"
    diff_2 = run_2 / "candidate.diff"
    eval_1 = run_1 / "eval-report.json"
    eval_2 = run_2 / "eval-report.json"
    diff_1.write_text("--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use held-out tests.\n", encoding="utf-8")
    diff_2.write_text("--- a/AGENTS.md\n+++ b/AGENTS.md\n@@\n+Document rollback.\n", encoding="utf-8")
    eval_1.write_text('{"passed":true}\n', encoding="utf-8")
    eval_2.write_text('{"passed":false}\n', encoding="utf-8")

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
                        payload={"text": "Sensitive customer request"},
                    ),
                ),
            ),
        )
        store.insert_run(
            run_id="run-1",
            stage="proposal",
            manifest_hash="manifest-hash-1",
            status="completed",
            run_dir=run_1,
            episode_id=episode_id,
        )
        store.insert_run(
            run_id="run-2",
            stage="proposal",
            manifest_hash="manifest-hash-2",
            status="completed",
            run_dir=run_2,
            episode_id=episode_id,
        )
        audit_1 = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.7,
            evidence_refs=["ev-1"],
            instruction_refs=["CODEX.md#Rules"],
        )
        audit_2 = store.insert_audit(
            run_id="run-2",
            failure_class="rollback_missing",
            severity="high",
            confidence=0.8,
            evidence_refs=["ev-1"],
            instruction_refs=["AGENTS.md#Agents"],
        )
        candidate_1 = store.insert_candidate(
            audit_id=audit_1,
            candidate=CandidatePatch(
                audit_id=audit_1,
                base_file="CODEX.md",
                base_hash=CandidatePatch.hash_file(codex),
                diff=diff_1.read_text(encoding="utf-8"),
                risk_class="instruction_clarification",
                rationale="First rationale should not print.",
                sources=(SourceRef("ev-1", trusted=True),),
            ),
            diff_path=diff_1,
            state="needs_review",
        )
        candidate_2 = store.insert_candidate(
            audit_id=audit_2,
            candidate=CandidatePatch(
                audit_id=audit_2,
                base_file="AGENTS.md",
                base_hash=CandidatePatch.hash_file(agents),
                diff=diff_2.read_text(encoding="utf-8"),
                risk_class="workflow_safety",
                rationale="Second rationale should not print.",
                sources=(SourceRef("ev-1", trusted=True),),
            ),
            diff_path=diff_2,
            state="rejected",
        )
        store.insert_eval(
            candidate_id=candidate_1,
            suite_id="all",
            report_path=eval_1,
            passed=True,
            metrics={"score": 1.0},
        )
        store.insert_eval(
            candidate_id=candidate_2,
            suite_id="all",
            report_path=eval_2,
            passed=False,
            metrics={"score": 0.5},
        )
        decision_1 = store.insert_decision(
            candidate_id=candidate_1,
            actor="tugboat",
            policy="optimization_acceptance_gate",
            decision="needs_review",
            reason="held_out_improved",
        )
        decision_2 = store.insert_decision(
            candidate_id=candidate_2,
            actor="operator",
            policy="manual_review",
            decision="rejected",
            reason="too_broad",
        )

    assert (
        main(
            [
                "inspect-decision",
                "--repo",
                str(repo),
                "--decision",
                str(decision_1),
                "--compare",
                str(decision_2),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out

    assert f"compare_decision_trace: {run_2 / 'decision-trace.json'}" in output
    assert f"candidate_id: {candidate_1} -> {candidate_2}" in output
    assert "candidate_file: CODEX.md -> AGENTS.md" in output
    assert "candidate_state: needs_review -> rejected" in output
    assert "risk_class: instruction_clarification -> workflow_safety" in output
    assert "decision: needs_review -> rejected" in output
    assert "evals: all=passed -> all=failed" in output
    assert "rollback_ready: no -> no" in output
    assert "highest_impact: none" in output
    assert "changed_fields: candidate_id, candidate_file, candidate_state, risk_class, decision, evals" in output
    assert "Sensitive customer request" not in output
    assert "payload_snippet" not in output
    assert "rationale should not print" not in output


def test_inspect_decision_prints_highest_impact_metadata_only(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    codex.write_text("# Rules\n\nUse regression tests.\n", encoding="utf-8")
    trace_path = repo / "trace.jsonl"
    trace_path.write_text('{"type":"user_request","text":"Secret payload"}\n', encoding="utf-8")
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    diff_path = run_dir / "candidate.diff"
    diff_path.write_text("--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use held-out tests.\n", encoding="utf-8")
    eval_report = run_dir / "eval-report.json"
    eval_report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 1,
                "governance_passed": True,
                "held_out_score": 0.92,
                "metrics": {"instruction_token_delta": 6},
                "passed": True,
                "recommendation": "accept",
                "suite_id": "all",
                "trigger_score": 0.84,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "optimization-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audit_run": "run-1",
                "candidate_id": 1,
                "decision": "needs_review",
                "governance_passed": True,
                "held_out_score": 0.92,
                "recommendation": "accept",
                "suite_id": "all",
                "trigger_score": 0.84,
                "validation_baseline_score": None,
                "acceptance_decision_recommendation": "needs_review",
                "acceptance_evidence": ["audit:1"],
                "acceptance_reasons": ["policy gate and eval report passed"],
                "acceptance_summary_path": ".sidecar/runs/run-1/acceptance-summary.raw.json",
                "accepted_bounded_edit_metadata": [
                    {
                        "changed_lines": 1,
                        "file": "CODEX.md",
                        "normative_changes": 0,
                        "operator": "add",
                        "section": "Testing",
                    },
                    {
                        "changed_lines": 2,
                        "file": "CODEX.md",
                        "normative_changes": 1,
                        "operator": "replace",
                        "section": "Safety",
                    },
                ],
                "reviewer_checklist": ["Review candidate diff"],
                "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

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
                        payload={"text": "Sensitive customer request"},
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
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.7,
            evidence_refs=["ev-1"],
            instruction_refs=["CODEX.md#Rules"],
        )
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=CandidatePatch(
                audit_id=audit_id,
                base_file="CODEX.md",
                base_hash=CandidatePatch.hash_file(codex),
                diff=diff_path.read_text(encoding="utf-8"),
                risk_class="instruction_clarification",
                rationale="Rationale should not print.",
                sources=(SourceRef("ev-1", trusted=True),),
            ),
            diff_path=diff_path,
            state="needs_review",
        )
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id="all",
            report_path=eval_report,
            passed=True,
            metrics={"instruction_token_delta": 6},
        )
        decision_id = store.insert_decision(
            candidate_id=candidate_id,
            actor="tugboat",
            policy="optimization_acceptance_gate",
            decision="needs_review",
            reason="held_out_improved",
        )

    assert main(["inspect-decision", "--repo", str(repo), "--decision", str(decision_id)]) == 0
    output = capsys.readouterr().out

    assert (
        "highest_impact: target=CODEX.md#Safety operator=replace changed_lines=2 "
        "normative_changes=1 held_out_delta=0.08 instruction_token_delta=6 "
        "governance_passed=true"
    ) in output
    assert "Sensitive customer request" not in output
    assert "payload_snippet" not in output
    assert "Rationale should not print" not in output
    assert "+Use held-out tests." not in output


def test_inspect_decision_prints_risk_and_rollback_readiness_metadata_only(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    codex.write_text("# Rules\n\nUse regression tests.\n", encoding="utf-8")
    trace_path = repo / "trace.jsonl"
    trace_path.write_text('{"type":"user_request","text":"Secret payload"}\n', encoding="utf-8")
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    diff_path = run_dir / "candidate.diff"
    diff_path.write_text("--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use held-out tests.\n", encoding="utf-8")
    eval_report = run_dir / "eval-report.json"
    eval_report.write_text('{"schema_version":1,"candidate_id":1,"governance_passed":true,"held_out_score":1.0,"metrics":{},"passed":true,"recommendation":"accept","suite_id":"all","trigger_score":0.8}\n', encoding="utf-8")
    (run_dir / "policy-gate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "allowed": False,
                "reasons": ["modal_weakening", "new_external_endpoint"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "apply-plan.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    (run_dir / "rollback-plan.json").write_text('{"schema_version":1}\n', encoding="utf-8")

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
                        payload={"text": "Sensitive customer request"},
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
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.7,
            evidence_refs=["ev-1"],
            instruction_refs=["CODEX.md#Rules"],
        )
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=CandidatePatch(
                audit_id=audit_id,
                base_file="CODEX.md",
                base_hash=CandidatePatch.hash_file(codex),
                diff=diff_path.read_text(encoding="utf-8"),
                risk_class="restricted_policy_change",
                rationale="Rationale should not print.",
                sources=(SourceRef("ev-1", trusted=True),),
            ),
            diff_path=diff_path,
            state="applied",
        )
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id="all",
            report_path=eval_report,
            passed=True,
            metrics={},
        )
        decision_id = store.insert_decision(
            candidate_id=candidate_id,
            actor="tugboat",
            policy="deterministic_policy_gate",
            decision="applied",
            reason="commit mode",
            applied_commit="abc123",
            rollback_ref=json.dumps(["tugboat", "rollback", "--decision", "latest"]),
        )

    assert main(["inspect-decision", "--repo", str(repo), "--decision", str(decision_id)]) == 0
    output = capsys.readouterr().out

    assert (
        "risk_explanation: class=restricted_policy_change policy_allowed=false "
        "policy_reasons=modal_weakening,new_external_endpoint review_required=restricted_review_required"
    ) in output
    assert (
        "rollback_readiness: state=applied_ready command=none "
        "artifact=.sidecar/runs/run-1/rollback-plan.json applied_commit=present"
    ) in output
    assert "Sensitive customer request" not in output
    assert "payload_snippet" not in output
    assert "Rationale should not print" not in output
    assert "tugboat rollback --decision latest" not in output


def test_inspect_decision_prints_planned_rollback_readiness_from_optimization_summary(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    codex = repo / "CODEX.md"
    codex.write_text("# Rules\n\nUse regression tests.\n", encoding="utf-8")
    trace_path = repo / "trace.jsonl"
    trace_path.write_text('{"type":"user_request","text":"Secret payload"}\n', encoding="utf-8")
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    diff_path = run_dir / "candidate.diff"
    diff_path.write_text("--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use held-out tests.\n", encoding="utf-8")
    eval_report = run_dir / "eval-report.json"
    eval_report.write_text('{"schema_version":1,"candidate_id":1,"governance_passed":true,"held_out_score":1.0,"metrics":{},"passed":true,"recommendation":"accept","suite_id":"all","trigger_score":0.8}\n', encoding="utf-8")
    (run_dir / "policy-gate.json").write_text(
        '{"schema_version":1,"allowed":true,"reasons":[]}\n',
        encoding="utf-8",
    )
    (run_dir / "optimization-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "audit_run": "run-1",
                "candidate_id": 1,
                "decision": "needs_review",
                "governance_passed": True,
                "held_out_score": 1.0,
                "recommendation": "accept",
                "suite_id": "all",
                "trigger_score": 0.8,
                "validation_baseline_score": None,
                "acceptance_decision_recommendation": "needs_review",
                "acceptance_evidence": ["audit:1"],
                "acceptance_reasons": ["policy gate and eval report passed"],
                "acceptance_summary_path": ".sidecar/runs/run-1/acceptance-summary.raw.json",
                "accepted_bounded_edit_metadata": [
                    {
                        "changed_lines": 1,
                        "file": "CODEX.md",
                        "normative_changes": 0,
                        "operator": "add",
                        "section": "Testing",
                    }
                ],
                "reviewer_checklist": ["Review candidate diff"],
                "rollback_command": ["tugboat", "rollback", "--decision", "latest"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

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
                        payload={"text": "Sensitive customer request"},
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
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.7,
            evidence_refs=["ev-1"],
            instruction_refs=["CODEX.md#Rules"],
        )
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=CandidatePatch(
                audit_id=audit_id,
                base_file="CODEX.md",
                base_hash=CandidatePatch.hash_file(codex),
                diff=diff_path.read_text(encoding="utf-8"),
                risk_class="instruction_clarification",
                rationale="Rationale should not print.",
                sources=(SourceRef("ev-1", trusted=True),),
            ),
            diff_path=diff_path,
            state="needs_review",
        )
        store.insert_eval(
            candidate_id=candidate_id,
            suite_id="all",
            report_path=eval_report,
            passed=True,
            metrics={},
        )
        decision_id = store.insert_decision(
            candidate_id=candidate_id,
            actor="tugboat",
            policy="optimization_acceptance_gate",
            decision="needs_review",
            reason="held_out_improved",
        )

    assert main(["inspect-decision", "--repo", str(repo), "--decision", str(decision_id)]) == 0
    output = capsys.readouterr().out

    assert (
        "rollback_readiness: state=planned command=tugboat rollback --decision latest "
        "artifact=.sidecar/runs/run-1/optimization-summary.json applied_commit=missing"
    ) in output
    assert "rollback_ready: no" in output
    assert "Sensitive customer request" not in output
    assert "payload_snippet" not in output
    assert "Rationale should not print" not in output
