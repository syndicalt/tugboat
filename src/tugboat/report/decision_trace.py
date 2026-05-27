from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tugboat.artifacts import SCHEMA_VERSION, validate_json_artifact, write_json_artifact
from tugboat.db import Store
from tugboat.paths import latest_run_dir, runs_dir, sidecar_dir
from tugboat.security.secrets import SecretScanError, scan_text


def write_decision_trace(repo: Path, decision_ref: str) -> Path:
    repo = repo.resolve()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        decision_id, run_dir = _resolve_decision(store, repo, decision_ref)
        payload = _decision_trace_payload(store, repo, decision_ref, decision_id, run_dir)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    findings = scan_text((run_dir / "decision-trace.json").as_posix(), serialized)
    if findings:
        raise SecretScanError(findings)
    return write_json_artifact(run_dir / "decision-trace.json", payload)


def _resolve_decision(
    store: Store,
    repo: Path,
    decision_ref: str,
) -> tuple[int, Path]:
    if decision_ref == "latest":
        run_dir = latest_run_dir(repo)
        candidate_id = _candidate_id_from_run_dir(run_dir)
        return _latest_decision_id_for_candidate(store, candidate_id), run_dir

    run_dir = runs_dir(repo) / decision_ref
    if run_dir.exists():
        candidate_id = _candidate_id_from_run_dir(run_dir)
        return _latest_decision_id_for_candidate(store, candidate_id), _repo_local_run_dir(
            repo,
            run_dir.name,
        )

    try:
        decision_id = int(decision_ref)
    except ValueError as error:
        raise ValueError(f"unknown decision ref: {decision_ref}") from error
    row = store.connection.execute(
        """
        SELECT a.run_id
        FROM decisions d
        JOIN candidates c ON c.id = d.candidate_id
        JOIN audits a ON a.id = c.audit_id
        WHERE d.id = ?
        """,
        (decision_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown decision id: {decision_ref}")
    return decision_id, _repo_local_run_dir(repo, str(row[0]))


def _candidate_id_from_run_dir(run_dir: Path) -> int:
    for artifact_name in ("candidate.json", "decision.json"):
        path = run_dir / artifact_name
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "candidate_id" in payload:
            return int(payload["candidate_id"])
    raise ValueError(f"run has no candidate id: {run_dir.name}")


def _latest_decision_id_for_candidate(store: Store, candidate_id: int) -> int:
    row = store.connection.execute(
        """
        SELECT id
        FROM decisions
        WHERE candidate_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"candidate has no decision: {candidate_id}")
    return int(row[0])


def _decision_trace_payload(
    store: Store,
    repo: Path,
    decision_ref: str,
    decision_id: int,
    run_dir: Path,
) -> dict[str, Any]:
    row = store.connection.execute(
        """
        SELECT
          d.id,
          d.candidate_id,
          d.actor,
          d.policy,
          d.decision,
          d.reason,
          d.created_at,
          d.applied_commit,
          d.rollback_ref,
          d.audit_event_sequence,
          de.event_hash,
          c.audit_id,
          c.base_file,
          c.base_hash,
          c.diff_hash,
          c.diff_path,
          c.risk_class,
          c.rationale,
          c.state,
          c.audit_event_sequence,
          ce.event_hash,
          a.run_id,
          a.failure_class,
          a.severity,
          a.confidence,
          a.evidence_json,
          a.instruction_refs_json,
          a.audit_event_sequence,
          ae.event_hash,
          r.episode_id
        FROM decisions d
        JOIN audit_events de ON de.sequence = d.audit_event_sequence
        JOIN candidates c ON c.id = d.candidate_id
        JOIN audit_events ce ON ce.sequence = c.audit_event_sequence
        JOIN audits a ON a.id = c.audit_id
        JOIN audit_events ae ON ae.sequence = a.audit_event_sequence
        LEFT JOIN runs r ON r.id = a.run_id
        WHERE d.id = ?
        """,
        (decision_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown decision id: {decision_id}")

    evidence_refs = _json_list(row[25])
    instruction_refs = _json_list(row[26])
    trace_events = _trace_events(store, episode_id=row[29], evidence_refs=evidence_refs)
    resolved_evidence_refs = {event["evidence_id"] for event in trace_events}
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "decision_ref": decision_ref,
        "run_id": str(row[21]),
        "decision": {
            "decision_id": int(row[0]),
            "candidate_id": int(row[1]),
            "actor": str(row[2]),
            "policy": str(row[3]),
            "decision": str(row[4]),
            "reason": str(row[5]),
            "created_at": str(row[6]),
            "applied_commit": str(row[7]),
            "rollback_ref": str(row[8]),
            "audit_event_sequence": int(row[9]),
            "event_hash": str(row[10]),
        },
        "candidate": {
            "candidate_id": int(row[1]),
            "audit_id": int(row[11]),
            "base_file": str(row[12]),
            "base_hash": str(row[13]),
            "diff_hash": str(row[14]),
            "diff_path": _repo_relative_path(repo, Path(str(row[15]))),
            "risk_class": str(row[16]),
            "rationale": str(row[17]),
            "state": str(row[18]),
            "audit_event_sequence": int(row[19]),
            "event_hash": str(row[20]),
        },
        "audit": {
            "audit_id": int(row[11]),
            "run_id": str(row[21]),
            "failure_class": str(row[22]),
            "severity": str(row[23]),
            "confidence": float(row[24]),
            "evidence_refs": evidence_refs,
            "instruction_refs": instruction_refs,
            "audit_event_sequence": int(row[27]),
            "event_hash": str(row[28]),
        },
        "trace_events": trace_events,
        "unresolved_evidence_refs": sorted(set(evidence_refs) - resolved_evidence_refs),
        "evals": _evals(store, repo, candidate_id=int(row[1])),
        "llmff_jobs": _llmff_jobs(store, repo, run_id=str(row[21])),
        "artifacts": _artifact_refs(repo, run_dir),
    }
    validate_json_artifact("decision-trace.json", payload)
    return payload


def _trace_events(
    store: Store,
    *,
    episode_id: object,
    evidence_refs: list[str],
) -> list[dict[str, Any]]:
    if episode_id is None or not evidence_refs:
        return []
    placeholders = ",".join("?" for _ in evidence_refs)
    rows = store.connection.execute(
        f"""
        SELECT
          te.evidence_id,
          te.event_type,
          te.source_trust,
          te.line_number,
          te.audit_event_sequence,
          ae.event_hash
        FROM trace_events te
        JOIN audit_events ae ON ae.sequence = te.audit_event_sequence
        WHERE te.episode_id = ? AND te.evidence_id IN ({placeholders})
        ORDER BY te.id
        """,
        (int(episode_id), *evidence_refs),
    ).fetchall()
    return [
        {
            "evidence_id": str(row[0]),
            "event_type": str(row[1]),
            "source_trust": str(row[2]),
            "line_number": int(row[3]),
            "audit_event_sequence": int(row[4]),
            "event_hash": str(row[5]),
        }
        for row in rows
    ]


def _evals(store: Store, repo: Path, *, candidate_id: int) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        """
        SELECT
          e.id,
          e.suite_id,
          e.report_path,
          e.passed,
          e.metrics_json,
          e.audit_event_sequence,
          ae.event_hash
        FROM evals e
        JOIN audit_events ae ON ae.sequence = e.audit_event_sequence
        WHERE e.candidate_id = ?
        ORDER BY e.id
        """,
        (candidate_id,),
    ).fetchall()
    return [
        {
            "eval_id": int(row[0]),
            "suite_id": str(row[1]),
            "report_path": _repo_relative_path(repo, Path(str(row[2]))),
            "passed": bool(row[3]),
            "metrics": _json_object(row[4]),
            "audit_event_sequence": int(row[5]),
            "event_hash": str(row[6]),
        }
        for row in rows
    ]


def _llmff_jobs(store: Store, repo: Path, *, run_id: str) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        """
        SELECT
          j.id,
          j.manifest_name,
          j.manifest_hash,
          j.status,
          j.exit_code,
          j.audit_event_sequence,
          ae.event_hash
        FROM llmff_jobs j
        JOIN audit_events ae ON ae.sequence = j.audit_event_sequence
        WHERE j.run_id = ?
        ORDER BY j.id
        """,
        (run_id,),
    ).fetchall()
    return [
        {
            "job_id": int(row[0]),
            "manifest_name": str(row[1]),
            "manifest_hash": str(row[2]),
            "status": str(row[3]),
            "exit_code": None if row[4] is None else int(row[4]),
            "audit_event_sequence": int(row[5]),
            "event_hash": str(row[6]),
            "events": _llmff_events(store, job_id=int(row[0])),
            "outputs": _llmff_outputs(store, repo, job_id=int(row[0])),
        }
        for row in rows
    ]


def _llmff_events(store: Store, *, job_id: int) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        """
        SELECT
          e.id,
          e.event_type,
          e.audit_event_sequence,
          ae.event_hash
        FROM llmff_events e
        JOIN audit_events ae ON ae.sequence = e.audit_event_sequence
        WHERE e.job_id = ?
        ORDER BY e.id
        """,
        (job_id,),
    ).fetchall()
    return [
        {
            "event_id": int(row[0]),
            "event_type": str(row[1]),
            "audit_event_sequence": int(row[2]),
            "event_hash": str(row[3]),
        }
        for row in rows
    ]


def _llmff_outputs(store: Store, repo: Path, *, job_id: int) -> list[dict[str, Any]]:
    rows = store.connection.execute(
        """
        SELECT
          o.id,
          o.output_name,
          o.artifact_path,
          o.content_hash,
          o.audit_event_sequence,
          ae.event_hash
        FROM llmff_outputs o
        JOIN audit_events ae ON ae.sequence = o.audit_event_sequence
        WHERE o.job_id = ?
        ORDER BY o.id
        """,
        (job_id,),
    ).fetchall()
    return [
        {
            "output_id": int(row[0]),
            "output_name": str(row[1]),
            "artifact_path": _repo_relative_path(repo, Path(str(row[2]))),
            "content_hash": str(row[3]),
            "audit_event_sequence": int(row[4]),
            "event_hash": str(row[5]),
        }
        for row in rows
    ]


def _artifact_refs(repo: Path, run_dir: Path) -> dict[str, str]:
    candidates = (
        ("trace_input", run_dir / "trace-input.jsonl"),
        ("trace_redacted", run_dir / "trace-redacted.jsonl"),
        ("canonical_episode", run_dir / "canonical-episode.json"),
        ("instruction_snapshot", run_dir / "instruction-snapshot"),
        ("instruction_graph", run_dir / "instruction-graph.json"),
        ("audit_report", run_dir / "audit.json"),
        ("audit_raw", run_dir / "audit.raw.json"),
        ("evidence_ids_raw", run_dir / "evidence-ids.raw.json"),
        ("batch_audit_reports", run_dir / "batch-audit-reports.json"),
        ("instruction_index_raw", run_dir / "instruction-index.raw.json"),
        ("drift_raw", run_dir / "drift.raw.json"),
        ("optimizer_notes_raw", run_dir / "optimizer-notes.raw.json"),
        ("optimizer_memory", run_dir / "optimizer-memory.json"),
        ("candidate_metadata", run_dir / "candidate.json"),
        ("candidate_raw", run_dir / "candidate.raw.json"),
        ("candidate_diff", run_dir / "candidate.diff"),
        ("proposal_rationale_raw", run_dir / "proposal-rationale.raw.json"),
        ("policy_gate", run_dir / "policy-gate.json"),
        ("eval_report", run_dir / "eval-report.json"),
        ("eval_report_raw", run_dir / "eval-report.raw.json"),
        ("policy_decision_raw", run_dir / "policy-decision.raw.json"),
        ("acceptance_summary_raw", run_dir / "acceptance-summary.raw.json"),
        ("optimization_summary", run_dir / "optimization-summary.json"),
        ("decision_artifact", run_dir / "decision.json"),
        ("apply_plan", run_dir / "apply-plan.json"),
        ("provenance_bundle", run_dir / "provenance-bundle.json"),
        ("rollback_plan", run_dir / "rollback-plan.json"),
        ("report", run_dir / "report.md"),
    )
    return {
        name: _repo_relative_path(repo, path)
        for name, path in candidates
        if path.exists()
    }


def _json_list(raw: str) -> list[str]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload]


def _json_object(raw: str) -> dict[str, Any]:
    payload = json.loads(raw)
    return payload if isinstance(payload, dict) else {}


def _repo_local_run_dir(repo: Path, run_id: str) -> Path:
    run_dir = runs_dir(repo) / run_id
    if not run_dir.resolve().is_relative_to(repo):
        raise ValueError("run_id must resolve inside repo")
    return run_dir


def _repo_relative_path(repo: Path, path: Path) -> str:
    absolute_path = path if path.is_absolute() else repo / path
    return absolute_path.resolve().relative_to(repo).as_posix()
