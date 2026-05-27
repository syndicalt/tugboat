from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tugboat.llmff.contracts import RunResult
from tugboat.models import IndexResult
from tugboat.policy.gate import CandidatePatch
from tugboat.traces.schema import TraceBundle


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,
  precedence INTEGER NOT NULL,
  protected INTEGER NOT NULL,
  hash TEXT NOT NULL,
  mtime REAL NOT NULL,
  parser_version TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL,
  heading_path TEXT NOT NULL,
  anchor TEXT NOT NULL,
  byte_start INTEGER NOT NULL,
  byte_end INTEGER NOT NULL,
  text_hash TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  trace_path TEXT NOT NULL,
  started_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  summary_hash TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  episode_id INTEGER,
  stage TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  run_dir TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  failure_class TEXT NOT NULL,
  severity TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_json TEXT NOT NULL,
  instruction_refs_json TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY,
  audit_id INTEGER NOT NULL,
  base_file TEXT NOT NULL,
  base_hash TEXT NOT NULL,
  diff_hash TEXT NOT NULL,
  diff_path TEXT NOT NULL,
  risk_class TEXT NOT NULL,
  rationale TEXT NOT NULL,
  state TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS evals (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  suite_id TEXT NOT NULL,
  report_path TEXT NOT NULL,
  passed INTEGER NOT NULL,
  metrics_json TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  actor TEXT NOT NULL,
  policy TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  applied_commit TEXT NOT NULL DEFAULT '',
  rollback_ref TEXT NOT NULL DEFAULT '',
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS rollbacks (
  id INTEGER PRIMARY KEY,
  decision_id TEXT NOT NULL,
  candidate_id INTEGER NOT NULL,
  reason TEXT NOT NULL,
  revert_commit TEXT NOT NULL,
  post_rollback_eval_result_json TEXT NOT NULL,
  rollback_plan TEXT NOT NULL,
  executed INTEGER NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS audit_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  previous_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
  SELECT RAISE(ABORT, 'audit_events are append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
  SELECT RAISE(ABORT, 'audit_events are append-only');
END;
CREATE TABLE IF NOT EXISTS trace_events (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER,
  evidence_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  source_trust TEXT NOT NULL DEFAULT 'untrusted',
  line_number INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS instruction_snapshots (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS instruction_graphs (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  graph_hash TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS llmff_jobs (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  manifest_name TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  exit_code INTEGER,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS llmff_events (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS llmff_outputs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL,
  output_name TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS reflections (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  reflection_hash TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS edit_operations (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  operator TEXT NOT NULL,
  target_path TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS candidate_edits (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  edit_operation_id INTEGER NOT NULL,
  target_path TEXT NOT NULL,
  risk_class TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS eval_cases (
  id INTEGER PRIMARY KEY,
  suite_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  case_hash TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  suite_id TEXT NOT NULL,
  status TEXT NOT NULL,
  report_path TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS validation_splits (
  id INTEGER PRIMARY KEY,
  suite_id TEXT NOT NULL,
  split_name TEXT NOT NULL,
  case_ids_json TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS review_actions (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS mcp_calls (
  id INTEGER PRIMARY KEY,
  tool_name TEXT NOT NULL,
  repo_path TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS daemon_jobs (
  id INTEGER PRIMARY KEY,
  job_id TEXT NOT NULL,
  repo_path TEXT NOT NULL,
  state TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS harness_findings (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  finding TEXT NOT NULL,
  severity TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS doc_gardening_runs (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  status TEXT NOT NULL,
  report_path TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
CREATE TABLE IF NOT EXISTS optimizer_memory (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER NOT NULL REFERENCES audit_events(sequence)
);
"""


CORE_DECISION_TABLES: tuple[str, ...] = (
    "audits",
    "candidates",
    "evals",
    "decisions",
    "rollbacks",
)


ROADMAP_EXTENSION_TABLES: tuple[str, ...] = (
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
)


AUDITED_PROVENANCE_TABLES: tuple[str, ...] = (
    "documents",
    "chunks",
    "episodes",
    "runs",
    *CORE_DECISION_TABLES,
    *ROADMAP_EXTENSION_TABLES,
)


@dataclass(frozen=True)
class AuditEvent:
    sequence: int
    event_type: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str


class Store:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @classmethod
    def open(cls, path: Path) -> "Store":
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            if _is_daemon_queue_database(connection):
                return cls(connection)
            connection.executescript(SCHEMA)
            _ensure_column(connection, "audits", "audit_event_sequence", "INTEGER")
            _ensure_column(connection, "candidates", "audit_event_sequence", "INTEGER")
            _ensure_column(connection, "evals", "audit_event_sequence", "INTEGER")
            _ensure_column(connection, "decisions", "audit_event_sequence", "INTEGER")
            _ensure_column(connection, "rollbacks", "audit_event_sequence", "INTEGER")
            _ensure_column(connection, "decisions", "applied_commit", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "decisions", "rollback_ref", "TEXT NOT NULL DEFAULT ''")
            _ensure_column(connection, "llmff_jobs", "exit_code", "INTEGER")
            _ensure_column(
                connection,
                "trace_events",
                "source_trust",
                "TEXT NOT NULL DEFAULT 'untrusted'",
            )
            for table in AUDITED_PROVENANCE_TABLES:
                _ensure_column(connection, table, "audit_event_sequence", "INTEGER")
            _backfill_instruction_index_audit_event_sequence(connection)
            _backfill_episodes_audit_event_sequence(connection)
            _backfill_runs_audit_event_sequence(connection)
            connection.commit()
            _repair_audit_event_constraints(connection)
            connection.commit()
            return cls(connection)
        except Exception:
            connection.close()
            raise

    def table_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row[0] for row in rows}

    def count(self, table: str) -> int:
        if table not in self.table_names():
            raise ValueError(f"unknown table: {table}")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _require_audit(self, audit_id: int, *, context: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM audits WHERE id = ?",
            (audit_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"{context} audit_id does not reference audits")

    def _require_run(self, run_id: str, *, context: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"{context} run_id does not reference runs")

    def _require_candidate(self, candidate_id: int, *, context: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM candidates WHERE id = ?",
            (candidate_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"{context} candidate_id does not reference candidates")

    def _require_edit_operation(self, edit_operation_id: int, *, context: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM edit_operations WHERE id = ?",
            (edit_operation_id,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"{context} edit_operation_id does not reference edit_operations"
            )

    def index_documents(self, repo: Path, result: IndexResult) -> None:
        repo_path = str(repo)
        self.connection.execute("DELETE FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE repo_path = ?)", (repo_path,))
        self.connection.execute("DELETE FROM documents WHERE repo_path = ?", (repo_path,))
        for document in result.documents:
            document_event = self.append_audit_event(
                "document.indexed",
                {
                    "repo": repo_path,
                    "path": document.path,
                    "kind": document.kind,
                    "hash": document.hash,
                },
            )
            cursor = self.connection.execute(
                """
                INSERT INTO documents(
                  repo_path, path, kind, precedence, protected, hash, mtime,
                  parser_version, audit_event_sequence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    repo_path,
                    document.path,
                    document.kind,
                    document.precedence,
                    int(document.protected),
                    document.hash,
                    document.mtime,
                    document.parser_version,
                    document_event.sequence,
                ),
            )
            document_id = int(cursor.lastrowid)
            for chunk in document.chunks:
                chunk_event = self.append_audit_event(
                    "instruction_chunk.indexed",
                    {
                        "repo": repo_path,
                        "path": document.path,
                        "document_id": document_id,
                        "anchor": chunk.anchor,
                        "text_hash": chunk.text_hash,
                    },
                )
                self.connection.execute(
                    """
                    INSERT INTO chunks(
                      document_id, heading_path, anchor, byte_start, byte_end,
                      text_hash, audit_event_sequence
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        json.dumps(list(chunk.heading_path), sort_keys=True),
                        chunk.anchor,
                        chunk.byte_start,
                        chunk.byte_end,
                        chunk.text_hash,
                        chunk_event.sequence,
                    ),
                )
        self.connection.commit()
        self.append_audit_event(
            "documents.indexed",
            {"repo": repo_path, "documents": len(result.documents)},
        )

    def insert_run(
        self,
        *,
        run_id: str,
        stage: str,
        manifest_hash: str,
        status: str,
        run_dir: Path,
        episode_id: int | None = None,
    ) -> None:
        now = _now()
        event = self.append_audit_event(
            "run.recorded",
            {"run_id": run_id, "stage": stage, "status": status},
        )
        existing = self.connection.execute(
            "SELECT created_at FROM runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO runs(
                  id, episode_id, stage, manifest_hash, status, run_dir,
                  created_at, updated_at, audit_event_sequence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    episode_id,
                    stage,
                    manifest_hash,
                    status,
                    str(run_dir),
                    now,
                    now,
                    event.sequence,
                ),
            )
        else:
            self.connection.execute(
                """
                UPDATE runs
                SET episode_id = ?, stage = ?, manifest_hash = ?, status = ?,
                    run_dir = ?, updated_at = ?, audit_event_sequence = ?
                WHERE id = ?
                """,
                (
                    episode_id,
                    stage,
                    manifest_hash,
                    status,
                    str(run_dir),
                    now,
                    event.sequence,
                    run_id,
                ),
            )
        self.connection.commit()

    def record_llmff_run(
        self,
        *,
        run_id: str,
        manifest_hash: str,
        result: RunResult,
    ) -> int:
        self._require_run(run_id, context="llmff_job")
        status = "completed" if result.exit_code == 0 else "failed"
        job_payload: dict[str, Any] = {
            "run_id": run_id,
            "manifest_name": result.manifest_path.name,
            "status": status,
            "exit_code": result.exit_code,
        }
        if result.failure_kind is not None or result.failure_message is not None:
            job_payload["run_failed"] = {
                "failure_kind": result.failure_kind,
                "failure_message": result.failure_message,
            }
        job_event = self.append_audit_event(
            "llmff_job.recorded",
            job_payload,
        )
        cursor = self.connection.execute(
            """
            INSERT INTO llmff_jobs(
              run_id,
              manifest_name,
              manifest_hash,
              status,
              exit_code,
              audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                result.manifest_path.name,
                manifest_hash,
                status,
                result.exit_code,
                job_event.sequence,
            ),
        )
        job_id = int(cursor.lastrowid)
        for payload in _jsonl_payloads(result.events_path):
            event_type = str(payload.get("event", "unknown"))
            event = self.append_audit_event(
                "llmff_event.recorded",
                {"job_id": job_id, "event_type": event_type},
            )
            self.connection.execute(
                """
                INSERT INTO llmff_events(job_id, event_type, payload_json, audit_event_sequence)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, event_type, json.dumps(payload, sort_keys=True), event.sequence),
            )
        for output_name, path in sorted(result.output_paths.items()):
            if not path.exists():
                continue
            event = self.append_audit_event(
                "llmff_output.recorded",
                {
                    "job_id": job_id,
                    "output_name": output_name,
                    "artifact_path": str(path),
                },
            )
            self.connection.execute(
                """
                INSERT INTO llmff_outputs(
                  job_id, output_name, artifact_path, content_hash, audit_event_sequence
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, output_name, str(path), _file_hash(path), event.sequence),
            )
        self.connection.commit()
        return job_id

    def record_instruction_snapshot(
        self,
        *,
        run_id: str,
        path: str,
        artifact_path: Path,
    ) -> int:
        self._require_run(run_id, context="instruction_snapshot")
        content_hash = _file_hash(artifact_path)
        event = self.append_audit_event(
            "instruction_snapshot.recorded",
            {
                "run_id": run_id,
                "path": path,
                "content_hash": content_hash,
                "artifact_path": str(artifact_path),
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO instruction_snapshots(
              run_id, path, content_hash, artifact_path, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, path, content_hash, str(artifact_path), event.sequence),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_instruction_graph(
        self,
        *,
        run_id: str,
        artifact_path: Path,
    ) -> int:
        self._require_run(run_id, context="instruction_graph")
        graph_hash = _file_hash(artifact_path)
        event = self.append_audit_event(
            "instruction_graph.recorded",
            {
                "run_id": run_id,
                "graph_hash": graph_hash,
                "artifact_path": str(artifact_path),
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO instruction_graphs(
              run_id, graph_hash, artifact_path, audit_event_sequence
            )
            VALUES (?, ?, ?, ?)
            """,
            (run_id, graph_hash, str(artifact_path), event.sequence),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_trace_episode(self, *, repo: Path, bundle: TraceBundle) -> int:
        episode_id = _next_integer_id(self.connection, "episodes")
        summary_hash = hashlib.sha256(
            json.dumps(
                [event.evidence_id for event in bundle.events],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        episode_event = self.append_audit_event(
            "episode.recorded",
            {
                "episode_id": episode_id,
                "repo": str(repo),
                "trace_path": str(bundle.trace_path),
                "events": len(bundle.events),
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO episodes(
              id, repo_path, trace_path, started_at, outcome, summary_hash,
              audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                episode_id,
                str(repo),
                str(bundle.trace_path),
                _now(),
                "captured",
                summary_hash,
                episode_event.sequence,
            ),
        )
        episode_id = int(cursor.lastrowid)
        for event in bundle.events:
            audit_event = self.append_audit_event(
                "trace_event.recorded",
                {
                    "episode_id": episode_id,
                    "evidence_id": event.evidence_id,
                    "event_type": event.event_type,
                    "source_trust": event.source_trust,
                    "line_number": event.line_number,
                },
            )
            self.connection.execute(
                """
                INSERT INTO trace_events(
                  episode_id, evidence_id, event_type, source_trust, line_number, payload_json,
                  audit_event_sequence
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    event.evidence_id,
                    event.event_type,
                    event.source_trust,
                    event.line_number,
                    json.dumps(event.payload, sort_keys=True),
                    audit_event.sequence,
                ),
            )
        self.connection.commit()
        return episode_id

    def insert_audit(
        self,
        *,
        run_id: str,
        failure_class: str,
        severity: str,
        confidence: float,
        evidence_refs: list[str],
        instruction_refs: list[str],
    ) -> int:
        self._require_run(run_id, context="audit")
        audit_id = _next_integer_id(self.connection, "audits")
        event = self.append_audit_event("audit.recorded", {"audit_id": audit_id, "run_id": run_id})
        self.connection.execute(
            """
            INSERT INTO audits(
              id, run_id, failure_class, severity, confidence, evidence_json,
              instruction_refs_json, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                run_id,
                failure_class,
                severity,
                confidence,
                json.dumps(evidence_refs, sort_keys=True),
                json.dumps(instruction_refs, sort_keys=True),
                event.sequence,
            ),
        )
        self.connection.commit()
        return audit_id

    def insert_candidate(
        self,
        *,
        audit_id: int,
        candidate: CandidatePatch,
        diff_path: Path,
        state: str,
    ) -> int:
        self._require_audit(audit_id, context="candidate")
        candidate_id = _next_integer_id(self.connection, "candidates")
        event = self.append_audit_event(
            "candidate.recorded",
            {"candidate_id": candidate_id, "audit_id": audit_id},
        )
        self.connection.execute(
            """
            INSERT INTO candidates(
              id, audit_id, base_file, base_hash, diff_hash, diff_path, risk_class,
              rationale, state, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                audit_id,
                candidate.base_file,
                candidate.base_hash,
                candidate.diff_hash,
                str(diff_path),
                candidate.risk_class,
                candidate.rationale,
                state,
                event.sequence,
            ),
        )
        self.connection.commit()
        return candidate_id

    def update_candidate_state(self, *, candidate_id: int, state: str, reason: str) -> None:
        self._require_candidate(candidate_id, context="candidate state")
        event = self.append_audit_event(
            "candidate.state_updated",
            {"candidate_id": candidate_id, "state": state, "reason": reason},
        )
        self.connection.execute(
            "UPDATE candidates SET state = ?, audit_event_sequence = ? WHERE id = ?",
            (state, event.sequence, candidate_id),
        )
        self.connection.commit()

    def insert_eval(
        self,
        *,
        candidate_id: int,
        suite_id: str,
        report_path: Path,
        passed: bool,
        metrics: dict[str, Any],
    ) -> int:
        self._require_candidate(candidate_id, context="eval")
        eval_id = _next_integer_id(self.connection, "evals")
        event = self.append_audit_event(
            "eval.recorded",
            {"eval_id": eval_id, "candidate_id": candidate_id},
        )
        self.connection.execute(
            """
            INSERT INTO evals(
              id, candidate_id, suite_id, report_path, passed, metrics_json,
              audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eval_id,
                candidate_id,
                suite_id,
                str(report_path),
                int(passed),
                json.dumps(metrics, sort_keys=True),
                event.sequence,
            ),
        )
        self.connection.commit()
        eval_run_event = self.append_audit_event(
            "eval_run.recorded",
            {
                "candidate_id": candidate_id,
                "suite_id": suite_id,
                "status": "passed" if passed else "failed",
            },
        )
        self.connection.execute(
            """
            INSERT INTO eval_runs(candidate_id, suite_id, status, report_path, audit_event_sequence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                suite_id,
                "passed" if passed else "failed",
                str(report_path),
                eval_run_event.sequence,
            ),
        )
        self.connection.commit()
        return eval_id

    def record_eval_case(
        self,
        *,
        suite_id: str,
        case_id: str,
        case_hash: str,
    ) -> int:
        event = self.append_audit_event(
            "eval_case.recorded",
            {
                "suite_id": suite_id,
                "case_id": case_id,
                "case_hash": case_hash,
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO eval_cases(suite_id, case_id, case_hash, audit_event_sequence)
            VALUES (?, ?, ?, ?)
            """,
            (suite_id, case_id, case_hash, event.sequence),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_validation_split(
        self,
        *,
        suite_id: str,
        split_name: str,
        case_ids: tuple[str, ...],
    ) -> int:
        event = self.append_audit_event(
            "validation_split.recorded",
            {
                "suite_id": suite_id,
                "split_name": split_name,
                "case_count": len(case_ids),
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO validation_splits(
              suite_id, split_name, case_ids_json, audit_event_sequence
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                suite_id,
                split_name,
                json.dumps(list(case_ids), sort_keys=True),
                event.sequence,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def insert_decision(
        self,
        *,
        candidate_id: int,
        actor: str,
        policy: str,
        decision: str,
        reason: str,
        applied_commit: str = "",
        rollback_ref: str = "",
    ) -> int:
        self._require_candidate(candidate_id, context="decision")
        decision_id = _next_integer_id(self.connection, "decisions")
        event = self.append_audit_event(
            "decision.recorded",
            {"decision_id": decision_id, "candidate_id": candidate_id},
        )
        self.connection.execute(
            """
            INSERT INTO decisions(
              id, candidate_id, actor, policy, decision, reason, created_at,
              applied_commit, rollback_ref, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision_id,
                candidate_id,
                actor,
                policy,
                decision,
                reason,
                _now(),
                applied_commit,
                rollback_ref,
                event.sequence,
            ),
        )
        self.connection.commit()
        return decision_id

    def record_review_action(
        self,
        *,
        candidate_id: int,
        actor: str,
        action: str,
        reason: str,
    ) -> int:
        self._require_candidate(candidate_id, context="review_action")
        event = self.append_audit_event(
            "review_action.recorded",
            {
                "candidate_id": candidate_id,
                "actor": actor,
                "action": action,
                "reason": reason,
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO review_actions(candidate_id, actor, action, reason, audit_event_sequence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (candidate_id, actor, action, reason, event.sequence),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_harness_finding(
        self,
        *,
        repo_path: Path,
        finding: str,
        severity: str,
    ) -> int:
        event = self.append_audit_event(
            "harness_finding.recorded",
            {
                "repo": str(repo_path),
                "finding": finding,
                "severity": severity,
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO harness_findings(
              repo_path, finding, severity, audit_event_sequence
            )
            VALUES (?, ?, ?, ?)
            """,
            (str(repo_path), finding, severity, event.sequence),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_doc_gardening_run(
        self,
        *,
        repo_path: Path,
        status: str,
        report_path: Path,
    ) -> int:
        event = self.append_audit_event(
            "doc_gardening_run.recorded",
            {
                "repo": str(repo_path),
                "status": status,
                "report_path": str(report_path),
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO doc_gardening_runs(
              repo_path, status, report_path, audit_event_sequence
            )
            VALUES (?, ?, ?, ?)
            """,
            (str(repo_path), status, str(report_path), event.sequence),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_daemon_job(
        self,
        *,
        job_id: str,
        repo_path: Path,
        state: str,
        payload: dict[str, Any],
    ) -> int:
        event = self.append_audit_event(
            "daemon_job.recorded",
            {
                "job_id": job_id,
                "repo": str(repo_path),
                "state": state,
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO daemon_jobs(
              job_id, repo_path, state, payload_json, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                job_id,
                str(repo_path),
                state,
                json.dumps(payload, sort_keys=True),
                event.sequence,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def update_daemon_job_state(
        self,
        *,
        job_id: str,
        repo_path: Path,
        state: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        row = self.connection.execute(
            """
            SELECT 1 FROM daemon_jobs
            WHERE job_id = ? AND repo_path = ?
            """,
            (job_id, str(repo_path)),
        ).fetchone()
        event = self.append_audit_event(
            "daemon_job.state_changed",
            {
                "job_id": job_id,
                "repo": str(repo_path),
                "state": state,
            },
        )
        if row is None:
            if payload is None:
                return
            self.connection.execute(
                """
                INSERT INTO daemon_jobs(
                  job_id, repo_path, state, payload_json, audit_event_sequence
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    str(repo_path),
                    state,
                    json.dumps(payload, sort_keys=True),
                    event.sequence,
                ),
            )
            self.connection.commit()
            return
        self.connection.execute(
            """
            UPDATE daemon_jobs
            SET state = ?, audit_event_sequence = ?
            WHERE job_id = ? AND repo_path = ?
            """,
            (state, event.sequence, job_id, str(repo_path)),
        )
        self.connection.commit()

    def record_mcp_call(
        self,
        *,
        tool_name: str,
        repo_path: Path,
        status: str,
        payload: dict[str, Any],
    ) -> int:
        event = self.append_audit_event("mcp.tool_called", payload)
        cursor = self.connection.execute(
            """
            INSERT INTO mcp_calls(tool_name, repo_path, status, payload_json, audit_event_sequence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                tool_name,
                str(repo_path),
                status,
                json.dumps(payload, sort_keys=True),
                event.sequence,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_optimizer_memory(
        self,
        *,
        repo_path: str,
        memory_type: str,
        key: str,
        payload: dict[str, Any],
    ) -> int:
        event = self.append_audit_event(
            "optimizer_memory.recorded",
            {
                "repo": repo_path,
                "memory_type": memory_type,
                "key": key,
            },
        )
        self.connection.execute(
            """
            DELETE FROM optimizer_memory
            WHERE repo_path = ? AND memory_type = ? AND key = ?
            """,
            (repo_path, memory_type, key),
        )
        cursor = self.connection.execute(
            """
            INSERT INTO optimizer_memory(repo_path, memory_type, key, payload_json, audit_event_sequence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                repo_path,
                memory_type,
                key,
                json.dumps(payload, sort_keys=True),
                event.sequence,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_reflection(
        self,
        *,
        run_id: str,
        source_ref: str,
        artifact_path: Path,
    ) -> int:
        self._require_run(run_id, context="reflection")
        reflection_hash = _file_hash(artifact_path)
        event = self.append_audit_event(
            "reflection.recorded",
            {
                "run_id": run_id,
                "source_ref": source_ref,
                "reflection_hash": reflection_hash,
                "artifact_path": str(artifact_path),
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO reflections(
              run_id, source_ref, reflection_hash, artifact_path, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (run_id, source_ref, reflection_hash, str(artifact_path), event.sequence),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_edit_operation(
        self,
        *,
        candidate_id: int,
        operator: str,
        target_path: str,
        payload: dict[str, Any],
    ) -> int:
        self._require_candidate(candidate_id, context="edit_operation")
        event = self.append_audit_event(
            "edit_operation.recorded",
            {
                "candidate_id": candidate_id,
                "operator": operator,
                "target_path": target_path,
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO edit_operations(
              candidate_id, operator, target_path, payload_json, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                operator,
                target_path,
                json.dumps(payload, sort_keys=True),
                event.sequence,
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def record_rollback(
        self,
        *,
        decision_id: str,
        candidate_id: int,
        reason: str,
        revert_commit: str,
        post_rollback_eval_result: dict[str, Any],
        rollback_plan: str,
        executed: bool,
    ) -> int:
        self._require_candidate(candidate_id, context="rollback")
        rollback_id = _next_integer_id(self.connection, "rollbacks")
        event = self.append_audit_event(
            "rollback.recorded",
            {
                "rollback_id": rollback_id,
                "decision_id": decision_id,
                "candidate_id": candidate_id,
                "rollback_plan": rollback_plan,
                "executed": executed,
            },
        )
        self.connection.execute(
            """
            INSERT INTO rollbacks(
              id, decision_id, candidate_id, reason, revert_commit,
              post_rollback_eval_result_json, rollback_plan, executed,
              audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rollback_id,
                decision_id,
                candidate_id,
                reason,
                revert_commit,
                json.dumps(post_rollback_eval_result, sort_keys=True),
                rollback_plan,
                int(executed),
                event.sequence,
            ),
        )
        self.connection.commit()
        return rollback_id

    def record_candidate_edit(
        self,
        *,
        candidate_id: int,
        edit_operation_id: int,
        target_path: str,
        risk_class: str,
    ) -> int:
        self._require_candidate(candidate_id, context="candidate_edit")
        self._require_edit_operation(edit_operation_id, context="candidate_edit")
        event = self.append_audit_event(
            "candidate_edit.recorded",
            {
                "candidate_id": candidate_id,
                "edit_operation_id": edit_operation_id,
                "target_path": target_path,
                "risk_class": risk_class,
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO candidate_edits(
              candidate_id, edit_operation_id, target_path, risk_class, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (candidate_id, edit_operation_id, target_path, risk_class, event.sequence),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def append_audit_event(self, event_type: str, payload: dict[str, Any]) -> AuditEvent:
        previous = self.connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = previous[0] if previous else ""
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        event_hash = hashlib.sha256(
            f"{previous_hash}\n{event_type}\n{payload_json}".encode("utf-8")
        ).hexdigest()
        cursor = self.connection.execute(
            """
            INSERT INTO audit_events(event_type, payload_json, previous_hash, event_hash)
            VALUES (?, ?, ?, ?)
            """,
            (event_type, payload_json, previous_hash, event_hash),
        )
        self.connection.commit()
        return AuditEvent(
            sequence=int(cursor.lastrowid),
            event_type=event_type,
            payload=payload,
            previous_hash=previous_hash,
            event_hash=event_hash,
        )

    def update_audit_event(self, sequence: int, changes: dict[str, Any]) -> None:
        raise PermissionError("audit events are append-only")

    def close(self) -> None:
        self.connection.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _is_daemon_queue_database(connection: sqlite3.Connection) -> bool:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(daemon_jobs)").fetchall()
    }
    return {
        "kind",
        "payload_json",
        "state",
        "attempts",
        "created_at",
        "updated_at",
    }.issubset(columns) and "repo_path" not in columns


def _next_integer_id(connection: sqlite3.Connection, table: str) -> int:
    row = connection.execute(
        f"SELECT COALESCE(MAX(id), 0) + 1 FROM {_quote_identifier(table)}"
    ).fetchone()
    return int(row[0])


def _repair_audit_event_constraints(connection: sqlite3.Connection) -> None:
    for table in AUDITED_PROVENANCE_TABLES:
        if not _requires_audit_event_constraint_repair(connection, table):
            continue
        _validate_audit_event_reachability(connection, table)
        _rebuild_table_with_audit_event_constraint(connection, table)
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ValueError(f"foreign key check failed after audit schema repair: {violations!r}")


def _requires_audit_event_constraint_repair(connection: sqlite3.Connection, table: str) -> bool:
    columns = {
        str(row[1]): row
        for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    }
    column = columns.get("audit_event_sequence")
    if column is None or int(column[3]) != 1:
        return True
    return not any(
        str(row[2]) == "audit_events"
        and str(row[3]) == "audit_event_sequence"
        and str(row[4]) == "sequence"
        for row in connection.execute(f"PRAGMA foreign_key_list({_quote_identifier(table)})").fetchall()
    )


def _backfill_runs_audit_event_sequence(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(runs)").fetchall()
    }
    if "audit_event_sequence" not in columns:
        return
    rows = connection.execute(
        """
        SELECT id, stage, status
        FROM runs
        WHERE audit_event_sequence IS NULL
        ORDER BY created_at, id
        """
    ).fetchall()
    for row in rows:
        event = connection.execute(
            """
            SELECT sequence
            FROM audit_events
            WHERE event_type = 'run.recorded'
              AND json_extract(payload_json, '$.run_id') = ?
              AND json_extract(payload_json, '$.stage') = ?
              AND json_extract(payload_json, '$.status') = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (str(row[0]), str(row[1]), str(row[2])),
        ).fetchone()
        if event is None:
            continue
        connection.execute(
            """
            UPDATE runs
            SET audit_event_sequence = ?
            WHERE id = ?
            """,
            (int(event[0]), str(row[0])),
        )


def _backfill_instruction_index_audit_event_sequence(connection: sqlite3.Connection) -> None:
    document_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(documents)").fetchall()
    }
    chunk_columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(chunks)").fetchall()
    }
    if "audit_event_sequence" not in document_columns or "audit_event_sequence" not in chunk_columns:
        return

    document_rows = connection.execute(
        """
        SELECT id, repo_path, path, kind, hash
        FROM documents
        WHERE audit_event_sequence IS NULL
        ORDER BY id
        """
    ).fetchall()
    for row in document_rows:
        event = _append_audit_event(
            connection,
            "document.indexed",
            {
                "repo": str(row[1]),
                "path": str(row[2]),
                "kind": str(row[3]),
                "hash": str(row[4]),
                "migration": True,
            },
        )
        connection.execute(
            "UPDATE documents SET audit_event_sequence = ? WHERE id = ?",
            (event.sequence, int(row[0])),
        )

    chunk_rows = connection.execute(
        """
        SELECT c.id, c.document_id, c.anchor, c.text_hash, d.repo_path, d.path
        FROM chunks c
        LEFT JOIN documents d ON d.id = c.document_id
        WHERE c.audit_event_sequence IS NULL
        ORDER BY c.id
        """
    ).fetchall()
    for row in chunk_rows:
        event = _append_audit_event(
            connection,
            "instruction_chunk.indexed",
            {
                "repo": str(row[4] or ""),
                "path": str(row[5] or ""),
                "document_id": int(row[1]),
                "anchor": str(row[2]),
                "text_hash": str(row[3]),
                "migration": True,
            },
        )
        connection.execute(
            "UPDATE chunks SET audit_event_sequence = ? WHERE id = ?",
            (event.sequence, int(row[0])),
        )


def _backfill_episodes_audit_event_sequence(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(episodes)").fetchall()
    }
    if "audit_event_sequence" not in columns:
        return

    rows = connection.execute(
        """
        SELECT id, repo_path, trace_path, outcome, summary_hash
        FROM episodes
        WHERE audit_event_sequence IS NULL
        ORDER BY id
        """
    ).fetchall()
    for row in rows:
        event = connection.execute(
            """
            SELECT sequence
            FROM audit_events
            WHERE event_type = 'episode.recorded'
              AND json_extract(payload_json, '$.episode_id') = ?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (int(row[0]),),
        ).fetchone()
        if event is None:
            audit_event = _append_audit_event(
                connection,
                "episode.recorded",
                {
                    "episode_id": int(row[0]),
                    "repo": str(row[1]),
                    "trace_path": str(row[2]),
                    "outcome": str(row[3]),
                    "summary_hash": str(row[4]),
                    "migration": True,
                },
            )
            sequence = audit_event.sequence
        else:
            sequence = int(event[0])
        connection.execute(
            "UPDATE episodes SET audit_event_sequence = ? WHERE id = ?",
            (sequence, int(row[0])),
        )


def _validate_audit_event_reachability(connection: sqlite3.Connection, table: str) -> None:
    quoted = _quote_identifier(table)
    null_count = int(
        connection.execute(
            f"SELECT COUNT(*) FROM {quoted} WHERE audit_event_sequence IS NULL"
        ).fetchone()[0]
    )
    if null_count:
        raise ValueError(f"{table}.audit_event_sequence contains NULL")
    orphan_count = int(
        connection.execute(
            f"""
            SELECT COUNT(*)
            FROM {quoted} row
            LEFT JOIN audit_events audit
              ON audit.sequence = row.audit_event_sequence
            WHERE audit.sequence IS NULL
            """
        ).fetchone()[0]
    )
    if orphan_count:
        raise ValueError(f"{table}.audit_event_sequence has orphaned values")


def _rebuild_table_with_audit_event_constraint(connection: sqlite3.Connection, table: str) -> None:
    temporary = f"__{table}_audit_migration"
    quoted_table = _quote_identifier(table)
    quoted_temporary = _quote_identifier(temporary)
    columns = connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    if not columns:
        return
    column_definitions = ",\n  ".join(_column_definition(row) for row in columns)
    column_names = ", ".join(_quote_identifier(str(row[1])) for row in columns)

    connection.execute(f"DROP TABLE IF EXISTS {quoted_temporary}")
    connection.execute(f"ALTER TABLE {quoted_table} RENAME TO {quoted_temporary}")
    connection.execute(f"CREATE TABLE {quoted_table} (\n  {column_definitions}\n)")
    connection.execute(
        f"INSERT INTO {quoted_table} ({column_names}) SELECT {column_names} FROM {quoted_temporary}"
    )
    connection.execute(f"DROP TABLE {quoted_temporary}")


def _column_definition(row: sqlite3.Row | tuple[Any, ...]) -> str:
    name = str(row[1])
    if name == "audit_event_sequence":
        return (
            f"{_quote_identifier(name)} INTEGER NOT NULL "
            "REFERENCES audit_events(sequence)"
        )
    column_type = str(row[2] or "")
    parts = [_quote_identifier(name)]
    if column_type:
        parts.append(column_type)
    if int(row[5]):
        parts.append("PRIMARY KEY")
    elif int(row[3]):
        parts.append("NOT NULL")
    if row[4] is not None:
        parts.append(f"DEFAULT {row[4]}")
    return " ".join(parts)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _append_audit_event(
    connection: sqlite3.Connection,
    event_type: str,
    payload: dict[str, Any],
) -> AuditEvent:
    previous = connection.execute(
        "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    previous_hash = previous[0] if previous else ""
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    event_hash = hashlib.sha256(
        f"{previous_hash}\n{event_type}\n{payload_json}".encode("utf-8")
    ).hexdigest()
    cursor = connection.execute(
        """
        INSERT INTO audit_events(event_type, payload_json, previous_hash, event_hash)
        VALUES (?, ?, ?, ?)
        """,
        (event_type, payload_json, previous_hash, event_hash),
    )
    return AuditEvent(
        sequence=int(cursor.lastrowid),
        event_type=event_type,
        payload=payload,
        previous_hash=previous_hash,
        event_hash=event_hash,
    )


def _jsonl_payloads(path: Path) -> tuple[dict[str, Any], ...]:
    if not path.exists():
        return ()
    payloads: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
    return tuple(payloads)


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
