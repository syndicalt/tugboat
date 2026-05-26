import json
from pathlib import Path

import pytest

from tugboat.db import Store
from tugboat.llmff.contracts import RunResult
from tugboat.policy.gate import CandidatePatch, SourceRef


def test_store_initializes_core_tables(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        tables = store.table_names()

        assert {
            "documents",
            "chunks",
            "episodes",
            "runs",
            "audits",
            "candidates",
            "evals",
            "decisions",
            "rollbacks",
            "audit_events",
        }.issubset(tables)


def test_store_initializes_roadmap_extension_tables(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        tables = store.table_names()

        assert {
            "trace_events",
            "instruction_snapshots",
            "instruction_graphs",
            "llmff_jobs",
            "llmff_events",
            "llmff_outputs",
            "reflections",
            "edit_operations",
            "candidate_edits",
            "eval_cases",
            "eval_runs",
            "validation_splits",
            "review_actions",
            "mcp_calls",
            "daemon_jobs",
            "harness_findings",
            "doc_gardening_runs",
            "optimizer_memory",
        }.issubset(tables)


def test_store_migrates_legacy_trace_events_with_source_trust_column(tmp_path: Path):
    db_path = tmp_path / "db.sqlite"
    with Store.open(db_path) as store:
        store.connection.execute("ALTER TABLE trace_events RENAME TO trace_events_legacy")
        store.connection.execute(
            """
            CREATE TABLE trace_events (
              id INTEGER PRIMARY KEY,
              episode_id INTEGER,
              evidence_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              line_number INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              audit_event_sequence INTEGER
            )
            """
        )
        store.connection.execute("DROP TABLE trace_events_legacy")
        store.connection.commit()

    with Store.open(db_path) as store:
        columns = {
            row[1]: row
            for row in store.connection.execute("PRAGMA table_info(trace_events)").fetchall()
        }

    assert "source_trust" in columns
    assert columns["source_trust"][2] == "TEXT"
    assert columns["source_trust"][3] == 1
    assert columns["source_trust"][4] == "'untrusted'"


def test_record_llmff_run_persists_exit_code(tmp_path: Path):
    manifest = tmp_path / "patch-eval.yaml"
    manifest.write_text("name: patch-eval\n", encoding="utf-8")
    events = tmp_path / "llmff-events.jsonl"
    events.write_text(
        '{"event":"run_failed","run_failed":{"failure_kind":"provider_error","failure_message":"backend unavailable"}}\n',
        encoding="utf-8",
    )
    trace = tmp_path / "llmff-trace.jsonl"
    trace.write_text('{"event":"step"}\n', encoding="utf-8")
    checkpoint = tmp_path / "checkpoint.json"
    checkpoint.write_text('{"manifest_hash":"abc"}\n', encoding="utf-8")

    with Store.open(tmp_path / "db.sqlite") as store:
        store.record_llmff_run(
            run_id="run-1",
            manifest_hash="abc",
            result=RunResult(
                manifest_path=manifest,
                exit_code=7,
                trace_path=trace,
                events_path=events,
                checkpoint_path=checkpoint,
                output_paths={},
                failure_kind="provider_error",
                failure_message="backend unavailable",
            ),
        )
        row = store.connection.execute(
            """
            SELECT manifest_name, status, exit_code
            FROM llmff_jobs
            """
        ).fetchone()

    assert row == ("patch-eval.yaml", "failed", 7)


def test_record_llmff_run_captures_failure_summary_without_events(tmp_path: Path):
    manifest = tmp_path / "patch-eval.yaml"
    manifest.write_text("name: patch-eval\n", encoding="utf-8")
    events = tmp_path / "missing-events.jsonl"
    trace = tmp_path / "llmff-trace.jsonl"
    checkpoint = tmp_path / "checkpoint.json"

    with Store.open(tmp_path / "db.sqlite") as store:
        store.record_llmff_run(
            run_id="run-1",
            manifest_hash="abc",
            result=RunResult(
                manifest_path=manifest,
                exit_code=124,
                trace_path=trace,
                events_path=events,
                checkpoint_path=checkpoint,
                output_paths={},
                failure_kind="timeout",
                failure_message="Timed out after 12000 ms",
            ),
        )
        row = store.connection.execute(
            """
            SELECT payload_json
            FROM audit_events
            WHERE event_type = 'llmff_job.recorded'
            """
        ).fetchone()

    assert row is not None
    payload = json.loads(row[0])
    assert payload["run_failed"] == {
        "failure_kind": "timeout",
        "failure_message": "Timed out after 12000 ms",
    }


def test_audit_events_are_hash_chained(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        first = store.append_audit_event("run.created", {"run_id": "run-1"})
        second = store.append_audit_event("run.completed", {"run_id": "run-1"})

        assert first.sequence == 1
        assert first.previous_hash == ""
        assert second.sequence == 2
        assert second.previous_hash == first.event_hash


def test_audit_event_update_is_not_supported(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        event = store.append_audit_event("run.created", {"run_id": "run-1"})

        with pytest.raises(PermissionError):
            store.update_audit_event(event.sequence, {"event_type": "tampered"})


def test_insert_decision_stores_audit_event_sequence(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        decision_id = store.insert_decision(
            candidate_id=7,
            actor="tugboat",
            policy="deterministic_policy_gate",
            decision="needs_review",
            reason="policy passed",
        )
        row = store.connection.execute(
            """
            SELECT decisions.id, decisions.audit_event_sequence, audit_events.event_type
            FROM decisions
            JOIN audit_events ON audit_events.sequence = decisions.audit_event_sequence
            WHERE decisions.id = ?
            """,
            (decision_id,),
        ).fetchone()

    assert row == (decision_id, row[1], "decision.recorded")
    assert row[1] is not None


def test_record_rollback_stores_audit_event_sequence(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        rollback_id = store.record_rollback(
            decision_id="run-1",
            candidate_id=7,
            reason="rollback decision run-1",
            revert_commit="def456",
            post_rollback_eval_result={"passed": True},
            rollback_plan=".sidecar/runs/run-1/rollback-plan.json",
            executed=True,
        )
        row = store.connection.execute(
            """
            SELECT rollbacks.id, rollbacks.audit_event_sequence, audit_events.event_type,
                   rollbacks.decision_id, rollbacks.candidate_id, rollbacks.reason,
                   rollbacks.revert_commit, rollbacks.post_rollback_eval_result_json,
                   rollbacks.rollback_plan, rollbacks.executed
            FROM rollbacks
            JOIN audit_events ON audit_events.sequence = rollbacks.audit_event_sequence
            WHERE rollbacks.id = ?
            """,
            (rollback_id,),
        ).fetchone()

    assert row == (
        rollback_id,
        row[1],
        "rollback.recorded",
        "run-1",
        7,
        "rollback decision run-1",
        "def456",
        '{"passed": true}',
        ".sidecar/runs/run-1/rollback-plan.json",
        1,
    )
    assert row[1] is not None


def test_insert_audit_stores_audit_event_sequence(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=["event:1"],
            instruction_refs=["CODEX.md#rules"],
        )
        row = store.connection.execute(
            """
            SELECT audits.id, audits.audit_event_sequence, audit_events.event_type
            FROM audits
            JOIN audit_events ON audit_events.sequence = audits.audit_event_sequence
            WHERE audits.id = ?
            """,
            (audit_id,),
        ).fetchone()

    assert row == (audit_id, row[1], "audit.recorded")
    assert row[1] is not None


def test_insert_candidate_stores_audit_event_sequence(tmp_path: Path):
    candidate = CandidatePatch(
        audit_id=3,
        base_file="CODEX.md",
        base_hash="abc123",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Clarify this.\n",
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
    )
    diff_path = tmp_path / "candidate.diff"
    diff_path.write_text(candidate.diff, encoding="utf-8")

    with Store.open(tmp_path / "db.sqlite") as store:
        candidate_id = store.insert_candidate(
            audit_id=3,
            candidate=candidate,
            diff_path=diff_path,
            state="needs_review",
        )
        row = store.connection.execute(
            """
            SELECT candidates.id, candidates.audit_event_sequence, audit_events.event_type
            FROM candidates
            JOIN audit_events ON audit_events.sequence = candidates.audit_event_sequence
            WHERE candidates.id = ?
            """,
            (candidate_id,),
        ).fetchone()

    assert row == (candidate_id, row[1], "candidate.recorded")
    assert row[1] is not None


def test_insert_eval_stores_audit_event_sequence(tmp_path: Path):
    report_path = tmp_path / "eval-report.json"
    report_path.write_text("{}\n", encoding="utf-8")

    with Store.open(tmp_path / "db.sqlite") as store:
        eval_id = store.insert_eval(
            candidate_id=5,
            suite_id="all",
            report_path=report_path,
            passed=True,
            metrics={"held_out_score": 0.9},
        )
        row = store.connection.execute(
            """
            SELECT evals.id, evals.audit_event_sequence, audit_events.event_type
            FROM evals
            JOIN audit_events ON audit_events.sequence = evals.audit_event_sequence
            WHERE evals.id = ?
            """,
            (eval_id,),
        ).fetchone()

    assert row == (eval_id, row[1], "eval.recorded")
    assert row[1] is not None


def test_store_can_be_used_as_context_manager(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        store.append_audit_event("run.created", {"run_id": "run-1"})

    with pytest.raises(Exception, match="closed"):
        store.table_names()
