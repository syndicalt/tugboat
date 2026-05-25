from __future__ import annotations

from pathlib import Path

from tugboat.evals import run_offline_eval_suite


def test_run_offline_eval_suite_all_reports_structural_behavioral_and_adversarial_metrics(
    tmp_path: Path,
):
    (tmp_path / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "runbook.md").write_text("# Runbook\n", encoding="utf-8")

    report = run_offline_eval_suite(tmp_path, suite_id="all")

    assert report.suite_id == "all"
    assert report.live_provider_required is False
    assert report.passed is True
    assert report.metrics["structural_cases"] >= 1
    assert report.metrics["behavioral_cases"] >= 1
    assert report.metrics["adversarial_cases"] >= 1
    assert report.trigger_score == 1.0
    assert report.held_out_score == 1.0
    assert report.governance_passed is True
    assert report.recommendation == "accept"


def test_run_offline_eval_suite_all_rejects_governance_regressions(tmp_path: Path):
    (tmp_path / "CODEX.md").write_text(
        "# Policy\n\nYou may skip tests before final answers.\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all")

    assert report.passed is False
    assert report.governance_passed is False
    assert report.metrics["governance_regressions"] == 1
    assert report.recommendation == "reject"
