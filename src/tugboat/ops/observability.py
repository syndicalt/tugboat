from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from tugboat.config import load_policy


def summarize_observability(
    *,
    runs: Iterable[dict[str, Any]] = (),
    jobs: Iterable[dict[str, Any]] = (),
    evals: Iterable[dict[str, Any]] = (),
    corpus_snapshots: Iterable[dict[str, Any]] = (),
    harness_findings: Iterable[object] = (),
    trace_events: Iterable[dict[str, Any]] = (),
    incidents: Iterable[dict[str, Any]] = (),
    auto_apply_events: Iterable[dict[str, Any]] = (),
    auto_apply_lane_names: Iterable[str] = (),
    auto_apply_paused: bool = False,
    auto_apply_paused_lanes: Iterable[str] = (),
) -> dict[str, Any]:
    run_items = list(runs)
    job_items = list(jobs)
    edit_counts = _edit_counts(job_items)
    eval_items = list(evals)
    harness_finding_items = list(harness_findings)
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
        "duplicate_rule_count": _duplicate_rule_count(harness_finding_items),
        "stale_doc_count": _stale_doc_count(harness_finding_items),
        "user_correction_recurrence": _user_correction_recurrence(trace_events),
        "recurring_incident_rate": _recurring_incident_rate(incidents),
        "auto_apply_lanes": _auto_apply_lane_counts(
            auto_apply_events,
            lane_names=auto_apply_lane_names,
            paused=auto_apply_paused,
            paused_lanes=auto_apply_paused_lanes,
        ),
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
            {
                "finding": str(row["finding"]),
                "severity": str(row["severity"]),
            }
            for row in connection.execute(
                "SELECT finding, severity FROM harness_findings ORDER BY id"
            )
        ]
        trace_events = _sidecar_trace_events(connection)
        corpus_snapshots = _sidecar_corpus_snapshots(connection, repo)
    daemon_queue = _daemon_queue_status(repo)
    jobs = [*decisions, *_audit_event_jobs(audit_events)]
    return summarize_observability(
        runs=[*runs, *_audit_event_runs(audit_events)],
        jobs=jobs,
        evals=evals,
        corpus_snapshots=corpus_snapshots,
        harness_findings=harness_findings,
        trace_events=trace_events,
        incidents=incidents,
        auto_apply_events=audit_events,
        auto_apply_lane_names=_auto_apply_lane_names(repo),
        auto_apply_paused_lanes=_auto_apply_paused_lane_names(repo),
        auto_apply_paused=bool(daemon_queue["kill_switch_enabled"]),
    ) | {"daemon_queue": daemon_queue}


def observability_event_log_text(
    summary: dict[str, Any],
    *,
    source: str = "ops.observability",
    repo: str | None = None,
) -> str:
    context = {"source": source}
    if repo is not None:
        context["repo"] = repo
    events = [
        {
            **context,
            "event": "observability.summary",
            "run_count": _numeric_summary_value(summary, "run_duration", "count"),
            "failure_count": sum(_numeric_mapping(summary.get("failure_kind_counts")).values()),
            "provider_backend_failure_rate": _numeric_summary_value(
                summary,
                "provider_backend_failure_rate",
                "rate",
            ),
            "accepted_edits": _numeric_summary_value(summary, "edits", "accepted"),
            "rejected_edits": _numeric_summary_value(summary, "edits", "rejected"),
            "rolled_back_edits": _numeric_summary_value(summary, "edits", "rolled_back"),
            "governance_regression_count": _number_or_zero(
                summary.get("governance_regression_count")
            ),
        }
    ]
    daemon_event = _daemon_queue_event(summary.get("daemon_queue"), context)
    if daemon_event is not None:
        events.append(daemon_event)
    for lane_event in _auto_apply_lane_events(summary.get("auto_apply_lanes"), context):
        events.append(lane_event)
    return "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n"


def _daemon_queue_event(
    daemon_queue: object,
    context: dict[str, str],
) -> dict[str, Any] | None:
    if not isinstance(daemon_queue, dict):
        return None
    jobs_by_state = _numeric_mapping(daemon_queue.get("jobs_by_state"))
    return {
        **context,
        "event": "observability.daemon_queue",
        "jobs_by_state": jobs_by_state,
        "kill_switch_enabled": bool(daemon_queue.get("kill_switch_enabled", False)),
        "leased_job_count": _number_or_zero(daemon_queue.get("leased_job_count")),
        "stuck_job_count": _number_or_zero(daemon_queue.get("stuck_job_count")),
    }


def _auto_apply_lane_events(
    lanes: object,
    context: dict[str, str],
) -> list[dict[str, Any]]:
    if not isinstance(lanes, dict):
        return []
    events: list[dict[str, Any]] = []
    for lane_name, lane_counts in sorted(lanes.items()):
        counts = _numeric_mapping(lane_counts)
        events.append(
            {
                **context,
                "event": "observability.auto_apply_lane",
                "lane": str(lane_name),
                "shadowed": counts.get("shadowed", 0),
                "eligible": counts.get("eligible", 0),
                "rejected": counts.get("rejected", 0),
                "staged": counts.get("staged", 0),
                "applied": counts.get("applied", 0),
                "rolled_back": counts.get("rolled_back", 0),
                "paused": counts.get("paused", 0),
            }
        )
    return events


def _numeric_summary_value(summary: dict[str, Any], section: str, key: str) -> int | float:
    section_value = summary.get(section)
    if not isinstance(section_value, dict):
        return 0
    return _number_or_zero(section_value.get(key))


def _numeric_mapping(values: object) -> dict[str, int | float]:
    if not isinstance(values, dict):
        return {}
    return {
        str(key): value
        for key, value in sorted(values.items())
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _number_or_zero(value: object) -> int | float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return 0


def observability_metrics_text(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_prefixed_numeric_metrics(lines, "tugboat_run_duration_seconds", summary.get("run_duration"))
    _append_labelled_counter(
        lines,
        "tugboat_failure_kind_total",
        "failure_kind",
        summary.get("failure_kind_counts"),
    )
    _append_prefixed_numeric_metrics(lines, "tugboat_edits", summary.get("edits"))
    _append_prefixed_numeric_metrics(lines, "tugboat_edit_rates", summary.get("edit_rates"))
    _append_metric(lines, "tugboat_mean_changed_lines", summary.get("mean_changed_lines"))
    _append_metric(
        lines,
        "tugboat_governance_regression_count",
        summary.get("governance_regression_count"),
    )
    _append_prefixed_numeric_metrics(lines, "tugboat_corpus_growth", summary.get("corpus_growth"))
    provider_backend_failure_rate = summary.get("provider_backend_failure_rate")
    if isinstance(provider_backend_failure_rate, dict):
        _append_metric(
            lines,
            "tugboat_provider_backend_failure_rate",
            provider_backend_failure_rate.get("rate"),
        )
        _append_metric(
            lines,
            "tugboat_provider_backend_failure_failed",
            provider_backend_failure_rate.get("failed"),
        )
        _append_metric(
            lines,
            "tugboat_provider_backend_failure_total",
            provider_backend_failure_rate.get("total"),
        )
    _append_metric(lines, "tugboat_duplicate_rule_count", summary.get("duplicate_rule_count"))
    _append_metric(lines, "tugboat_stale_doc_count", summary.get("stale_doc_count"))
    _append_prefixed_numeric_metrics(
        lines,
        "tugboat_user_correction_recurrence",
        summary.get("user_correction_recurrence"),
    )
    _append_prefixed_numeric_metrics(
        lines,
        "tugboat_recurring_incident_rate",
        summary.get("recurring_incident_rate"),
    )
    _append_auto_apply_lane_metrics(lines, summary.get("auto_apply_lanes"))
    _append_daemon_queue_metrics(lines, summary.get("daemon_queue"))
    return "\n".join(lines) + ("\n" if lines else "")


def _append_auto_apply_lane_metrics(lines: list[str], lanes: object) -> None:
    if not isinstance(lanes, dict):
        return
    for lane_name, lane_counts in sorted(lanes.items()):
        if not isinstance(lane_counts, dict):
            continue
        for state, value in sorted(lane_counts.items()):
            _append_metric(
                lines,
                "tugboat_auto_apply_lane_candidates_total",
                value,
                labels={"lane": str(lane_name), "state": str(state)},
            )


def _append_daemon_queue_metrics(lines: list[str], daemon_queue: object) -> None:
    if not isinstance(daemon_queue, dict):
        return
    jobs_by_state = daemon_queue.get("jobs_by_state")
    if isinstance(jobs_by_state, dict):
        for state, value in sorted(jobs_by_state.items()):
            _append_metric(
                lines,
                "tugboat_daemon_queue_jobs_total",
                value,
                labels={"state": str(state)},
            )
    _append_metric(
        lines,
        "tugboat_daemon_kill_switch_enabled",
        1 if daemon_queue.get("kill_switch_enabled") else 0,
    )
    _append_metric(lines, "tugboat_daemon_leased_job_count", daemon_queue.get("leased_job_count"))
    _append_metric(lines, "tugboat_daemon_stuck_job_count", daemon_queue.get("stuck_job_count"))


def _append_labelled_counter(
    lines: list[str],
    metric_name: str,
    label_name: str,
    values: object,
) -> None:
    if not isinstance(values, dict):
        return
    for label_value, value in sorted(values.items()):
        _append_metric(lines, metric_name, value, labels={label_name: str(label_value)})


def _append_prefixed_numeric_metrics(
    lines: list[str],
    metric_prefix: str,
    values: object,
) -> None:
    if not isinstance(values, dict):
        return
    for key, value in sorted(values.items()):
        _append_metric(lines, f"{metric_prefix}_{key}", value)


def _append_metric(
    lines: list[str],
    metric_name: str,
    value: object,
    *,
    labels: dict[str, str] | None = None,
) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return
    label_text = ""
    if labels:
        label_text = "{" + ",".join(
            f'{name}="{_escape_metric_label(label_value)}"'
            for name, label_value in sorted(labels.items())
        ) + "}"
    lines.append(f"{metric_name}{label_text} {_format_metric_value(value)}")


def _escape_metric_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_metric_value(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if value.is_integer():
        return str(int(value))
    return str(value)


def _daemon_queue_status(repo: Path) -> dict[str, Any]:
    from tugboat.daemon.service import daemon_status, default_kill_switch

    status = daemon_status(repo, kill_switch=default_kill_switch(repo))
    return {
        "jobs_by_state": status["jobs_by_state"],
        "oldest_queued_job_id": status["oldest_queued_job_id"],
        "kill_switch_enabled": status["kill_switch_enabled"],
        "leased_job_count": status.get("leased_job_count", 0),
        "stuck_job_count": status.get("stuck_job_count", 0),
        "oldest_stuck_job_id": status.get("oldest_stuck_job_id"),
        "oldest_stuck_lease_expires_at": status.get("oldest_stuck_lease_expires_at"),
        "recovery_hint": status.get("recovery_hint"),
    }


def _auto_apply_lane_names(repo: Path) -> tuple[str, ...]:
    try:
        return tuple(lane.name for lane in load_policy(repo).auto_apply_lanes)
    except (OSError, ValueError):
        return ()


def _auto_apply_paused_lane_names(repo: Path) -> tuple[str, ...]:
    try:
        policy = load_policy(repo)
        paused_lanes = {*policy.auto_apply_paused_lanes}
        paused_lanes.update(lane.name for lane in policy.auto_apply_lanes if not lane.enabled)
        return tuple(sorted(paused_lanes))
    except (OSError, ValueError):
        return ()


def _auto_apply_lane_counts(
    events: Iterable[dict[str, Any]],
    *,
    lane_names: Iterable[str],
    paused: bool,
    paused_lanes: Iterable[str],
) -> dict[str, dict[str, int]]:
    counts = {_lane_name(lane): _empty_auto_apply_lane_counts() for lane in lane_names}
    paused_lane_set = {_lane_name(lane) for lane in paused_lanes}
    candidate_lanes: dict[str, str] = {}
    shadowed_candidates: set[tuple[str, str]] = set()
    eligible_candidates: set[tuple[str, str]] = set()
    rejected_candidates: set[tuple[str, str]] = set()
    staged_candidates: set[tuple[str, str]] = set()
    applied_candidates: set[tuple[str, str]] = set()
    rolled_back_candidates: set[tuple[str, str]] = set()
    paused_candidates: set[tuple[str, str]] = set()

    event_items = list(events)
    for event in event_items:
        if str(event.get("event_type", "")) == "auto_apply.shadowed":
            candidate_id = _candidate_id(event)
            lane = _lane_name(event.get("lane"))
            if candidate_id is None:
                continue
            counts.setdefault(lane, _empty_auto_apply_lane_counts())
            candidate_lanes[candidate_id] = lane
            shadowed_candidates.add((lane, candidate_id))
            continue
        if str(event.get("event_type", "")) != "auto_apply.decided":
            continue
        candidate_id = _candidate_id(event)
        lane = _lane_name(event.get("lane"))
        if candidate_id is None:
            continue
        counts.setdefault(lane, _empty_auto_apply_lane_counts())
        candidate_lanes[candidate_id] = lane
        key = (lane, candidate_id)
        if bool(event.get("eligible", False)):
            eligible_candidates.add(key)
            if str(event.get("phase", "")) == "precheck":
                staged_candidates.add(key)
        elif _auto_apply_pause_reasons(event):
            paused_candidates.add(key)
        else:
            rejected_candidates.add(key)

    for event in event_items:
        event_type = str(event.get("event_type", ""))
        candidate_id = _candidate_id(event)
        if candidate_id is None:
            continue
        if event_type == "auto_apply.applied":
            lane = _lane_name(_auto_apply_applied_lane(event) or candidate_lanes.get(candidate_id))
            counts.setdefault(lane, _empty_auto_apply_lane_counts())
            candidate_lanes[candidate_id] = lane
            applied_candidates.add((lane, candidate_id))
        elif event_type == "rollback.applied" and candidate_id in candidate_lanes:
            rolled_back_candidates.add((candidate_lanes[candidate_id], candidate_id))

    for lane, candidate_id in shadowed_candidates:
        counts[lane]["shadowed"] += 1
    for lane, candidate_id in eligible_candidates:
        counts[lane]["eligible"] += 1
    for lane, candidate_id in rejected_candidates:
        if (lane, candidate_id) not in eligible_candidates:
            counts[lane]["rejected"] += 1
    for lane, candidate_id in staged_candidates:
        counts[lane]["staged"] += 1
    for lane, candidate_id in applied_candidates:
        counts[lane]["applied"] += 1
    for lane, candidate_id in rolled_back_candidates:
        counts[lane]["rolled_back"] += 1
    for lane, lane_counts in counts.items():
        lane_paused_candidates = {
            candidate_id
            for paused_lane, candidate_id in paused_candidates
            if paused_lane == lane
        }
        if paused or lane in paused_lane_set:
            lane_paused_candidates.update(
                candidate_id
                for staged_lane, candidate_id in staged_candidates
                if staged_lane == lane
            )
            lane_paused_candidates.difference_update(
                candidate_id
                for applied_lane, candidate_id in applied_candidates
                if applied_lane == lane
            )
        lane_counts["paused"] = len(lane_paused_candidates)
    return {lane: counts[lane] for lane in sorted(counts)}


def _empty_auto_apply_lane_counts() -> dict[str, int]:
    return {
        "shadowed": 0,
        "eligible": 0,
        "rejected": 0,
        "staged": 0,
        "applied": 0,
        "rolled_back": 0,
        "paused": 0,
    }


def _candidate_id(event: dict[str, Any]) -> str | None:
    value = event.get("candidate_id")
    if value is None:
        return None
    return str(value)


def _lane_name(value: object) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "unmatched"


def _auto_apply_applied_lane(event: dict[str, Any]) -> str | None:
    approval_bundle = event.get("approval_bundle")
    if isinstance(approval_bundle, dict):
        lane = approval_bundle.get("lane")
        if isinstance(lane, str) and lane.strip():
            return lane.strip()
    lane = event.get("lane")
    if isinstance(lane, str) and lane.strip():
        return lane.strip()
    return None


def _auto_apply_pause_reasons(event: dict[str, Any]) -> tuple[str, ...]:
    reasons = event.get("reasons", ())
    if not isinstance(reasons, (list, tuple)):
        return ()
    pause_reasons = {
        "auto_apply_repository_paused",
        "auto_apply_lane_paused",
        "auto_apply_category_paused",
        "auto_apply_incident_pause_active",
    }
    return tuple(reason for reason in reasons if str(reason) in pause_reasons)


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


def _finding_text(finding: object) -> str:
    if isinstance(finding, dict):
        return str(finding.get("finding", ""))
    return str(finding)


def _finding_severity(finding: object) -> str:
    if isinstance(finding, dict):
        return str(finding.get("severity", ""))
    return ""


def _duplicate_rule_count(harness_findings: Iterable[object]) -> int:
    return sum(
        1
        for finding in harness_findings
        if _finding_text(finding).startswith("Duplicate instruction rule appears ")
    )


def _stale_doc_count(harness_findings: Iterable[object]) -> int:
    return sum(
        1
        for finding in harness_findings
        if _finding_severity(finding) == "stale_doc"
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
