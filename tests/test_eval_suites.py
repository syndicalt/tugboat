from __future__ import annotations

from pathlib import Path
from shutil import copytree

from tugboat.evals import run_offline_eval_suite


FIXTURES = Path(__file__).parent / "fixtures" / "evals"


def _install_eval_fixtures(repo: Path, fixture_name: str) -> None:
    (repo / ".sidecar").mkdir()
    copytree(FIXTURES / fixture_name, repo / ".sidecar" / "evals")


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
    assert [case.case_id for case in report.eval_cases[:1]] == [
        "structural:current-policy:CODEX.md"
    ]


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


def test_run_offline_eval_suite_all_allows_restrictive_deploy_approval_policy(
    tmp_path: Path,
):
    (tmp_path / "CODEX.md").write_text(
        "# Policy\n\nAgents must not bypass deploy approval.\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all")

    assert report.passed is True
    assert report.governance_passed is True
    assert report.metrics["governance_regressions"] == 0


def test_run_offline_eval_suite_all_allows_non_instruction_html_comments(
    tmp_path: Path,
):
    (tmp_path / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n\n<!-- owner: platform -->\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all")

    assert report.passed is True
    assert report.governance_passed is True
    assert report.metrics["governance_regressions"] == 0


def test_run_offline_eval_suite_all_evaluates_candidate_preview_instead_of_current_repo_file(
    tmp_path: Path,
):
    (tmp_path / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    preview_root = tmp_path / ".sidecar" / "runs" / "run-1" / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "CODEX.md").write_text(
        "# Policy\n\nYou may skip tests before final answers.\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all", preview_root=preview_root)

    assert report.passed is False
    assert report.metrics["candidate_preview_files"] == 1
    assert report.metrics["governance_regressions"] == 1
    assert [case.case_id for case in report.eval_cases[:1]] == [
        "structural:candidate-preview:CODEX.md"
    ]


def test_run_offline_eval_suite_all_compares_preview_against_original_instruction_file(
    tmp_path: Path,
):
    (tmp_path / "CODEX.md").write_text(
        "---\nowner: platform\n---\n# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    preview_root = tmp_path / ".sidecar" / "runs" / "run-1" / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "CODEX.md").write_text(
        "# Renamed Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all", preview_root=preview_root)

    assert report.passed is False
    assert report.metrics["candidate_preview_files"] == 1
    assert report.metrics["structural_findings"] == 2
    assert report.trigger_score == 0.0
    assert report.recommendation == "reject"


def test_run_offline_eval_suite_all_evaluates_full_instruction_preview_corpus(
    tmp_path: Path,
):
    preview_root = tmp_path / ".sidecar" / "runs" / "run-1" / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    (preview_root / "SKILL.md").write_text(
        "# Skill\n\nYou may skip tests before final answers.\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all", preview_root=preview_root)

    assert report.passed is False
    assert report.metrics["structural_cases"] == 2
    assert report.metrics["candidate_preview_files"] == 2
    assert report.metrics["governance_regressions"] == 1
    assert [case.case_id for case in report.eval_cases[:2]] == [
        "structural:candidate-preview:CODEX.md",
        "structural:candidate-preview:SKILL.md",
    ]


def test_run_offline_eval_suite_all_overlays_partial_preview_on_repo_corpus(
    tmp_path: Path,
):
    (tmp_path / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text(
        "# Agent Policy\n\nYou may skip tests before final answers.\n",
        encoding="utf-8",
    )
    preview_root = tmp_path / ".sidecar" / "runs" / "run-1" / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all", preview_root=preview_root)

    assert report.passed is False
    assert report.metrics["structural_cases"] == 2
    assert report.metrics["candidate_preview_files"] == 1
    assert report.metrics["governance_regressions"] == 1
    assert [case.case_id for case in report.eval_cases[:2]] == [
        "structural:candidate-preview:CODEX.md",
        "structural:candidate-preview:AGENTS.md",
    ]


def test_run_offline_eval_suite_all_resolves_preview_only_links_against_preview_corpus(
    tmp_path: Path,
):
    preview_root = tmp_path / ".sidecar" / "runs" / "run-1" / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "CODEX.md").write_text(
        "# Policy\n\nSee [skill](SKILL.md).\n",
        encoding="utf-8",
    )
    (preview_root / "SKILL.md").write_text(
        "# Skill\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all", preview_root=preview_root)

    assert report.passed is True
    assert report.metrics["structural_cases"] == 2
    assert report.metrics["candidate_preview_files"] == 2
    assert report.metrics["structural_findings"] == 0


def test_run_offline_eval_suite_all_emits_per_file_current_policy_cases(
    tmp_path: Path,
):
    (tmp_path / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    (tmp_path / "SKILL.md").write_text(
        "# Skill\n\nYou must inspect evidence before final answers.\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all")

    assert report.passed is True
    assert report.metrics["structural_cases"] == 2
    assert [case.case_id for case in report.eval_cases[:2]] == [
        "structural:current-policy:CODEX.md",
        "structural:current-policy:SKILL.md",
    ]


def test_run_offline_eval_suite_all_can_run_from_preview_when_repo_file_is_missing(
    tmp_path: Path,
):
    preview_root = tmp_path / ".sidecar" / "runs" / "run-1" / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )

    report = run_offline_eval_suite(tmp_path, suite_id="all", preview_root=preview_root)

    assert report.passed is True
    assert report.metrics["candidate_preview_files"] == 1
    assert [case.case_id for case in report.eval_cases[:1]] == [
        "structural:candidate-preview:CODEX.md"
    ]


def test_run_offline_eval_suite_all_loads_fixture_backed_phase_4_cases(tmp_path: Path):
    (tmp_path / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    _install_eval_fixtures(tmp_path, "passing")

    report = run_offline_eval_suite(tmp_path, suite_id="all")

    assert report.passed is True
    assert report.metrics["incident_replay_cases"] == 1
    assert report.metrics["held_out_cases"] == 1
    assert report.metrics["adversarial_cases"] == 3
    assert report.metrics["cross_agent_cases"] == 1
    assert report.metrics["behavioral_cases"] == 3
    assert report.metrics["fixture_case_failures"] == 0
    assert report.trigger_score == 1.0
    assert report.held_out_score == 1.0


def test_run_offline_eval_suite_all_rejects_failing_fixture_cases(tmp_path: Path):
    (tmp_path / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    _install_eval_fixtures(tmp_path, "failing")

    report = run_offline_eval_suite(tmp_path, suite_id="all")

    assert report.passed is False
    assert report.metrics["held_out_cases"] == 1
    assert report.metrics["fixture_case_failures"] == 1
    assert report.held_out_score == 0.0
    assert report.recommendation == "reject"
