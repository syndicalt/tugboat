from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from tugboat.config import load_policy
from tugboat.corpus.indexer import index_repo
from tugboat.daemon.queue import DaemonQueue
from tugboat.daemon.service import daemon_status, default_kill_switch
from tugboat.db import Store
from tugboat.harness.checks import check_harness_legibility
from tugboat.paths import runs_dir, sidecar_dir
from tugboat.security.redaction import redact_payload, redact_text
from tugboat.security.secrets import SecretScanError, scan_path


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
_STRING_ID_SCHEMA = {"pattern": _SAFE_MCP_ID_PATTERN_TEXT, "type": "string"}

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
        {"repo": _REPO_SCHEMA, "candidate_id": _STRING_ID_SCHEMA, "suite": {"type": "string"}},
        ("repo", "candidate_id", "suite"),
    ),
    "tugboat_request_proposal": _object_schema(
        {"repo": _REPO_SCHEMA, "audit_id": _STRING_ID_SCHEMA},
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
            pending_candidates = int(
                store.connection.execute(
                    "SELECT COUNT(*) FROM candidates WHERE state = 'needs_review'"
                ).fetchone()[0]
            )
            indexed_documents = store.count("documents")
        return {
            "mode": policy.mode,
            "auto_apply": "enabled" if policy.auto_apply_enabled else "disabled",
            "indexed_documents": indexed_documents,
            "latest_run": (
                {"run_id": str(latest[0]), "stage": str(latest[1]), "status": str(latest[2])}
                if latest
                else None
            ),
            "pending_candidates": pending_candidates,
        }

    return _audit_call(repo_path, "tugboat_status", {}, read)


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


def tugboat_daemon_status(repo: str | Path) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        return daemon_status(repo_path, kill_switch=default_kill_switch(repo_path))

    return _audit_call(repo_path, "tugboat_daemon_status", {}, read)


def tugboat_record_episode(repo: str | Path, trace_jsonl: str) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def write() -> dict[str, Any]:
        trace_id = f"mcp-trace-{_stamp()}"
        path = sidecar_dir(repo_path) / "mcp" / "episodes" / f"{trace_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(trace_jsonl, encoding="utf-8")
        try:
            scan_path(path)
        except SecretScanError as error:
            path.unlink(missing_ok=True)
            raise ValueError("secret detected in episode payload") from error
        return {
            "trace_id": trace_id,
            "artifact_ref": _relative_ref(repo_path, path),
        }

    return _audit_call(
        repo_path,
        "tugboat_record_episode",
        {"trace_jsonl": "[artifact-payload]"},
        write,
    )


def tugboat_request_audit(repo: str | Path, trace_id: str) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)
    _validate_mcp_artifact_id("trace_id", trace_id)
    trace_path = sidecar_dir(repo_path) / "mcp" / "episodes" / f"{trace_id}.jsonl"
    if not trace_path.is_file():
        raise ValueError(f"unknown trace_id: {trace_id}")
    return _write_request_artifact(
        repo_path,
        tool="tugboat_request_audit",
        kind="audit",
        payload={"trace_id": trace_id},
        queue_kind="trace_audit",
        queue_payload={"trace_path": str(trace_path)},
    )


def tugboat_request_proposal(repo: str | Path, audit_id: str) -> dict[str, Any]:
    return _write_request_artifact(
        repo,
        tool="tugboat_request_proposal",
        kind="proposal",
        payload={"audit_id": audit_id},
    )


def tugboat_request_eval(repo: str | Path, candidate_id: str, suite: str) -> dict[str, Any]:
    return _write_request_artifact(
        repo,
        tool="tugboat_request_eval",
        kind="eval",
        payload={"candidate_id": candidate_id, "suite": suite},
    )


def list_mcp_tools() -> list[dict[str, Any]]:
    return [
        {
            "inputSchema": MCP_TOOL_INPUT_SCHEMAS[name],
            "name": name,
            "mutates_instructions": False,
            "write_intent": name in WRITE_INTENT_TOOLS,
        }
        for name in sorted(MCP_TOOLS)
    ]


def handle_jsonrpc_request(request: dict[str, Any]) -> dict[str, Any]:
    request_id = request.get("id")
    method = request.get("method")
    try:
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": list_mcp_tools()}}
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
            invalid_params = _validate_tool_arguments(name, arguments)
            if invalid_params is not None:
                return _jsonrpc_error(request_id, -32602, f"invalid params: {invalid_params}")
            result = tool(**arguments)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "json", "json": result}]},
            }
        return _jsonrpc_error(request_id, -32601, f"unknown JSON-RPC method: {method}")
    except Exception as error:
        return _jsonrpc_error(request_id, -32000, str(error))


MCP_TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "tugboat_active_instructions": tugboat_active_instructions,
    "tugboat_candidate": tugboat_candidate,
    "tugboat_candidate_report": tugboat_candidate_report,
    "tugboat_daemon_status": tugboat_daemon_status,
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


def run_stdio_server(input_stream, output_stream) -> int:
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
                response = handle_jsonrpc_request(request)
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


def _validate_mcp_artifact_id(kind: str, value: str) -> None:
    if not value or value in {".", ".."} or _SAFE_MCP_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {kind}")


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
    if allowed_repositories and repo.resolve() not in allowed_repositories:
        raise ValueError(f"repo is not allowed for MCP: {repo}")
    decision = policy.mcp_tool_policy.get(tool, "allow").lower()
    if decision != "allow":
        raise ValueError(f"MCP tool denied by policy: {tool}")


def _write_request_artifact(
    repo: str | Path,
    *,
    tool: str,
    kind: str,
    payload: dict[str, Any],
    queue_kind: str | None = None,
    queue_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def write() -> dict[str, Any]:
        request_id = f"mcp-{kind}-{_stamp()}"
        path = sidecar_dir(repo_path) / "mcp" / "requests" / f"{request_id}.json"
        repo_policy = _repo_policy_ref(repo_path)
        artifact = {
            "request_id": request_id,
            "kind": kind,
            "state": "queued",
            "write_intent": True,
            "repo_policy": repo_policy,
            **payload,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        artifact_ref = _relative_ref(repo_path, path)
        with DaemonQueue.open_sidecar(repo_path) as queue:
            queue.enqueue(
                kind=queue_kind or kind,
                payload={
                    "request_id": request_id,
                    "artifact_ref": artifact_ref,
                    **(queue_payload or payload),
                },
            )
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
