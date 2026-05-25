from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from tugboat.config import load_policy
from tugboat.corpus.indexer import index_repo
from tugboat.db import Store
from tugboat.harness.checks import check_harness_legibility
from tugboat.paths import runs_dir, sidecar_dir
from tugboat.security.redaction import redact_payload, redact_text


T = TypeVar("T")

RUN_ARTIFACTS: tuple[tuple[str, str], ...] = (
    ("audit", "audit.json"),
    ("candidate", "candidate.json"),
    ("candidate_diff", "candidate.diff"),
    ("eval_report", "eval-report.json"),
    ("policy_gate", "policy-gate.json"),
    ("report", "report.md"),
)


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


def tugboat_harness_findings(repo: str | Path) -> dict[str, Any]:
    repo_path = _resolve_local_repo(repo)

    def read() -> dict[str, Any]:
        result = check_harness_legibility(repo_path)
        return {"passed": result.passed, "findings": list(result.findings)}

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


def _audit_call(
    repo: Path,
    tool: str,
    arguments: dict[str, Any],
    read: Callable[[], T],
) -> T:
    status = "completed"
    try:
        return read()
    except Exception:
        status = "failed"
        raise
    finally:
        with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
            store.append_audit_event(
                "mcp.tool_called",
                {
                    "tool": tool,
                    "repo": repo.as_posix(),
                    "arguments": redact_payload(arguments),
                    "status": status,
                },
            )


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
