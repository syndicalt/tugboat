from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable


def summarize_observability(
    *,
    runs: Iterable[dict[str, Any]] = (),
    jobs: Iterable[dict[str, Any]] = (),
    evals: Iterable[dict[str, Any]] = (),
    corpus_snapshots: Iterable[dict[str, Any]] = (),
    harness_findings: Iterable[str] = (),
) -> dict[str, Any]:
    run_items = list(runs)
    job_items = list(jobs)
    edit_counts = _edit_counts(job_items)
    return {
        "run_duration": _run_duration_summary(run_items),
        "failure_kind_counts": dict(sorted(_failure_kind_counts(run_items).items())),
        "edits": edit_counts,
        "edit_rates": _edit_rates(edit_counts),
        "mean_changed_lines": _mean_changed_lines(job_items),
        "eval_suite_trends": _eval_suite_trends(evals),
        "corpus_growth": _corpus_growth(corpus_snapshots),
        "provider_backend_failure_rate": _provider_backend_failure_rate(run_items),
        "duplicate_rule_count": _duplicate_rule_count(harness_findings),
    }


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


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _round(value: float) -> int | float:
    rounded = round(value, 6)
    return int(rounded) if rounded.is_integer() else rounded
