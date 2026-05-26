from pathlib import Path

import pytest

from tugboat.db import Store
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


def test_store_can_be_used_as_context_manager(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        store.append_audit_event("run.created", {"run_id": "run-1"})

    with pytest.raises(Exception, match="closed"):
        store.table_names()
