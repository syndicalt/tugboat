from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from tugboat.daemon.service import daemon_status, default_kill_switch


def summarize_observability(
    *,
    runs: Iterable[dict[str, Any]] = (),
    jobs: Iterable[dict[str, Any]] = (),
    evals: Iterable[dict[str, Any]] = (),
    corpus_snapshots: Iterable[dict[str, Any]] = (),
    harness_findings: Iterable[str] = (),
    trace_events: Iterable[dict[str, Any]] = (),
    incidents: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    run_items = list(runs)
    job_items = list(jobs)
    edit_counts = _edit_counts(job_items)
    eval_items = list(evals)
    return {
        "run_duration": _run_duration_summary(run_items),
        "failure_kind_counts": dict(sorted(_failure_kind_counts(run_items).items())),
        "edits": edit_counts,
        "edit_rates": _edit_rates(edit_counts),
        "mean_changed_lines": _mean_changed_lines(job_items),
        "eval_suite_trends": _eval_suite_trends(eval_items),
        "governance_regression_count": _governance_regression_count(eval_items),
        "corpus_growth": _corpus_growth(corpus_snapshots),
        "provider_backend_failure_rate": _provider_backend_failure_rate(run_items),
        "duplicate_rule_count": _duplicate_rule_count(harness_findings),
        "user_correction_recurrence": _user_correction_recurrence(trace_events),
        "recurring_incident_rate": _recurring_incident_rate(incidents),
    }


def summarize_sidecar_observability(repo: Path) -> dict[str, Any]:
    db_path = repo / ".sidecar" / "db.sqlite"
    if not db_path.exists():
        return summarize_observability()
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        runs = _sidecar_runs(connection)
        audit_events = _audit_events(connection)
        evals = _sidecar_evals(connection)
        decisions = _sidecar_decisions(connection)
        incidents = _sidecar_incidents(connection)
        harness_findings = [
            str(row["finding"])
            for row in connection.execute("SELECT finding FROM harness_findings ORDER BY id")
        ]
        trace_events = _sidecar_trace_events(connection)
        corpus_snapshots = _sidecar_corpus_snapshots(connection, repo)
    jobs = [*decisions, *_audit_event_jobs(audit_events)]
    return summarize_observability(
        runs=[*runs, *_audit_event_runs(audit_events)],
        jobs=jobs,
        evals=evals,
        corpus_snapshots=corpus_snapshots,
        harness_findings=harness_findings,
        trace_events=trace_events,
        incidents=incidents,
    ) | {"daemon_queue": _daemon_queue_status(repo)}


def _daemon_queue_status(repo: Path) -> dict[str, Any]:
    status = daemon_status(repo, kill_switch=default_kill_switch(repo))
    return {
        "jobs_by_state": status["jobs_by_state"],
        "oldest_queued_job_id": status["oldest_queued_job_id"],
        "kill_switch_enabled": status["kill_switch_enabled"],
    }


def _sidecar_runs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "run_id": row["id"],
            "status": row["status"],
            "started_at": row["created_at"],
            "finished_at": row["updated_at"],
        }
        for row in connection.execute(
            "SELECT id, status, created_at, updated_at FROM runs ORDER BY created_at"
        )
    ]


def _audit_events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT event_type, payload_json FROM audit_events ORDER BY sequence"
    ):
        payload = json.loads(str(row["payload_json"]))
        if isinstance(payload, dict):
            payload = {"event_type": row["event_type"], **payload}
            events.append(payload)
    return events


def _sidecar_evals(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    evals: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT suite_id, status, report_path, audit_event_sequence
        FROM eval_runs
        ORDER BY id
        """
    ):
        item: dict[str, Any] = {
            "suite_id": row["suite_id"],
            "passed": str(row["status"]) == "passed",
            "completed_at": str(row["audit_event_sequence"] or ""),
        }
        report_path = Path(str(row["report_path"]))
        if report_path.exists():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if isinstance(report, dict):
                item["score"] = report.get("held_out_score", report.get("trigger_score"))
                metrics = report.get("metrics")
                if isinstance(metrics, dict):
                    governance_regressions = _int_metric(metrics.get("governance_regressions"))
                    if governance_regressions is not None:
                        item["governance_regressions"] = governance_regressions
        evals.append(item)
    return evals


def _sidecar_decisions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "state": row["decision"],
        }
        for row in connection.execute("SELECT decision FROM decisions ORDER BY id")
    ]


def _sidecar_incidents(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "failure_class": row["failure_class"],
        }
        for row in connection.execute("SELECT failure_class FROM audits ORDER BY id")
    ]


def _audit_event_jobs(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for event in events:
        event_type = str(event.get("event_type", ""))
        if event_type == "rollback.applied":
            jobs.append({"state": "rolled_back"})
        elif event_type == "apply.applied":
            job = {"state": "observed"}
            if event.get("changed_lines") is not None:
                job["changed_lines"] = event["changed_lines"]
            jobs.append(job)
    return jobs


def _audit_event_runs(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    event_items = list(events)
    explicit_failure_run_ids = {
        str(event.get("run_id"))
        for event in event_items
        if event.get("failure_kind") is not None and event.get("run_id") is not None
    }
    for event in event_items:
        run_failed = event.get("run_failed")
        if isinstance(run_failed, dict) and run_failed.get("failure_kind") is not None:
            if str(event.get("run_id", "")) in explicit_failure_run_ids:
                continue
            runs.append(
                {
                    "run_id": event.get("run_id", ""),
                    "failure_kind": run_failed.get("failure_kind"),
                    "provider": event.get("provider"),
                    "backend": event.get("backend"),
                    "status": event.get("status", "failed"),
                    "duration_seconds": event.get("duration_seconds", 0),
                }
            )
            continue
        if event.get("failure_kind") is None:
            continue
        runs.append(
            {
                "run_id": event.get("run_id", ""),
                "failure_kind": event.get("failure_kind"),
                "provider": event.get("provider"),
                "backend": event.get("backend"),
                "status": event.get("status", "failed"),
                "duration_seconds": event.get("duration_seconds", 0),
            }
        )
    return runs


def _sidecar_trace_events(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT event_type, payload_json FROM trace_events ORDER BY id"
    ):
        payload = json.loads(str(row["payload_json"]))
        if isinstance(payload, dict):
            events.append({"type": row["event_type"], **payload})
    return events


def _sidecar_corpus_snapshots(connection: sqlite3.Connection, repo: Path) -> list[dict[str, Any]]:
    repo_path = str(repo)
    snapshots: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT sequence, payload_json
        FROM audit_events
        WHERE event_type = 'documents.indexed'
        ORDER BY sequence
        """
    ):
        try:
            payload = json.loads(str(row["payload_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or str(payload.get("repo")) != repo_path:
            continue
        document_count = _int_metric(payload.get("documents"))
        if document_count is None:
            continue
        snapshots.append(
            {
                "captured_at": str(row["sequence"]),
                "document_count": document_count,
            }
        )
    if snapshots:
        return snapshots
    return [
        {
            "captured_at": _latest_document_mtime(connection),
            "document_count": int(connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]),
        }
    ]


def _latest_document_mtime(connection: sqlite3.Connection) -> str:
    value = connection.execute("SELECT MAX(mtime) FROM documents").fetchone()[0]
    return str(value or "")


def _run_duration_summary(runs: list[dict[str, Any]]) -> dict[str, int | float]:
    durations = [_duration_seconds(run) for run in runs]
    durations = [duration for duration in durations if duration is not None]
    if not durations:
        return {"count": 0, "total_seconds": 0, "average_seconds": 0, "max_seconds": 0}

    total = sum(durations)
    return {
        "count": len(durations),
        "total_seconds": _round(total),
        "average_seconds": _round(total / len(durations)),
        "max_seconds": _round(max(durations)),
    }


def _duration_seconds(run: dict[str, Any]) -> float | None:
    if "duration_seconds" in run:
        return float(run["duration_seconds"])

    started_at = run.get("started_at")
    finished_at = run.get("finished_at")
    if not started_at or not finished_at:
        return None

    return (_parse_datetime(str(finished_at)) - _parse_datetime(str(started_at))).total_seconds()


def _failure_kind_counts(runs: list[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for run in runs:
        failure_kind = run.get("failure_kind")
        if failure_kind:
            counts[str(failure_kind)] += 1
    return counts


def _edit_counts(jobs: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts = {"accepted": 0, "rejected": 0, "rolled_back": 0}
    for job in jobs:
        state = str(job.get("state", job.get("decision", ""))).lower()
        if state in {"applied", "accepted", "accept"}:
            counts["accepted"] += 1
        elif state in {"rejected", "reject"}:
            counts["rejected"] += 1
        elif state in {"rolled_back", "rollback", "rolled-back"}:
            counts["rolled_back"] += 1
    return counts


def _edit_rates(counts: dict[str, int]) -> dict[str, int | float]:
    reviewed_count = counts["accepted"] + counts["rejected"] + counts["rolled_back"]
    if reviewed_count == 0:
        return {
            "acceptance_rate": 0,
            "rejection_rate": 0,
            "rollback_rate": 0,
            "reviewed_count": 0,
        }
    return {
        "acceptance_rate": _round(counts["accepted"] / reviewed_count),
        "rejection_rate": _round(counts["rejected"] / reviewed_count),
        "rollback_rate": _round(counts["rolled_back"] / reviewed_count),
        "reviewed_count": reviewed_count,
    }


def _mean_changed_lines(jobs: Iterable[dict[str, Any]]) -> int | float:
    changed_lines = [
        float(job["changed_lines"])
        for job in jobs
        if job.get("changed_lines") is not None
    ]
    if not changed_lines:
        return 0
    return _round(sum(changed_lines) / len(changed_lines))


def _eval_suite_trends(evals: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_suite: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in evals:
        suite_id = item.get("suite_id")
        if suite_id:
            by_suite[str(suite_id)].append(item)

    trends: dict[str, dict[str, Any]] = {}
    for suite_id, suite_items in by_suite.items():
        ordered = sorted(suite_items, key=lambda item: str(item.get("completed_at", "")))
        scores = [_eval_score(item) for item in ordered]
        latest = scores[-1]
        previous = scores[-2] if len(scores) > 1 else None
        trends[suite_id] = {
            "count": len(scores),
            "latest_score": latest,
            "previous_score": previous,
            "delta": _round(latest - previous) if previous is not None else None,
        }
    return dict(sorted(trends.items()))


def _eval_score(item: dict[str, Any]) -> float:
    if "score" in item:
        return float(item["score"])
    return 1.0 if bool(item.get("passed")) else 0.0


def _governance_regression_count(evals: Iterable[dict[str, Any]]) -> int:
    count = 0
    for item in evals:
        governance_regressions = _int_metric(item.get("governance_regressions"))
        if governance_regressions is not None:
            count += governance_regressions
    return count


def _int_metric(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _corpus_growth(snapshots: Iterable[dict[str, Any]]) -> dict[str, int]:
    ordered = sorted(snapshots, key=lambda item: str(item.get("captured_at", "")))
    if not ordered:
        return {"earliest_count": 0, "latest_count": 0, "delta": 0}

    earliest = int(ordered[0].get("document_count", ordered[0].get("corpus_size", 0)))
    latest = int(ordered[-1].get("document_count", ordered[-1].get("corpus_size", 0)))
    return {"earliest_count": earliest, "latest_count": latest, "delta": latest - earliest}


def _provider_backend_failure_rate(runs: list[dict[str, Any]]) -> dict[str, int | float]:
    relevant = [
        run
        for run in runs
        if run.get("provider") is not None
        or run.get("backend") is not None
        or run.get("failure_kind") in {"provider_error", "backend_error"}
    ]
    failed = sum(
        1
        for run in relevant
        if run.get("failure_kind") in {"provider_error", "backend_error"}
        or str(run.get("status", "")).lower() == "failed"
    )
    total = len(relevant)
    return {
        "failed": failed,
        "rate": _round(failed / total) if total else 0,
        "total": total,
    }


def _duplicate_rule_count(harness_findings: Iterable[str]) -> int:
    return sum(
        1
        for finding in harness_findings
        if str(finding).startswith("Duplicate instruction rule appears ")
    )


def _user_correction_recurrence(events: Iterable[dict[str, Any]]) -> dict[str, int]:
    corrections: list[str] = []
    for event in events:
        if str(event.get("type", event.get("event", ""))) != "user_correction":
            continue
        text = str(event.get("content", event.get("text", event.get("message", "")))).strip()
        if text:
            corrections.append(" ".join(text.casefold().split()))
    counts = Counter(corrections)
    return {
        "correction_count": len(corrections),
        "recurring_correction_count": sum(1 for count in counts.values() if count > 1),
        "unique_correction_count": len(counts),
    }


def _recurring_incident_rate(incidents: Iterable[dict[str, Any]]) -> dict[str, int | float]:
    incident_classes = [
        str(incident.get("failure_class", incident.get("incident_class", ""))).strip()
        for incident in incidents
    ]
    incident_classes = [value for value in incident_classes if value]
    counts = Counter(incident_classes)
    recurring_count = sum(count for count in counts.values() if count > 1)
    incident_count = len(incident_classes)
    return {
        "incident_count": incident_count,
        "recurring_incident_count": recurring_count,
        "rate": _round(recurring_count / incident_count) if incident_count else 0,
        "unique_incident_class_count": len(counts),
    }


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _round(value: float) -> int | float:
    rounded = round(value, 6)
    return int(rounded) if rounded.is_integer() else rounded
