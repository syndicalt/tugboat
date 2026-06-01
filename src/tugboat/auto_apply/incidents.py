from __future__ import annotations

import json
from pathlib import Path

from tugboat.artifacts import validate_json_artifact
from tugboat.auto_apply import AutoApplyIncidentState
from tugboat.db import Store
from tugboat.paths import sidecar_dir


def active_rollback_incidents(repo: Path) -> tuple[AutoApplyIncidentState, ...]:
    db_path = sidecar_dir(repo) / "db.sqlite"
    if not db_path.exists():
        return ()
    active_by_candidate: dict[int, AutoApplyIncidentState] = {}
    with Store.open(db_path) as store:
        rows = store.connection.execute(
            """
            SELECT event_type, payload_json
            FROM audit_events
            WHERE event_type IN ('rollback.failed', 'rollback.applied')
            ORDER BY sequence
            """
        ).fetchall()
    for event_type, payload_json in rows:
        try:
            payload = json.loads(str(payload_json))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        candidate_id = payload.get("candidate_id")
        if isinstance(candidate_id, bool):
            continue
        try:
            candidate_key = int(candidate_id)
        except (TypeError, ValueError):
            continue
        if str(event_type) == "rollback.applied":
            active_by_candidate.pop(candidate_key, None)
            continue
        incident_ref = payload.get("incident")
        failure_kind = payload.get("failure_kind")
        incident = str(incident_ref) if isinstance(incident_ref, str) else ""
        artifact_valid, artifact_status = rollback_incident_artifact_status(
            repo,
            incident=incident,
            candidate_id=candidate_key,
        )
        active_by_candidate[candidate_key] = AutoApplyIncidentState(
            artifact_status=artifact_status,
            artifact_valid=artifact_valid,
            candidate_id=candidate_key,
            event_type="rollback.failed",
            failure_kind=str(failure_kind) if isinstance(failure_kind, str) else "",
            incident=incident,
        )
    return tuple(active_by_candidate[candidate_id] for candidate_id in sorted(active_by_candidate))


def rollback_incident_artifact_status(
    repo: Path,
    *,
    incident: str,
    candidate_id: int,
) -> tuple[bool, str]:
    if not incident:
        return False, "missing_incident_ref"
    incident_path = (repo / incident).resolve()
    try:
        incident_path.relative_to(repo.resolve())
    except ValueError:
        return False, "outside_repo"
    if not incident_path.exists():
        return False, "missing"
    try:
        payload = json.loads(incident_path.read_text(encoding="utf-8"))
        validate_json_artifact("rollback-incident.json", payload)
    except (OSError, json.JSONDecodeError, ValueError):
        return False, "invalid"
    try:
        incident_candidate_id = int(payload.get("candidate_id", -1))
    except (TypeError, ValueError):
        return False, "candidate_mismatch"
    if incident_candidate_id != candidate_id:
        return False, "candidate_mismatch"
    if payload.get("rollback_plan_written") is not False:
        return False, "not_active"
    return True, "valid"
