from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from tugboat import __version__
from tugboat.artifacts import validate_json_artifact
from tugboat.config import load_policy
from tugboat.corpus.indexer import index_repo
from tugboat.daemon.queue import DaemonQueue
from tugboat.daemon.service import daemon_status, default_kill_switch
from tugboat.db import Store
from tugboat.harness.checks import check_harness_legibility
from tugboat.ops.retention import apply_retention_policy
from tugboat.paths import ensure_private_dir, mark_private_file, runs_dir, sidecar_dir
from tugboat.report.decision_trace import write_decision_trace
from tugboat.security.redaction import redact_payload, redact_text
from tugboat.security.secrets import SecretScanError, scan_path
from tugboat.traces.adapters import ingest_mcp_session_bundle
from tugboat.traces.ingest import ingest_jsonl_trace


T = TypeVar("T")

RUN_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("audit", "audit.json"),
    ("candidate", "candidate.json"),
    ("candidate_diff", "candidate.diff"),
    ("eval_report", "eval-report.json"),
    ("optimization_summary", "optimization-summary.json"),
    ("policy_gate", "policy-gate.json"),
    ("report", "report.md"),
)

WRITE_INTENT_TOOLS = frozenset(
    {
        "tugboat_record_episode",
        "tugboat_request_audit",
        "tugboat_request_eval",
        "tugboat_request_proposal",
    }
)
READ_ONLY_EXCLUDED_TOOLS = frozenset({"tugboat_decision_trace"})


def _object_schema(
    properties: dict[str, dict[str, Any]],
    required: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "additionalProperties": False,
        "properties": properties,
        "required": list(required),
        "type": "object",
    }


_REPO_SCHEMA = {"type": "string"}
_INTEGER_ID_SCHEMA = {"type": "integer"}
_SAFE_MCP_ID_PATTERN_TEXT = r"^(?!\.\.?$)[A-Za-z0-9_.-]+$"
_SAFE_MCP_ID_PATTERN = re.compile(_SAFE_MCP_ID_PATTERN_TEXT)
_SAFE_MCP_SUITE_PATTERN_TEXT = r"^[A-Za-z0-9_.-]{1,64}$"
_SAFE_MCP_SUITE_PATTERN = re.compile(_SAFE_MCP_SUITE_PATTERN_TEXT)
_STRING_ID_SCHEMA = {"pattern": _SAFE_MCP_ID_PATTERN_TEXT, "type": "string"}
_DECIMAL_ID_SCHEMA = {"pattern": r"^[0-9]+$", "type": "string"}

MCP_TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "tugboat_active_instructions": _object_schema({"repo": _REPO_SCHEMA}, ("repo",)),
    "tugboat_candidate": _object_schema(
        {"repo": _REPO_SCHEMA, "candidate_id": _INTEGER_ID_SCHEMA},
        ("repo", "candidate_id"),
    ),
    "tugboat_candidate_report": _object_schema(
        {"repo": _REPO_SCHEMA, "candidate_id": _INTEGER_ID_SCHEMA},
        ("repo", "candidate_id"),
    ),
    "tugboat_daemon_status": _object_schema({"repo": _REPO_SCHEMA}, ("repo",)),
    "tugboat_decision_trace": _object_schema(
        {"repo": _REPO_SCHEMA, "decision": _STRING_ID_SCHEMA},
        ("repo", "decision"),
    ),
    "tugboat_harness_findings": _object_schema({"repo": _REPO_SCHEMA}, ("repo",)),
    "tugboat_index_summary": _object_schema({"repo": _REPO_SCHEMA}, ("repo",)),
    "tugboat_instruction_graph": _object_schema({"repo": _REPO_SCHEMA}, ("repo",)),
    "tugboat_latest_audit": _object_schema({"repo": _REPO_SCHEMA}, ("repo",)),
    "tugboat_latest_runs": _object_schema(
        {"repo": _REPO_SCHEMA, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
        ("repo",),
    ),
    "tugboat_record_episode": _object_schema(
        {"repo": _REPO_SCHEMA, "trace_jsonl": {"type": "string"}},
        ("repo", "trace_jsonl"),
    ),
    "tugboat_request_audit": _object_schema(
        {"repo": _REPO_SCHEMA, "trace_id": _STRING_ID_SCHEMA},
        ("repo", "trace_id"),
    ),
    "tugboat_request_eval": _object_schema(
        {
            "repo": _REPO_SCHEMA,
            "candidate_id": _DECIMAL_ID_SCHEMA,
            "suite": {"pattern": _SAFE_MCP_SUITE_PATTERN_TEXT, "type": "string"},
        },
        ("repo", "candidate_id", "suite"),
    ),
    "tugboat_request_proposal": _object_schema(
        {"repo": _REPO_SCHEMA, "audit_id": _DECIMAL_ID_SCHEMA},
        ("repo", "audit_id"),
    ),
    "tugboat_run_report": _object_schema(
        {"repo": _REPO_SCHEMA, "run_id": _STRING_ID_SCHEMA},
        ("repo", "run_id"),
    ),
    "tugboat_status": _object_schema({"repo": _REPO_SCHEMA}, ("repo",)),
}


def tugboat_status(repo: str | Path) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        policy = load_policy(repo_path)
        with Store.open(sidecar_dir(repo_path) / "db.sqlite") as store:
            latest = store.connection.execute(
                "SELECT id, stage, status FROM runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            latest_llmff = None
            latest_failure_kind = None
            if latest is not None:
                latest_llmff = store.connection.execute(
                    """
                    SELECT id, manifest_name, status, exit_code
                    FROM llmff_jobs
                    WHERE run_id = ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (latest[0],),
                ).fetchone()
                if latest_llmff is not None:
                    latest_failure_kind = _latest_llmff_failure_kind(store, int(latest_llmff[0]))
            pending_candidates = int(
                store.connection.execute(
                    "SELECT COUNT(*) FROM candidates WHERE state = 'needs_review'"
                ).fetchone()[0]
            )
            indexed_documents = store.count("documents")
        retention = apply_retention_policy(repo_path, policy, dry_run=True)
        manifest_policy = (
            f"pinned {len(policy.allowed_manifest_hashes)}"
            if policy.allowed_manifest_hashes
            else "unrestricted"
        )
        return {
            "mode": policy.mode,
            "auto_apply": "enabled" if policy.auto_apply_enabled else "disabled",
            "indexed_documents": indexed_documents,
            "latest_run": (
                {"run_id": str(latest[0]), "stage": str(latest[1]), "status": str(latest[2])}
                if latest
                else None
            ),
            "latest_llmff_job": (
                {"manifest_name": str(latest_llmff[1]), "status": str(latest_llmff[2])}
                if latest_llmff
                else None
            ),
            "latest_llmff_exit_code": (
                int(latest_llmff[3])
                if latest_llmff is not None and latest_llmff[3] is not None
                else None
            ),
            "latest_llmff_failure_kind": latest_failure_kind,
            "pending_candidates": pending_candidates,
            "retention_candidates": len(retention.candidates),
            "retention_redaction_candidates": len(retention.redaction_candidates),
            "manifest_policy": manifest_policy,
        }

    return _audit_call(repo_path, "tugboat_status", {}, read)


def _latest_llmff_failure_kind(store: Store, job_id: int) -> str | None:
    row = store.connection.execute(
        """
        SELECT payload_json
        FROM llmff_events
        WHERE job_id = ? AND event_type = 'run_failed'
        ORDER BY id DESC
        LIMIT 1
        """,
        (job_id,),
    ).fetchone()
    if row is not None:
        try:
            payload = json.loads(str(row[0]))
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            details = payload.get("run_failed")
            if isinstance(details, dict) and details.get("failure_kind"):
                return str(details["failure_kind"])
            failure_kind = payload.get("failure_kind")
            if failure_kind:
                return str(failure_kind)

    row = store.connection.execute(
        """
        SELECT ae.payload_json
        FROM llmff_jobs job
        JOIN audit_events ae ON ae.sequence = job.audit_event_sequence
        WHERE job.id = ? AND ae.event_type = 'llmff_job.recorded'
        """,
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    details = payload.get("run_failed")
    if isinstance(details, dict) and details.get("failure_kind"):
        return str(details["failure_kind"])
    return None


def tugboat_instruction_graph(repo: str | Path) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        result = index_repo(repo_path, load_policy(repo_path))
        return {
            "documents": [
                {
                    "path": document.path,
                    "kind": document.kind,
                    "precedence": document.precedence,
                    "protected": document.protected,
                    "hash": document.hash,
                    "parser_version": document.parser_version,
                    "chunk_count": len(document.chunks),
                    "chunks": [
                        {
                            "heading_path": list(chunk.heading_path),
                            "anchor": chunk.anchor,
                            "byte_start": chunk.byte_start,
                            "byte_end": chunk.byte_end,
                            "text_hash": chunk.text_hash,
                        }
                        for chunk in document.chunks
                    ],
                }
                for document in result.documents
            ]
        }

    return _audit_call(repo_path, "tugboat_instruction_graph", {}, read)


def tugboat_active_instructions(repo: str | Path) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        result = index_repo(repo_path, load_policy(repo_path))
        documents = sorted(result.documents, key=lambda document: (-document.precedence, document.path))
        return {
            "documents": [
                {
                    "path": document.path,
                    "kind": document.kind,
                    "precedence": document.precedence,
                    "protected": document.protected,
                    "active": True,
                    "hash": document.hash,
                    "chunk_count": len(document.chunks),
                    "refs": [
                        _instruction_chunk_ref(document.path, chunk.byte_start, chunk.byte_end)
                        for chunk in document.chunks
                    ],
                }
                for document in documents
            ]
        }

    return _audit_call(repo_path, "tugboat_active_instructions", {}, read)


def tugboat_index_summary(repo: str | Path) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        result = index_repo(repo_path, load_policy(repo_path))
        documents = sorted(result.documents, key=lambda document: (-document.precedence, document.path))
        return {
            "indexed_documents": len(documents),
            "indexed_chunks": sum(len(document.chunks) for document in documents),
            "protected_documents": sum(1 for document in documents if document.protected),
            "documents": [
                {
                    "path": document.path,
                    "kind": document.kind,
                    "precedence": document.precedence,
                    "protected": document.protected,
                    "hash": document.hash,
                    "chunk_count": len(document.chunks),
                    "refs": [
                        _instruction_chunk_ref(document.path, chunk.byte_start, chunk.byte_end)
                        for chunk in document.chunks
                    ],
                }
                for document in documents
            ],
        }

    return _audit_call(repo_path, "tugboat_index_summary", {}, read)


def tugboat_harness_findings(repo: str | Path) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        result = check_harness_legibility(repo_path)
        return {
            "passed": result.passed,
            "findings": [_sanitize_harness_finding(finding) for finding in result.findings],
        }

    return _audit_call(repo_path, "tugboat_harness_findings", {}, read)


def tugboat_latest_runs(repo: str | Path, limit: int = 10) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)
    normalized_limit = _normalize_limit(limit)

    def read() -> dict[str, Any]:
        with Store.open(sidecar_dir(repo_path) / "db.sqlite") as store:
            rows = store.connection.execute(
                """
                SELECT id, stage, status, created_at, updated_at, run_dir
                FROM runs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (normalized_limit,),
            ).fetchall()
        return {
            "runs": [
                {
                    "run_id": str(row[0]),
                    "stage": str(row[1]),
                    "status": str(row[2]),
                    "created_at": str(row[3]),
                    "updated_at": str(row[4]),
                    "artifacts": _run_artifact_refs(repo_path, _stored_path(repo_path, row[5])),
                }
                for row in rows
            ]
        }

    return _audit_call(repo_path, "tugboat_latest_runs", {"limit": normalized_limit}, read)


def tugboat_latest_audit(repo: str | Path) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        with Store.open(sidecar_dir(repo_path) / "db.sqlite") as store:
            row = store.connection.execute(
                """
                SELECT id, status, run_dir
                FROM runs
                WHERE stage = 'audit'
                ORDER BY created_at DESC, updated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return {"audit": None}
        run_dir = _stored_path(repo_path, row[2])
        audit = {
            "run": {"run_id": str(row[0]), "status": str(row[1])},
            "artifacts": _run_artifact_refs(repo_path, run_dir),
        }
        summary = _audit_artifact_summary(run_dir / "audit.json")
        if summary is not None:
            audit["summary"] = summary
        return {"audit": audit}

    return _audit_call(repo_path, "tugboat_latest_audit", {}, read)


def tugboat_run_report(repo: str | Path, run_id: str) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)
    run_dir = _run_dir(repo_path, run_id)

    def read() -> dict[str, Any]:
        with Store.open(sidecar_dir(repo_path) / "db.sqlite") as store:
            row = store.connection.execute(
                "SELECT id, stage, status FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown run_id: {run_id}")
        result: dict[str, Any] = {
            "run": {"run_id": str(row[0]), "stage": str(row[1]), "status": str(row[2])},
            "artifacts": _run_artifact_refs(repo_path, run_dir),
        }
        audit_summary = _audit_artifact_summary(run_dir / "audit.json")
        if audit_summary is not None:
            result["audit"] = audit_summary
        return result

    return _audit_call(repo_path, "tugboat_run_report", {"run_id": run_id}, read)


def tugboat_candidate(repo: str | Path, candidate_id: int) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        with Store.open(sidecar_dir(repo_path) / "db.sqlite") as store:
            row = store.connection.execute(
                """
                SELECT id, audit_id, base_file, diff_path, risk_class, rationale, state
                FROM candidates
                WHERE id = ?
                """,
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown candidate_id: {candidate_id}")
        diff_path = _stored_path(repo_path, row[3])
        return {
            "candidate_id": int(row[0]),
            "audit_id": int(row[1]),
            "base_file": str(row[2]),
            "risk_class": str(row[4]),
            "state": str(row[6]),
            "rationale_summary": _summarize_text(str(row[5])),
            "artifacts": [{"kind": "candidate_diff", "path": _relative_ref(repo_path, diff_path)}],
        }

    return _audit_call(
        repo_path,
        "tugboat_candidate",
        {"candidate_id": int(candidate_id)},
        read,
    )


def tugboat_candidate_report(repo: str | Path, candidate_id: int) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        with Store.open(sidecar_dir(repo_path) / "db.sqlite") as store:
            candidate_row = store.connection.execute(
                """
                SELECT id, audit_id, base_file, diff_path, risk_class, rationale, state
                FROM candidates
                WHERE id = ?
                """,
                (candidate_id,),
            ).fetchone()
            eval_row = store.connection.execute(
                """
                SELECT suite_id, passed, report_path
                FROM evals
                WHERE candidate_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            decision_row = store.connection.execute(
                """
                SELECT actor, policy, decision, reason
                FROM decisions
                WHERE candidate_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
        if candidate_row is None:
            raise ValueError(f"unknown candidate_id: {candidate_id}")
        diff_path = _stored_path(repo_path, candidate_row[3])
        report: dict[str, Any] = {
            "candidate": {
                "candidate_id": int(candidate_row[0]),
                "audit_id": int(candidate_row[1]),
                "base_file": str(candidate_row[2]),
                "risk_class": str(candidate_row[4]),
                "state": str(candidate_row[6]),
                "rationale_summary": _summarize_text(str(candidate_row[5])),
            },
            "latest_eval": None,
            "latest_decision": None,
            "artifacts": [{"kind": "candidate_diff", "path": _relative_ref(repo_path, diff_path)}],
        }
        if eval_row is not None:
            report["latest_eval"] = {
                "suite_id": str(eval_row[0]),
                "passed": bool(eval_row[1]),
                "artifact": {
                    "kind": "eval_report",
                    "path": _relative_ref(repo_path, _stored_path(repo_path, eval_row[2])),
                },
            }
        if decision_row is not None:
            report["latest_decision"] = {
                "actor": redact_text(str(decision_row[0])),
                "policy": str(decision_row[1]),
                "decision": str(decision_row[2]),
                "reason_summary": _summarize_text(str(decision_row[3])),
            }
        return report

    return _audit_call(
        repo_path,
        "tugboat_candidate_report",
        {"candidate_id": int(candidate_id)},
        read,
    )


def tugboat_decision_trace(repo: str | Path, decision: str) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        path = write_decision_trace(repo_path, decision)
        return {
            "decision_ref": decision,
            "artifact": {
                "kind": "decision_trace",
                "path": _relative_ref(repo_path, path),
                "sha256": _sha256(path),
            },
        }

    return _audit_call(repo_path, "tugboat_decision_trace", {"decision": decision}, read)


def tugboat_daemon_status(repo: str | Path) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        return daemon_status(repo_path, kill_switch=default_kill_switch(repo_path))

    return _audit_call(repo_path, "tugboat_daemon_status", {}, read)


def tugboat_record_episode(repo: str | Path, trace_jsonl: str) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def write() -> dict[str, Any]:
        trace_id = f"mcp-trace-{_stamp()}"
        mcp_dir = sidecar_dir(repo_path) / "mcp"
        episode_dir = mcp_dir / "episodes"
        ensure_private_dir(sidecar_dir(repo_path))
        ensure_private_dir(mcp_dir)
        ensure_private_dir(episode_dir)
        path = episode_dir / f"{trace_id}.jsonl"
        path.write_text(trace_jsonl, encoding="utf-8")
        mark_private_file(path)
        try:
            scan_path(path)
        except SecretScanError as error:
            path.unlink(missing_ok=True)
            raise ValueError("secret detected in episode payload") from error
        trace_format = _detect_episode_trace_format(path)
        try:
            bundle = (
                ingest_mcp_session_bundle(path)
                if trace_format == "mcp"
                else ingest_jsonl_trace(path)
            )
        except (json.JSONDecodeError, ValueError) as error:
            path.unlink(missing_ok=True)
            raise ValueError("invalid episode JSONL payload") from error
        metadata_path = _episode_trace_metadata_path(path)
        metadata_path.write_text(
            json.dumps(
                {
                    "trace_id": trace_id,
                    "trace_format": trace_format,
                    "trace_path": path.as_posix(),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        mark_private_file(metadata_path)
        with Store.open(sidecar_dir(repo_path) / "db.sqlite") as store:
            episode_id = store.record_trace_episode(repo=repo_path, bundle=bundle)
        return {
            "episode_id": episode_id,
            "trace_id": trace_id,
            "trace_format": trace_format,
            "artifact_ref": _relative_ref(repo_path, path),
        }

    return _audit_call(
        repo_path,
        "tugboat_record_episode",
        {"trace_jsonl": "[artifact-payload]"},
        write,
    )


def _looks_like_mcp_live_event_jsonl(path: Path) -> bool:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError("trace line must be a JSON object")
            return "event" in payload and "type" not in payload
    return False


def _detect_episode_trace_format(path: Path) -> str:
    return "mcp" if _looks_like_mcp_live_event_jsonl(path) else "generic-jsonl"


def _episode_trace_metadata_path(trace_path: Path) -> Path:
    return trace_path.with_suffix(".json")


def _read_episode_trace_format(trace_path: Path) -> str:
    if not trace_path.exists():
        return "generic-jsonl"
    metadata_path = _episode_trace_metadata_path(trace_path)
    if metadata_path.exists():
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("episode trace metadata must be a JSON object")
        trace_format = payload.get("trace_format")
        if not isinstance(trace_format, str) or not trace_format:
            raise ValueError("episode trace metadata missing trace_format")
        return trace_format
    return _detect_episode_trace_format(trace_path)


def tugboat_request_audit(repo: str | Path, trace_id: str) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)
    trace_path = sidecar_dir(repo_path) / "mcp" / "episodes" / f"{trace_id}.jsonl"
    trace_format = _read_episode_trace_format(trace_path)
    queue_payload = {
        "trace_path": str(trace_path),
        "trace_artifact_ref": _relative_ref(repo_path, trace_path),
        "trace_format": trace_format,
    }

    def validate_trace() -> None:
        _validate_mcp_artifact_id("trace_id", trace_id)
        if not trace_path.is_file():
            raise ValueError(f"unknown trace_id: {trace_id}")

    return _write_request_artifact(
        repo_path,
        tool="tugboat_request_audit",
        kind="audit",
        payload={"trace_id": trace_id, "trace_format": trace_format},
        queue_kind="trace_audit",
        queue_payload=queue_payload,
        preflight=validate_trace,
    )


def tugboat_request_proposal(repo: str | Path, audit_id: str) -> dict[str, Any]:
    def validate_audit_id() -> None:
        _validate_mcp_artifact_id("audit_id", audit_id)
        numeric_audit_id = _parse_mcp_integer_id("audit_id", audit_id)
        with Store.open(sidecar_dir(_resolve_local_repo(repo)) / "db.sqlite") as store:
            row = store.connection.execute(
                "SELECT 1 FROM audits WHERE id = ?",
                (numeric_audit_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown audit_id: {audit_id}")

    return _write_request_artifact(
        repo,
        tool="tugboat_request_proposal",
        kind="proposal",
        payload={"audit_id": audit_id},
        preflight=validate_audit_id,
    )


def tugboat_request_eval(repo: str | Path, candidate_id: str, suite: str) -> dict[str, Any]:
    def validate_candidate_id() -> None:
        _validate_mcp_artifact_id("candidate_id", candidate_id)
        _validate_mcp_suite_id(suite)
        numeric_candidate_id = _parse_mcp_integer_id("candidate_id", candidate_id)
        with Store.open(sidecar_dir(_resolve_local_repo(repo)) / "db.sqlite") as store:
            row = store.connection.execute(
                "SELECT 1 FROM candidates WHERE id = ?",
                (numeric_candidate_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown candidate_id: {candidate_id}")

    return _write_request_artifact(
        repo,
        tool="tugboat_request_eval",
        kind="eval",
        payload={"candidate_id": candidate_id, "suite": suite},
        preflight=validate_candidate_id,
    )


def list_mcp_tools(*, bound_repo: Path | None = None, read_only: bool = False) -> list[dict[str, Any]]:
    tool_names = sorted(MCP_TOOLS)
    if read_only:
        excluded_tools = WRITE_INTENT_TOOLS | READ_ONLY_EXCLUDED_TOOLS
        tool_names = [name for name in tool_names if name not in excluded_tools]
    return [
        {
            "inputSchema": _mcp_display_schema(
                MCP_TOOL_INPUT_SCHEMAS[name],
                bound_repo=bound_repo,
            ),
            "name": name,
            "mutates_instructions": False,
            "write_intent": name in WRITE_INTENT_TOOLS,
        }
        for name in tool_names
    ]


def _mcp_display_schema(schema: dict[str, Any], *, bound_repo: Path | None) -> dict[str, Any]:
    if bound_repo is None:
        return schema
    bound_schema = deepcopy(schema)
    properties = bound_schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("repo", None)
    required = bound_schema.get("required")
    if isinstance(required, list):
        bound_schema["required"] = [name for name in required if name != "repo"]
    return bound_schema


def handle_jsonrpc_request(
    request: dict[str, Any],
    *,
    repo: Path | None = None,
    read_only: bool = False,
) -> dict[str, Any] | None:
    bound_repo = repo.expanduser().resolve() if repo is not None else None
    request_id = request.get("id")
    method = request.get("method")
    try:
        if method == "initialize":
            params = request.get("params", {})
            protocol_version = (
                str(params.get("protocolVersion"))
                if isinstance(params, dict) and params.get("protocolVersion")
                else "2024-11-05"
            )
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "tugboat", "version": __version__},
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": list_mcp_tools(bound_repo=bound_repo, read_only=read_only)
                },
            }
        if method == "tools/call":
            params = request.get("params", {})
            if not isinstance(params, dict):
                return _jsonrpc_error(request_id, -32602, "invalid params: params must be an object")
            name = str(params.get("name", ""))
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                return _jsonrpc_error(
                    request_id,
                    -32602,
                    "invalid params: arguments must be an object",
                )
            tool = MCP_TOOLS.get(name)
            if tool is None:
                return _jsonrpc_error(request_id, -32601, f"unknown MCP tool: {name}")
            if read_only and name in (WRITE_INTENT_TOOLS | READ_ONLY_EXCLUDED_TOOLS):
                reason = f"bound read-only MCP session does not expose side-effecting tool: {name}"
                if bound_repo is not None:
                    _audit_bound_mcp_denial(bound_repo, name, arguments, reason)
                return _jsonrpc_error(request_id, -32000, reason)
            bound_error = _apply_bound_mcp_repo(
                bound_repo,
                name,
                arguments,
            )
            if bound_error is not None:
                return _jsonrpc_error(request_id, -32000, bound_error)
            invalid_params = _validate_tool_arguments(name, arguments)
            if invalid_params is not None:
                _audit_jsonrpc_validation_failure(name, arguments, invalid_params)
                return _jsonrpc_error(request_id, -32602, f"invalid params: {invalid_params}")
            invalid_policy = _validate_jsonrpc_mcp_policy(name, arguments)
            if invalid_policy is not None:
                _audit_jsonrpc_policy_denial(name, arguments, invalid_policy)
                return _jsonrpc_error(request_id, -32000, invalid_policy)
            result = tool(**arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "json", "json": redact_payload(result)}]},
            }
        return _jsonrpc_error(request_id, -32601, f"unknown JSON-RPC method: {method}")
    except Exception as error:
        return _jsonrpc_error(request_id, -32000, str(error))


MCP_TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "tugboat_active_instructions": tugboat_active_instructions,
    "tugboat_candidate": tugboat_candidate,
    "tugboat_candidate_report": tugboat_candidate_report,
    "tugboat_daemon_status": tugboat_daemon_status,
    "tugboat_decision_trace": tugboat_decision_trace,
    "tugboat_harness_findings": tugboat_harness_findings,
    "tugboat_index_summary": tugboat_index_summary,
    "tugboat_instruction_graph": tugboat_instruction_graph,
    "tugboat_latest_audit": tugboat_latest_audit,
    "tugboat_latest_runs": tugboat_latest_runs,
    "tugboat_record_episode": tugboat_record_episode,
    "tugboat_request_audit": tugboat_request_audit,
    "tugboat_request_eval": tugboat_request_eval,
    "tugboat_request_proposal": tugboat_request_proposal,
    "tugboat_run_report": tugboat_run_report,
    "tugboat_status": tugboat_status,
}


def _apply_bound_mcp_repo(
    bound_repo: Path | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> str | None:
    if bound_repo is None:
        return None
    raw_repo = arguments.get("repo")
    if raw_repo is None:
        arguments["repo"] = bound_repo.as_posix()
        return None
    if not isinstance(raw_repo, str):
        reason = "bound MCP session repo argument must be string"
        _audit_bound_mcp_denial(bound_repo, tool_name, arguments, reason)
        return reason
    requested_repo = Path(raw_repo).expanduser().resolve()
    if requested_repo != bound_repo:
        reason = "bound MCP session does not allow repo override"
        _audit_bound_mcp_denial(bound_repo, tool_name, arguments, reason)
        return reason
    arguments["repo"] = bound_repo.as_posix()
    return None


def _audit_bound_mcp_denial(
    repo: Path,
    tool_name: str,
    arguments: dict[str, Any],
    reason: str,
) -> None:
    payload = {
        "tool": tool_name,
        "repo": repo.as_posix(),
        "arguments": redact_payload(arguments),
        "status": "failed",
        "reason": redact_text(reason),
    }
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_mcp_call(
            tool_name=tool_name,
            repo_path=repo,
            status="failed",
            payload=payload,
        )


def run_stdio_server(
    input_stream,
    output_stream,
    *,
    repo: Path | None = None,
    read_only: bool = False,
) -> int:
    for line in input_stream:
        if not line.strip():
            continue
        try:
            request = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            response = _jsonrpc_error(None, -32700, "parse error")
        else:
            if not isinstance(request, dict):
                response = _jsonrpc_error(None, -32600, "request must be an object")
            else:
                response = handle_jsonrpc_request(request, repo=repo, read_only=read_only)
        if response is not None:
            output_stream.write(json.dumps(response, sort_keys=True) + "\n")
            output_stream.flush()
    return 0


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _validate_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> str | None:
    schema = MCP_TOOL_INPUT_SCHEMAS[tool_name]
    required = schema.get("required", [])
    properties = schema.get("properties", {})
    for name in required:
        if name not in arguments:
            return f"missing required argument: {name}"
    for name, value in sorted(arguments.items()):
        expected = properties.get(name)
        if expected is None:
            return f"unknown argument: {name}"
        expected_type = expected.get("type")
        if expected_type == "string" and not isinstance(value, str):
            return f"{name} must be string"
        if expected_type == "integer" and (
            not isinstance(value, int) or isinstance(value, bool)
        ):
            return f"{name} must be integer"
        minimum = expected.get("minimum")
        if minimum is not None and isinstance(value, int) and value < int(minimum):
            return f"{name} must be >= {minimum}"
        maximum = expected.get("maximum")
        if maximum is not None and isinstance(value, int) and value > int(maximum):
            return f"{name} must be <= {maximum}"
        pattern = expected.get("pattern")
        if pattern is not None and isinstance(value, str) and re.fullmatch(str(pattern), value) is None:
            return f"{name} has invalid format"
    return None


def _validate_jsonrpc_mcp_policy(tool_name: str, arguments: dict[str, Any]) -> str | None:
    repo_value = arguments.get("repo")
    if not isinstance(repo_value, str):
        return None
    repo = Path(repo_value).expanduser().resolve()
    if not repo.is_dir():
        return None
    policy = load_policy(repo)
    allowed_repositories = tuple(
        Path(path).expanduser().resolve() for path in policy.mcp_allowed_repositories
    )
    if not allowed_repositories:
        return "MCP repo allowlist is required"
    if repo not in allowed_repositories:
        return f"repo is not allowed for MCP: {repo}"
    if tool_name in WRITE_INTENT_TOOLS and tool_name not in policy.mcp_tool_policy:
        return f"MCP write-intent tool requires explicit allow: {tool_name}"
    decision = _mcp_tool_policy_decision(policy.mcp_tool_policy, tool_name)
    if decision != "allow":
        return f"MCP tool denied by policy: {tool_name}"
    return None


def _audit_jsonrpc_policy_denial(
    tool_name: str,
    arguments: dict[str, Any],
    reason: str,
) -> None:
    repo_value = arguments.get("repo")
    if not isinstance(repo_value, str):
        return
    try:
        repo = _resolve_local_repo(repo_value)
    except ValueError:
        return
    payload = {
        "tool": tool_name,
        "repo": repo.as_posix(),
        "arguments": redact_payload(arguments),
        "status": "denied",
        "reason": redact_text(reason),
    }
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_mcp_call(
            tool_name=tool_name,
            repo_path=repo,
            status="denied",
            payload=payload,
        )


def _audit_jsonrpc_validation_failure(
    tool_name: str,
    arguments: dict[str, Any],
    reason: str,
) -> None:
    repo_value = arguments.get("repo")
    if not isinstance(repo_value, str):
        return
    try:
        repo = _resolve_local_repo(repo_value)
    except ValueError:
        return
    payload = {
        "tool": tool_name,
        "repo": repo.as_posix(),
        "arguments": redact_payload(arguments),
        "status": "failed",
        "reason": redact_text(reason),
    }
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_mcp_call(
            tool_name=tool_name,
            repo_path=repo,
            status="failed",
            payload=payload,
        )


def _validate_mcp_artifact_id(kind: str, value: str) -> None:
    if not value or value in {".", ".."} or _SAFE_MCP_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {kind}")


def _validate_mcp_suite_id(value: str) -> None:
    if _SAFE_MCP_SUITE_PATTERN.fullmatch(value) is None:
        raise ValueError("invalid suite")


def _parse_mcp_integer_id(kind: str, value: str) -> int:
    if not value.isdecimal():
        raise ValueError(f"invalid {kind}")
    return int(value)


def _audit_call(
    repo: Path,
    tool: str,
    arguments: dict[str, Any],
    read: Callable[[], T],
) -> T:
    status = "completed"
    result: T | None = None
    event_extra: dict[str, Any] = {}
    try:
        _enforce_mcp_policy(repo, tool)
        result = read()
        if isinstance(result, dict):
            raw_extra = result.pop("_mcp_event", None)
            if isinstance(raw_extra, dict):
                event_extra = raw_extra
        return result
    except ValueError as error:
        if "MCP" in str(error):
            status = "denied"
        else:
            status = "failed"
        raise
    except Exception:
        status = "failed"
        raise
    finally:
        payload = {
            "tool": tool,
            "repo": repo.as_posix(),
            "arguments": redact_payload(arguments),
            "status": status,
        }
        payload.update(redact_payload(event_extra))
        if not event_extra and isinstance(result, dict):
            request = _request_audit_summary(result)
            if request is not None:
                payload["write_intent"] = True
                payload["request"] = request
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.record_mcp_call(
                tool_name=tool,
                repo_path=repo,
                status=status,
                payload=payload,
            )


def _enforce_mcp_policy(repo: Path, tool: str) -> None:
    policy = load_policy(repo)
    allowed_repositories = tuple(Path(path).expanduser().resolve() for path in policy.mcp_allowed_repositories)
    if not allowed_repositories:
        raise ValueError("MCP repo allowlist is required")
    if repo.resolve() not in allowed_repositories:
        raise ValueError(f"repo is not allowed for MCP: {repo}")
    if tool in WRITE_INTENT_TOOLS and tool not in policy.mcp_tool_policy:
        raise ValueError(f"MCP write-intent tool requires explicit allow: {tool}")
    decision = _mcp_tool_policy_decision(policy.mcp_tool_policy, tool)
    if decision != "allow":
        raise ValueError(f"MCP tool denied by policy: {tool}")
    if tool in WRITE_INTENT_TOOLS and default_kill_switch(repo).is_enabled():
        raise ValueError("MCP write-intent tool denied by read-only kill switch")


def _mcp_tool_policy_decision(tool_policy: dict[str, str], tool: str) -> str:
    decision = tool_policy.get(tool)
    if decision is None and tool in WRITE_INTENT_TOOLS:
        return "deny"
    return (decision or "allow").lower()


def _write_request_artifact(
    repo: str | Path,
    *,
    tool: str,
    kind: str,
    payload: dict[str, Any],
    queue_kind: str | None = None,
    queue_payload: dict[str, Any] | None = None,
    preflight: Callable[[], None] | None = None,
) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def write() -> dict[str, Any]:
        if preflight is not None:
            preflight()
        request_id = f"mcp-{kind}-{_stamp()}"
        mcp_dir = sidecar_dir(repo_path) / "mcp"
        request_dir = mcp_dir / "requests"
        path = request_dir / f"{request_id}.json"
        repo_policy = _repo_policy_ref(repo_path)
        artifact = {
            "request_id": request_id,
            "kind": kind,
            "state": "queued",
            "write_intent": True,
            "repo_policy": repo_policy,
            "execution": {
                "kind": queue_kind or kind,
                "payload": queue_payload or payload,
            },
            **payload,
        }
        validate_json_artifact("mcp-request.json", artifact)
        ensure_private_dir(sidecar_dir(repo_path))
        ensure_private_dir(mcp_dir)
        ensure_private_dir(request_dir)
        path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        mark_private_file(path)
        artifact_ref = _relative_ref(repo_path, path)
        daemon_payload = {
            "request_id": request_id,
            "artifact_ref": artifact_ref,
            **(queue_payload or payload),
        }
        recorded_daemon_job_id: str | None = None
        try:
            with DaemonQueue.open_sidecar(repo_path) as queue:
                job = queue.enqueue_uncommitted(
                    kind=queue_kind or kind,
                    payload=daemon_payload,
                )
                with Store.open(sidecar_dir(repo_path) / "db.sqlite") as store:
                    store.record_daemon_job(
                        job_id=str(job.id),
                        repo_path=repo_path,
                        state=job.state.value,
                        payload=daemon_payload,
                    )
                recorded_daemon_job_id = str(job.id)
                queue.connection.commit()
        except Exception:
            if recorded_daemon_job_id is not None:
                with Store.open(sidecar_dir(repo_path) / "db.sqlite") as store:
                    store.update_daemon_job_state(
                        job_id=recorded_daemon_job_id,
                        repo_path=repo_path,
                        state="failed",
                        payload=daemon_payload,
                    )
            path.unlink(missing_ok=True)
            raise
        return {
            "request_id": request_id,
            "kind": kind,
            "state": "queued",
            "write_intent": True,
            "repo_policy": repo_policy,
            "artifact_ref": artifact_ref,
            "_mcp_event": {
                "write_intent": True,
                "request": {
                    "request_id": request_id,
                    "kind": kind,
                    "state": "queued",
                    "artifact_ref": artifact_ref,
                    "repo_policy": repo_policy,
                },
            },
        }

    return _audit_call(repo_path, tool, payload, write)


def _request_audit_summary(result: dict[str, Any]) -> dict[str, Any] | None:
    required = ("request_id", "kind", "state", "artifact_ref", "repo_policy")
    if not all(key in result for key in required):
        return None
    return redact_payload({key: result[key] for key in required})


def _repo_policy_ref(repo: Path) -> dict[str, Any]:
    path = sidecar_dir(repo) / "policy.yaml"
    policy = load_policy(repo)
    if not path.exists():
        return {
            "path": ".sidecar/policy.yaml",
            "version": policy.version,
            "hash": None,
        }
    content = path.read_bytes()
    return {
        "path": _relative_ref(repo, path),
        "version": policy.version,
        "hash": hashlib.sha256(content).hexdigest(),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_local_repo(repo: str | Path) -> Path:
    raw = str(repo)
    if "://" in raw:
        raise ValueError("repo must be a local repo path")
    path = Path(repo).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("repo must be a local repo path")
    return path


def _normalize_limit(limit: int) -> int:
    value = int(limit)
    if value < 1:
        raise ValueError("limit must be at least 1")
    return min(value, 100)


def _run_dir(repo: Path, run_id: str) -> Path:
    run_dir = runs_dir(repo) / run_id
    resolved_run_dir = run_dir.resolve()
    resolved_runs_dir = runs_dir(repo).resolve()
    if not resolved_run_dir.is_relative_to(resolved_runs_dir):
        raise ValueError("run_id must resolve inside repo runs")
    return resolved_run_dir


def _stored_path(repo: Path, raw_path: object) -> Path:
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = repo / path
    resolved = path.resolve()
    if not resolved.is_relative_to(repo):
        raise ValueError("stored artifact path must resolve inside repo")
    return resolved


def _run_artifact_refs(repo: Path, run_dir: Path) -> list[dict[str, str]]:
    return [
        {"kind": kind, "path": _relative_ref(repo, run_dir / filename)}
        for kind, filename in RUN_ARTIFACTS
        if (run_dir / filename).is_file()
    ]


def _relative_ref(repo: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(repo):
        raise ValueError("artifact path must resolve inside repo")
    return resolved.relative_to(repo).as_posix()


def _instruction_chunk_ref(path: str, byte_start: int, byte_end: int) -> str:
    return f"{path}#bytes-{byte_start}-{byte_end}"


def _audit_artifact_summary(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    evidence_refs = payload.get("evidence_refs", [])
    if not isinstance(evidence_refs, list):
        evidence_refs = []
    return redact_payload(
        {
            "audit_id": int(payload["audit_id"]),
            "edit_warranted": bool(payload["edit_warranted"]),
            "failure_class": str(payload["failure_class"]),
            "severity": str(payload["severity"]),
            "confidence": float(payload["confidence"]),
            "evidence_ref_count": len(evidence_refs),
        }
    )


def _summarize_text(text: str, max_length: int = 240) -> str:
    redacted = redact_text(text).strip()
    if len(redacted) <= max_length:
        return redacted
    return redacted[: max_length - 3].rstrip() + "..."


def _sanitize_harness_finding(finding: str) -> str:
    redacted = redact_text(finding)
    duplicate_prefix = "Duplicate instruction rule appears "
    conflict_prefix = "Conflicting instruction rules: "
    if redacted.startswith(duplicate_prefix):
        return _redact_after_delimiter(redacted, ": ")
    if redacted.startswith(conflict_prefix):
        return f"{conflict_prefix}[REDACTED:harness_rule_text]"
    return redacted


def _redact_after_delimiter(value: str, delimiter: str) -> str:
    if delimiter not in value:
        return value
    prefix, _ = value.split(delimiter, 1)
    return f"{prefix}{delimiter}[REDACTED:harness_rule_text]"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _jsonrpc_error(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": redact_text(message)},
    }
