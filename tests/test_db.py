import json
import sqlite3
from pathlib import Path

import pytest

from tugboat.db import Store
from tugboat.llmff.contracts import RunResult
from tugboat.policy.gate import CandidatePatch, SourceRef


def _candidate_patch(audit_id: int, *, base_file: str = "CODEX.md") -> CandidatePatch:
    return CandidatePatch(
        audit_id=audit_id,
        base_file=base_file,
        base_hash="abc123",
        diff=f"--- a/{base_file}\n+++ b/{base_file}\n@@\n+Clarify this.\n",
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
    )


def _seed_candidate(
    store: Store,
    tmp_path: Path,
    *,
    run_id: str = "run-1",
    state: str = "needs_review",
) -> int:
    audit_id = store.insert_audit(
        run_id=run_id,
        failure_class="instruction_missing",
        severity="medium",
        confidence=0.75,
        evidence_refs=["event:1"],
        instruction_refs=["CODEX.md#rules"],
    )
    candidate = _candidate_patch(audit_id)
    diff_path = tmp_path / f"candidate-{audit_id}.diff"
    diff_path.write_text(candidate.diff, encoding="utf-8")
    return store.insert_candidate(
        audit_id=audit_id,
        candidate=candidate,
        diff_path=diff_path,
        state=state,
    )


def _seed_run(store: Store, tmp_path: Path, *, run_id: str = "run-1") -> None:
    run_dir = tmp_path / run_id
    run_dir.mkdir(exist_ok=True)
    store.insert_run(
        run_id=run_id,
        stage="test",
        manifest_hash="abc",
        status="running",
        run_dir=run_dir,
    )


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


def test_core_decision_tables_require_audit_event_sequence(tmp_path: Path):
    core_tables = {"audits", "candidates", "evals", "decisions", "rollbacks"}
    with Store.open(tmp_path / "db.sqlite") as store:
        for table in core_tables:
            columns = {
                row[1]: row
                for row in store.connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            foreign_keys = store.connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()

            assert columns["audit_event_sequence"][3] == 1, table
            assert any(
                row[2] == "audit_events"
                and row[3] == "audit_event_sequence"
                and row[4] == "sequence"
                for row in foreign_keys
            ), table


def test_runs_require_audit_event_sequence(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        columns = {
            row[1]: row
            for row in store.connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        foreign_keys = store.connection.execute("PRAGMA foreign_key_list(runs)").fetchall()

        assert columns["audit_event_sequence"][3] == 1
        assert any(
            row[2] == "audit_events"
            and row[3] == "audit_event_sequence"
            and row[4] == "sequence"
            for row in foreign_keys
        )


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


def test_roadmap_extension_tables_require_audit_event_sequence(tmp_path: Path):
    roadmap_tables = {
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
    }
    with Store.open(tmp_path / "db.sqlite") as store:
        for table in roadmap_tables:
            columns = {
                row[1]: row
                for row in store.connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            foreign_keys = store.connection.execute(f"PRAGMA foreign_key_list({table})").fetchall()

            assert columns["audit_event_sequence"][3] == 1, table
            assert any(
                row[2] == "audit_events"
                and row[3] == "audit_event_sequence"
                and row[4] == "sequence"
                for row in foreign_keys
            ), table


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


def test_store_migrates_legacy_nullable_audit_event_sequence_constraint(
    tmp_path: Path,
):
    db_path = tmp_path / "db.sqlite"
    with Store.open(db_path) as store:
        event = store.append_audit_event(
            "trace_event.recorded",
            {"evidence_id": "ev-1", "event_type": "user_request"},
        )
        store.connection.execute("ALTER TABLE trace_events RENAME TO trace_events_legacy")
        store.connection.execute(
            """
            CREATE TABLE trace_events (
              id INTEGER PRIMARY KEY,
              episode_id INTEGER,
              evidence_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              source_trust TEXT NOT NULL DEFAULT 'untrusted',
              line_number INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              audit_event_sequence INTEGER
            )
            """
        )
        store.connection.execute(
            """
            INSERT INTO trace_events(
              episode_id, evidence_id, event_type, source_trust, line_number,
              payload_json, audit_event_sequence
            )
            VALUES (NULL, 'ev-1', 'user_request', 'user', 1, '{"type":"user_request"}', ?)
            """,
            (event.sequence,),
        )
        store.connection.execute("DROP TABLE trace_events_legacy")
        store.connection.commit()

    with Store.open(db_path) as store:
        columns = {
            row[1]: row
            for row in store.connection.execute("PRAGMA table_info(trace_events)").fetchall()
        }
        foreign_keys = store.connection.execute("PRAGMA foreign_key_list(trace_events)").fetchall()
        row = store.connection.execute(
            "SELECT evidence_id, audit_event_sequence FROM trace_events"
        ).fetchone()

    assert columns["audit_event_sequence"][3] == 1
    assert any(
        key[2] == "audit_events"
        and key[3] == "audit_event_sequence"
        and key[4] == "sequence"
        for key in foreign_keys
    )
    assert row == ("ev-1", event.sequence)


def test_store_rejects_legacy_null_audit_event_sequence(tmp_path: Path):
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
              source_trust TEXT NOT NULL DEFAULT 'untrusted',
              line_number INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              audit_event_sequence INTEGER
            )
            """
        )
        store.connection.execute(
            """
            INSERT INTO trace_events(
              episode_id, evidence_id, event_type, source_trust, line_number,
              payload_json, audit_event_sequence
            )
            VALUES (NULL, 'ev-1', 'user_request', 'user', 1, '{"type":"user_request"}', NULL)
            """
        )
        store.connection.execute("DROP TABLE trace_events_legacy")
        store.connection.commit()

    with pytest.raises(ValueError, match="trace_events.audit_event_sequence contains NULL"):
        Store.open(db_path)


def test_store_rejects_legacy_orphan_audit_event_sequence(tmp_path: Path):
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
              source_trust TEXT NOT NULL DEFAULT 'untrusted',
              line_number INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              audit_event_sequence INTEGER
            )
            """
        )
        store.connection.execute(
            """
            INSERT INTO trace_events(
              episode_id, evidence_id, event_type, source_trust, line_number,
              payload_json, audit_event_sequence
            )
            VALUES (NULL, 'ev-1', 'user_request', 'user', 1, '{"type":"user_request"}', 999)
            """
        )
        store.connection.execute("DROP TABLE trace_events_legacy")
        store.connection.commit()

    with pytest.raises(ValueError, match="trace_events.audit_event_sequence has orphaned values"):
        Store.open(db_path)


def test_store_backfills_legacy_runs_audit_event_sequence(tmp_path: Path):
    db_path = tmp_path / "db.sqlite"
    with Store.open(db_path) as store:
        event = store.append_audit_event(
            "run.recorded",
            {"run_id": "run-1", "stage": "audit", "status": "completed"},
        )
        store.connection.execute("ALTER TABLE runs RENAME TO runs_legacy")
        store.connection.execute(
            """
            CREATE TABLE runs (
              id TEXT PRIMARY KEY,
              episode_id INTEGER,
              stage TEXT NOT NULL,
              manifest_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              run_dir TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        store.connection.execute(
            """
            INSERT INTO runs(
              id, episode_id, stage, manifest_hash, status, run_dir, created_at, updated_at
            )
            VALUES ('run-1', NULL, 'audit', 'abc123', 'completed', '.sidecar/runs/run-1',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00')
            """
        )
        store.connection.execute("DROP TABLE runs_legacy")
        store.connection.commit()

    with Store.open(db_path) as store:
        row = store.connection.execute(
            """
            SELECT runs.audit_event_sequence, audit_events.event_type
            FROM runs
            JOIN audit_events ON audit_events.sequence = runs.audit_event_sequence
            WHERE runs.id = 'run-1'
            """
        ).fetchone()

    assert row == (event.sequence, "run.recorded")


def test_store_rejects_legacy_runs_without_reachable_record_event(tmp_path: Path):
    db_path = tmp_path / "db.sqlite"
    with Store.open(db_path) as store:
        store.connection.execute("ALTER TABLE runs RENAME TO runs_legacy")
        store.connection.execute(
            """
            CREATE TABLE runs (
              id TEXT PRIMARY KEY,
              episode_id INTEGER,
              stage TEXT NOT NULL,
              manifest_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              run_dir TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        store.connection.execute(
            """
            INSERT INTO runs(
              id, episode_id, stage, manifest_hash, status, run_dir, created_at, updated_at
            )
            VALUES ('run-1', NULL, 'audit', 'abc123', 'completed', '.sidecar/runs/run-1',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:01+00:00')
            """
        )
        store.connection.execute("DROP TABLE runs_legacy")
        store.connection.commit()

    with pytest.raises(ValueError, match="runs.audit_event_sequence contains NULL"):
        Store.open(db_path)


def test_insert_core_decision_rows_without_provenance_backfill(tmp_path: Path):
    candidate = CandidatePatch(
        audit_id=1,
        base_file="CODEX.md",
        base_hash="abc123",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Clarify this.\n",
        risk_class="instruction_clarification",
        rationale="Clarify ambiguous guidance.",
        sources=(SourceRef("trace-1", trusted=True),),
    )
    diff_path = tmp_path / "candidate.diff"
    diff_path.write_text(candidate.diff, encoding="utf-8")
    eval_report = tmp_path / "eval-report.json"
    eval_report.write_text("{}\n", encoding="utf-8")

    with Store.open(tmp_path / "db.sqlite") as store:
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=["event:1"],
            instruction_refs=["CODEX.md#rules"],
        )
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=candidate,
            diff_path=diff_path,
            state="needs_review",
        )
        eval_id = store.insert_eval(
            candidate_id=candidate_id,
            suite_id="all",
            report_path=eval_report,
            passed=True,
            metrics={"held_out_score": 0.9},
        )
        decision_id = store.insert_decision(
            candidate_id=candidate_id,
            actor="tugboat",
            policy="deterministic_policy_gate",
            decision="needs_review",
            reason="policy passed",
        )
        rollback_id = store.record_rollback(
            decision_id=str(decision_id),
            candidate_id=candidate_id,
            reason="operator requested rollback",
            revert_commit="def456",
            post_rollback_eval_result={"passed": True},
            rollback_plan=".sidecar/runs/run-1/rollback-plan.json",
            executed=False,
        )
        rows = [
            store.connection.execute(
                f"""
                SELECT row.audit_event_sequence, event.event_type
                FROM {table} row
                JOIN audit_events event ON event.sequence = row.audit_event_sequence
                WHERE row.id = ?
                """,
                (row_id,),
            ).fetchone()
            for table, row_id in (
                ("audits", audit_id),
                ("candidates", candidate_id),
                ("evals", eval_id),
                ("decisions", decision_id),
                ("rollbacks", rollback_id),
            )
        ]

    assert [row[1] for row in rows] == [
        "audit.recorded",
        "candidate.recorded",
        "eval.recorded",
        "decision.recorded",
        "rollback.recorded",
    ]
    assert all(row[0] is not None for row in rows)


def test_insert_run_stores_audit_event_sequence(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="audit",
            manifest_hash="abc123",
            status="completed",
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
        )
        row = store.connection.execute(
            """
            SELECT runs.audit_event_sequence, audit_events.event_type, audit_events.payload_json
            FROM runs
            JOIN audit_events ON audit_events.sequence = runs.audit_event_sequence
            WHERE runs.id = 'run-1'
            """
        ).fetchone()

    payload = json.loads(row[2])
    assert row[0] is not None
    assert row[1] == "run.recorded"
    assert payload == {"run_id": "run-1", "stage": "audit", "status": "completed"}


def test_insert_run_status_update_appends_new_event_without_overwriting_created_at(
    tmp_path: Path,
):
    with Store.open(tmp_path / "db.sqlite") as store:
        store.insert_run(
            run_id="run-1",
            stage="audit",
            manifest_hash="abc123",
            status="running",
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
        )
        first = store.connection.execute(
            """
            SELECT created_at, audit_event_sequence
            FROM runs
            WHERE id = 'run-1'
            """
        ).fetchone()

        store.insert_run(
            run_id="run-1",
            stage="audit",
            manifest_hash="abc123",
            status="completed",
            run_dir=tmp_path / ".sidecar" / "runs" / "run-1",
        )
        second = store.connection.execute(
            """
            SELECT created_at, status, audit_event_sequence
            FROM runs
            WHERE id = 'run-1'
            """
        ).fetchone()
        events = store.connection.execute(
            """
            SELECT sequence, event_type, json_extract(payload_json, '$.status')
            FROM audit_events
            WHERE event_type = 'run.recorded'
            ORDER BY sequence
            """
        ).fetchall()

    assert second[0] == first[0]
    assert second[1] == "completed"
    assert second[2] != first[1]
    assert events == [
        (first[1], "run.recorded", "running"),
        (second[2], "run.recorded", "completed"),
    ]


def test_update_candidate_state_moves_row_audit_link_to_state_update_event(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        candidate_id = _seed_candidate(store, tmp_path)
        original_sequence = store.connection.execute(
            "SELECT audit_event_sequence FROM candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()[0]

        store.update_candidate_state(
            candidate_id=candidate_id,
            state="rejected",
            reason="held_out_regressed",
        )

        row = store.connection.execute(
            """
            SELECT candidates.state, candidates.audit_event_sequence,
                   audit_events.event_type, audit_events.payload_json
            FROM candidates
            JOIN audit_events ON audit_events.sequence = candidates.audit_event_sequence
            WHERE candidates.id = ?
            """,
            (candidate_id,),
        ).fetchone()

    assert row[0] == "rejected"
    assert row[1] != original_sequence
    assert row[2] == "candidate.state_updated"
    assert json.loads(row[3]) == {
        "candidate_id": candidate_id,
        "state": "rejected",
        "reason": "held_out_regressed",
    }


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
        _seed_run(store, tmp_path)
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
        _seed_run(store, tmp_path)
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


def test_record_llmff_run_rejects_missing_run_parent_without_audit_event(
    tmp_path: Path,
):
    manifest = tmp_path / "patch-eval.yaml"
    manifest.write_text("name: patch-eval\n", encoding="utf-8")
    events = tmp_path / "missing-events.jsonl"
    trace = tmp_path / "llmff-trace.jsonl"
    checkpoint = tmp_path / "checkpoint.json"

    with Store.open(tmp_path / "db.sqlite") as store:
        with pytest.raises(ValueError, match="llmff_job run_id does not reference runs"):
            store.record_llmff_run(
                run_id="missing-run",
                manifest_hash="abc",
                result=RunResult(
                    manifest_path=manifest,
                    exit_code=0,
                    trace_path=trace,
                    events_path=events,
                    checkpoint_path=checkpoint,
                    output_paths={},
                ),
            )

        assert store.count("audit_events") == 0


def test_run_scoped_artifacts_reject_missing_run_parent_without_audit_event(
    tmp_path: Path,
):
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}\n", encoding="utf-8")

    with Store.open(tmp_path / "db.sqlite") as store:
        with pytest.raises(
            ValueError,
            match="instruction_snapshot run_id does not reference runs",
        ):
            store.record_instruction_snapshot(
                run_id="missing-run",
                path="CODEX.md",
                artifact_path=artifact,
            )
        with pytest.raises(
            ValueError,
            match="instruction_graph run_id does not reference runs",
        ):
            store.record_instruction_graph(
                run_id="missing-run",
                artifact_path=artifact,
            )
        with pytest.raises(ValueError, match="reflection run_id does not reference runs"):
            store.record_reflection(
                run_id="missing-run",
                source_ref="audit:1",
                artifact_path=artifact,
            )

        assert store.count("audit_events") == 0


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


def test_audit_event_rows_cannot_be_updated_directly(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        event = store.append_audit_event("run.created", {"run_id": "run-1"})

        with pytest.raises(sqlite3.IntegrityError, match="audit_events are append-only"):
            store.connection.execute(
                "UPDATE audit_events SET event_type = ? WHERE sequence = ?",
                ("tampered", event.sequence),
            )


def test_audit_event_rows_cannot_be_deleted_directly(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        event = store.append_audit_event("run.created", {"run_id": "run-1"})

        with pytest.raises(sqlite3.IntegrityError, match="audit_events are append-only"):
            store.connection.execute(
                "DELETE FROM audit_events WHERE sequence = ?",
                (event.sequence,),
            )


def test_insert_decision_stores_audit_event_sequence(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        candidate_id = _seed_candidate(store, tmp_path)
        decision_id = store.insert_decision(
            candidate_id=candidate_id,
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
        candidate_id = _seed_candidate(store, tmp_path)
        rollback_id = store.record_rollback(
            decision_id="run-1",
            candidate_id=candidate_id,
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
        candidate_id,
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
    with Store.open(tmp_path / "db.sqlite") as store:
        audit_id = store.insert_audit(
            run_id="run-1",
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=["event:1"],
            instruction_refs=["CODEX.md#rules"],
        )
        candidate = _candidate_patch(audit_id)
        diff_path = tmp_path / "candidate.diff"
        diff_path.write_text(candidate.diff, encoding="utf-8")
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
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


def test_insert_candidate_rejects_missing_audit_parent_without_audit_event(tmp_path: Path):
    candidate = _candidate_patch(999)
    diff_path = tmp_path / "candidate.diff"
    diff_path.write_text(candidate.diff, encoding="utf-8")

    with Store.open(tmp_path / "db.sqlite") as store:
        with pytest.raises(ValueError, match="candidate audit_id does not reference audits"):
            store.insert_candidate(
                audit_id=999,
                candidate=candidate,
                diff_path=diff_path,
                state="needs_review",
            )

        assert store.count("audit_events") == 0


def test_insert_eval_stores_audit_event_sequence(tmp_path: Path):
    report_path = tmp_path / "eval-report.json"
    report_path.write_text("{}\n", encoding="utf-8")

    with Store.open(tmp_path / "db.sqlite") as store:
        candidate_id = _seed_candidate(store, tmp_path)
        eval_id = store.insert_eval(
            candidate_id=candidate_id,
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


def test_insert_eval_rejects_missing_candidate_parent_without_audit_event(tmp_path: Path):
    report_path = tmp_path / "eval-report.json"
    report_path.write_text("{}\n", encoding="utf-8")

    with Store.open(tmp_path / "db.sqlite") as store:
        with pytest.raises(ValueError, match="eval candidate_id does not reference candidates"):
            store.insert_eval(
                candidate_id=999,
                suite_id="all",
                report_path=report_path,
                passed=True,
                metrics={"held_out_score": 0.9},
            )

        assert store.count("audit_events") == 0


def test_insert_decision_rejects_missing_candidate_parent_without_audit_event(
    tmp_path: Path,
):
    with Store.open(tmp_path / "db.sqlite") as store:
        with pytest.raises(
            ValueError,
            match="decision candidate_id does not reference candidates",
        ):
            store.insert_decision(
                candidate_id=999,
                actor="tugboat",
                policy="deterministic_policy_gate",
                decision="needs_review",
                reason="policy passed",
            )

        assert store.count("audit_events") == 0


def test_record_candidate_edit_rejects_missing_edit_operation_parent_without_audit_event(
    tmp_path: Path,
):
    with Store.open(tmp_path / "db.sqlite") as store:
        candidate_id = _seed_candidate(store, tmp_path)
        baseline_events = store.count("audit_events")

        with pytest.raises(
            ValueError,
            match="candidate_edit edit_operation_id does not reference edit_operations",
        ):
            store.record_candidate_edit(
                candidate_id=candidate_id,
                edit_operation_id=999,
                target_path="CODEX.md",
                risk_class="instruction_clarification",
            )

        assert store.count("audit_events") == baseline_events


def test_update_candidate_state_rejects_missing_candidate_without_audit_event(
    tmp_path: Path,
):
    with Store.open(tmp_path / "db.sqlite") as store:
        with pytest.raises(
            ValueError,
            match="candidate state candidate_id does not reference candidates",
        ):
            store.update_candidate_state(
                candidate_id=999,
                state="rejected",
                reason="missing parent",
            )

        assert store.count("audit_events") == 0


def test_record_review_action_rejects_missing_candidate_without_audit_event(
    tmp_path: Path,
):
    with Store.open(tmp_path / "db.sqlite") as store:
        with pytest.raises(
            ValueError,
            match="review_action candidate_id does not reference candidates",
        ):
            store.record_review_action(
                candidate_id=999,
                actor="reviewer",
                action="approved",
                reason="missing parent",
            )

        assert store.count("audit_events") == 0


def test_record_edit_operation_rejects_missing_candidate_without_audit_event(
    tmp_path: Path,
):
    with Store.open(tmp_path / "db.sqlite") as store:
        with pytest.raises(
            ValueError,
            match="edit_operation candidate_id does not reference candidates",
        ):
            store.record_edit_operation(
                candidate_id=999,
                operator="replace",
                target_path="CODEX.md",
                payload={"section": "Policy"},
            )

        assert store.count("audit_events") == 0


def test_record_rollback_rejects_missing_candidate_without_audit_event(
    tmp_path: Path,
):
    with Store.open(tmp_path / "db.sqlite") as store:
        with pytest.raises(
            ValueError,
            match="rollback candidate_id does not reference candidates",
        ):
            store.record_rollback(
                decision_id="run-1",
                candidate_id=999,
                reason="missing parent",
                revert_commit="def456",
                post_rollback_eval_result={"passed": True},
                rollback_plan=".sidecar/runs/run-1/rollback-plan.json",
                executed=False,
            )

        assert store.count("audit_events") == 0


def test_store_can_be_used_as_context_manager(tmp_path: Path):
    with Store.open(tmp_path / "db.sqlite") as store:
        store.append_audit_event("run.created", {"run_id": "run-1"})

    with pytest.raises(Exception, match="closed"):
        store.table_names()
