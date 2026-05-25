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
  parser_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL,
  heading_path TEXT NOT NULL,
  anchor TEXT NOT NULL,
  byte_start INTEGER NOT NULL,
  byte_end INTEGER NOT NULL,
  text_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  trace_path TEXT NOT NULL,
  started_at TEXT NOT NULL,
  outcome TEXT NOT NULL,
  summary_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY,
  episode_id INTEGER,
  stage TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  run_dir TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audits (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  failure_class TEXT NOT NULL,
  severity TEXT NOT NULL,
  confidence REAL NOT NULL,
  evidence_json TEXT NOT NULL,
  instruction_refs_json TEXT NOT NULL
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
  state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evals (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  suite_id TEXT NOT NULL,
  report_path TEXT NOT NULL,
  passed INTEGER NOT NULL,
  metrics_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  actor TEXT NOT NULL,
  policy TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  previous_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trace_events (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER,
  evidence_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  line_number INTEGER NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS instruction_snapshots (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS instruction_graphs (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  graph_hash TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS llmff_jobs (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  manifest_name TEXT NOT NULL,
  manifest_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS llmff_events (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS llmff_outputs (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL,
  output_name TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS reflections (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  reflection_hash TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS edit_operations (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  operator TEXT NOT NULL,
  target_path TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS candidate_edits (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  edit_operation_id INTEGER NOT NULL,
  target_path TEXT NOT NULL,
  risk_class TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS eval_cases (
  id INTEGER PRIMARY KEY,
  suite_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  case_hash TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  suite_id TEXT NOT NULL,
  status TEXT NOT NULL,
  report_path TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS validation_splits (
  id INTEGER PRIMARY KEY,
  suite_id TEXT NOT NULL,
  split_name TEXT NOT NULL,
  case_ids_json TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS review_actions (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS mcp_calls (
  id INTEGER PRIMARY KEY,
  tool_name TEXT NOT NULL,
  repo_path TEXT NOT NULL,
  status TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS daemon_jobs (
  id INTEGER PRIMARY KEY,
  job_id TEXT NOT NULL,
  repo_path TEXT NOT NULL,
  state TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS harness_findings (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  finding TEXT NOT NULL,
  severity TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS doc_gardening_runs (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  status TEXT NOT NULL,
  report_path TEXT NOT NULL,
  audit_event_sequence INTEGER
);
CREATE TABLE IF NOT EXISTS optimizer_memory (
  id INTEGER PRIMARY KEY,
  repo_path TEXT NOT NULL,
  memory_type TEXT NOT NULL,
  key TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  audit_event_sequence INTEGER
);
"""


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
        connection.executescript(SCHEMA)
        connection.commit()
        return cls(connection)

    def table_names(self) -> set[str]:
        rows = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return {row[0] for row in rows}

    def count(self, table: str) -> int:
        if table not in self.table_names():
            raise ValueError(f"unknown table: {table}")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def index_documents(self, repo: Path, result: IndexResult) -> None:
        repo_path = str(repo)
        self.connection.execute("DELETE FROM chunks WHERE document_id IN (SELECT id FROM documents WHERE repo_path = ?)", (repo_path,))
        self.connection.execute("DELETE FROM documents WHERE repo_path = ?", (repo_path,))
        for document in result.documents:
            cursor = self.connection.execute(
                """
                INSERT INTO documents(repo_path, path, kind, precedence, protected, hash, mtime, parser_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                ),
            )
            document_id = int(cursor.lastrowid)
            for chunk in document.chunks:
                self.connection.execute(
                    """
                    INSERT INTO chunks(document_id, heading_path, anchor, byte_start, byte_end, text_hash)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        json.dumps(list(chunk.heading_path), sort_keys=True),
                        chunk.anchor,
                        chunk.byte_start,
                        chunk.byte_end,
                        chunk.text_hash,
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
        self.connection.execute(
            """
            INSERT OR REPLACE INTO runs(id, episode_id, stage, manifest_hash, status, run_dir, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM runs WHERE id = ?), ?), ?)
            """,
            (run_id, episode_id, stage, manifest_hash, status, str(run_dir), run_id, now, now),
        )
        self.connection.commit()
        self.append_audit_event("run.recorded", {"run_id": run_id, "stage": stage, "status": status})

    def record_llmff_run(
        self,
        *,
        run_id: str,
        manifest_hash: str,
        result: RunResult,
    ) -> int:
        status = "completed" if result.exit_code == 0 else "failed"
        job_event = self.append_audit_event(
            "llmff_job.recorded",
            {
                "run_id": run_id,
                "manifest_name": result.manifest_path.name,
                "status": status,
                "exit_code": result.exit_code,
            },
        )
        cursor = self.connection.execute(
            """
            INSERT INTO llmff_jobs(run_id, manifest_name, manifest_hash, status, audit_event_sequence)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                result.manifest_path.name,
                manifest_hash,
                status,
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

    def record_trace_episode(self, *, repo: Path, bundle: TraceBundle) -> int:
        summary_hash = hashlib.sha256(
            json.dumps(
                [event.evidence_id for event in bundle.events],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cursor = self.connection.execute(
            """
            INSERT INTO episodes(repo_path, trace_path, started_at, outcome, summary_hash)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(repo), str(bundle.trace_path), _now(), "captured", summary_hash),
        )
        episode_id = int(cursor.lastrowid)
        self.connection.commit()
        self.append_audit_event(
            "episode.recorded",
            {
                "episode_id": episode_id,
                "repo": str(repo),
                "trace_path": str(bundle.trace_path),
                "events": len(bundle.events),
            },
        )
        for event in bundle.events:
            audit_event = self.append_audit_event(
                "trace_event.recorded",
                {
                    "episode_id": episode_id,
                    "evidence_id": event.evidence_id,
                    "event_type": event.event_type,
                    "line_number": event.line_number,
                },
            )
            self.connection.execute(
                """
                INSERT INTO trace_events(
                  episode_id, evidence_id, event_type, line_number, payload_json, audit_event_sequence
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    episode_id,
                    event.evidence_id,
                    event.event_type,
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
        cursor = self.connection.execute(
            """
            INSERT INTO audits(run_id, failure_class, severity, confidence, evidence_json, instruction_refs_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                failure_class,
                severity,
                confidence,
                json.dumps(evidence_refs, sort_keys=True),
                json.dumps(instruction_refs, sort_keys=True),
            ),
        )
        self.connection.commit()
        audit_id = int(cursor.lastrowid)
        self.append_audit_event("audit.recorded", {"audit_id": audit_id, "run_id": run_id})
        return audit_id

    def insert_candidate(
        self,
        *,
        audit_id: int,
        candidate: CandidatePatch,
        diff_path: Path,
        state: str,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO candidates(audit_id, base_file, base_hash, diff_hash, diff_path, risk_class, rationale, state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                candidate.base_file,
                candidate.base_hash,
                candidate.diff_hash,
                str(diff_path),
                candidate.risk_class,
                candidate.rationale,
                state,
            ),
        )
        self.connection.commit()
        candidate_id = int(cursor.lastrowid)
        self.append_audit_event("candidate.recorded", {"candidate_id": candidate_id, "audit_id": audit_id})
        return candidate_id

    def insert_eval(
        self,
        *,
        candidate_id: int,
        suite_id: str,
        report_path: Path,
        passed: bool,
        metrics: dict[str, Any],
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO evals(candidate_id, suite_id, report_path, passed, metrics_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (candidate_id, suite_id, str(report_path), int(passed), json.dumps(metrics, sort_keys=True)),
        )
        self.connection.commit()
        eval_id = int(cursor.lastrowid)
        self.append_audit_event("eval.recorded", {"eval_id": eval_id, "candidate_id": candidate_id})
        return eval_id

    def insert_decision(
        self,
        *,
        candidate_id: int,
        actor: str,
        policy: str,
        decision: str,
        reason: str,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO decisions(candidate_id, actor, policy, decision, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (candidate_id, actor, policy, decision, reason, _now()),
        )
        self.connection.commit()
        decision_id = int(cursor.lastrowid)
        self.append_audit_event("decision.recorded", {"decision_id": decision_id, "candidate_id": candidate_id})
        return decision_id

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
