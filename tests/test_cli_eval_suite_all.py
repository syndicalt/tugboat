from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from shutil import copytree

from tugboat.cli import main


FIXTURES = Path(__file__).parent / "fixtures" / "evals"


def _write_candidate_preview(run_dir: Path, text: str, *, preview_hash: str | None = None) -> None:
    preview_root = run_dir / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "CODEX.md").write_text(text, encoding="utf-8")
    preview_hash = preview_hash or hashlib.sha256(text.encode("utf-8")).hexdigest()
    (run_dir / "candidate-preview.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_file": "CODEX.md",
                "base_hash": "base",
                "diff_hash": "diff",
                "preview_path": f".sidecar/runs/{run_dir.name}/candidate-preview/CODEX.md",
                "preview_hash": preview_hash,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_candidate_json(run_dir: Path) -> None:
    (run_dir / "candidate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 7,
                "audit_id": 1,
                "base_file": "CODEX.md",
                "base_hash": "base",
                "diff_hash": "diff",
                "expected_behavior_change": "Clarifies the testing obligation.",
                "evals_required": ["all"],
                "risk_class": "instruction_clarification",
                "rationale": "Fixture candidate for offline eval.",
                "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
                "sources": [{"source_id": "ev_fixture", "trusted": True}],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_eval_suite_all_runs_offline_and_writes_recommendation_metrics(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nYou must run tests before final answers.\n",
    )
    _write_candidate_json(run_dir)
    (repo / ".sidecar").mkdir(exist_ok=True)
    copytree(FIXTURES / "passing", repo / ".sidecar" / "evals")

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["suite_id"] == "all"
    assert report["trigger_score"] == 1.0
    assert report["held_out_score"] == 1.0
    assert report["governance_passed"] is True
    assert report["recommendation"] == "reject"
    assert report["metrics"]["held_out_improved"] == 0
    assert report["metrics"]["incident_replay_passed"] == 1
    assert "trigger_score" not in report["metrics"]
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        eval_run = connection.execute(
            """
            SELECT candidate_id, suite_id, status, report_path, audit_event_sequence
            FROM eval_runs
            """
        ).fetchone()
        eval_cases = connection.execute(
            """
            SELECT case_id, case_hash, audit_event_sequence
            FROM eval_cases
            WHERE suite_id = 'all'
            ORDER BY case_id
            """
        ).fetchall()
        validation_splits = connection.execute(
            """
            SELECT split_name, case_ids_json, audit_event_sequence
            FROM validation_splits
            WHERE suite_id = 'all'
            ORDER BY split_name
            """
        ).fetchall()

    assert eval_run[:4] == (7, "all", "failed", str(run_dir / "eval-report.json"))
    assert eval_run[4] is not None
    assert [row[0] for row in eval_cases] == [
        "adversarial:reject-emergency-deploy-bypass",
        "adversarial:reject-eval-leakage",
        "adversarial:reject-final-answer-evidence-omission",
        "adversarial:reject-forged-success-claim",
        "adversarial:reject-hidden-prompt-injection",
        "adversarial:reject-malicious-issue-text",
        "adversarial:reject-poisoned-command-output",
        "adversarial:reject-skip-tests",
        "adversarial:reject-tool-permission-escalation",
        "common_obligation:preserve-required-test-command",
        "cross_agent:codex-claude-shared-obligation",
        "final_answer_evidence:cite-verification-in-final-answer",
        "held_out:no-regression",
        "incident_replay:preserve-test-obligation",
        "structural:candidate-preview:CODEX.md",
        "tool_permission_boundary:require-approval-before-tool-permission",
    ]
    assert all(len(row[1]) == 64 and row[2] is not None for row in eval_cases)
    split_payloads = {row[0]: json.loads(row[1]) for row in validation_splits}
    assert split_payloads["trigger"] == [
        "common_obligation:preserve-required-test-command",
        "final_answer_evidence:cite-verification-in-final-answer",
        "incident_replay:preserve-test-obligation",
        "structural:candidate-preview:CODEX.md",
        "tool_permission_boundary:require-approval-before-tool-permission",
    ]
    assert split_payloads["held_out"] == ["held_out:no-regression"]
    assert split_payloads["governance"] == [
        "adversarial:reject-emergency-deploy-bypass",
        "adversarial:reject-eval-leakage",
        "adversarial:reject-final-answer-evidence-omission",
        "adversarial:reject-forged-success-claim",
        "adversarial:reject-hidden-prompt-injection",
        "adversarial:reject-malicious-issue-text",
        "adversarial:reject-poisoned-command-output",
        "adversarial:reject-skip-tests",
        "adversarial:reject-tool-permission-escalation",
        "cross_agent:codex-claude-shared-obligation",
    ]
    assert all(row[2] is not None for row in validation_splits)


def test_eval_suite_all_returns_nonzero_for_governance_regression(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Policy\n\nYou may skip tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nYou may skip tests before final answers.\n",
    )
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["metrics"]["governance_regressions"] == 1
    assert report["recommendation"] == "reject"


def test_eval_suite_all_uses_candidate_preview_artifact_for_report_and_db_rows(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nYou may skip tests before final answers.\n",
    )
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["recommendation"] == "reject"
    assert report["metrics"]["candidate_preview_files"] == 1
    assert report["metrics"]["governance_regressions"] == 1
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        eval_cases = connection.execute(
            """
            SELECT case_id, audit_event_sequence
            FROM eval_cases
            WHERE suite_id = 'all'
            """
        ).fetchall()
        eval_run = connection.execute(
            """
            SELECT status, audit_event_sequence
            FROM eval_runs
            """
        ).fetchone()

    assert ("structural:candidate-preview:CODEX.md", eval_cases[0][1]) in eval_cases
    assert eval_run[0] == "failed"
    assert eval_run[1] is not None


def test_eval_suite_all_rejects_missing_candidate_preview_without_repo_fallback(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    assert not (run_dir / "eval-report.json").exists()


def test_eval_suite_all_rejects_candidate_preview_hash_mismatch(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Policy\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nYou must run tests before final answers.\n",
        preview_hash="not-the-preview-hash",
    )
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    assert not (run_dir / "eval-report.json").exists()


def test_eval_rejects_unsupported_offline_suite_without_accepting_report(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "unknown-suite"]) == 1

    assert not (run_dir / "eval-report.json").exists()


def test_eval_provider_smoke_requires_explicit_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TUGBOAT_PROVIDER_SMOKE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "provider-smoke"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["suite_id"] == "provider-smoke"
    assert report["live_provider_required"] is True
    assert report["recommendation"] == "skip"
    assert report["metrics"] == {
        "provider_smoke_cases": 0,
        "provider_smoke_failures": 0,
        "provider_smoke_skipped": 1,
        "provider_smoke_opted_in": 0,
    }


def test_eval_provider_smoke_env_flag_without_repo_policy_still_skips(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TUGBOAT_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("TUGBOAT_PROVIDER_SMOKE_PROVIDER", "openai")
    repo = tmp_path / "repo"
    repo.mkdir()
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "provider-smoke"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["suite_id"] == "provider-smoke"
    assert report["live_provider_required"] is True
    assert report["recommendation"] == "skip"
    assert report["metrics"] == {
        "provider_smoke_cases": 0,
        "provider_smoke_failures": 0,
        "provider_smoke_skipped": 1,
        "provider_smoke_opted_in": 0,
    }


def test_eval_provider_smoke_policy_opt_in_reports_missing_provider_credentials(
    tmp_path: Path, monkeypatch
):
    monkeypatch.delenv("TUGBOAT_PROVIDER_SMOKE", raising=False)
    monkeypatch.delenv("TUGBOAT_PROVIDER_SMOKE_PROVIDER", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        """
version: 1
llmff:
  allowed_providers:
    - openai
provider_smoke:
  enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = sidecar / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "provider-smoke"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["suite_id"] == "provider-smoke"
    assert report["live_provider_required"] is True
    assert report["recommendation"] == "reject"
    assert report["metrics"] == {
        "provider_smoke_cases": 1,
        "provider_smoke_failures": 1,
        "provider_smoke_skipped": 0,
        "provider_smoke_opted_in": 1,
        "provider_smoke_configured": 0,
        "provider_smoke_missing_credentials": 1,
    }


def test_eval_provider_smoke_policy_opt_in_runs_env_configured_smoke_command_and_passes(
    tmp_path: Path,
    monkeypatch,
):
    smoke = tmp_path / "provider_smoke.py"
    smoke.write_text("raise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.setenv("TUGBOAT_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("TUGBOAT_PROVIDER_SMOKE_PROVIDER", "openai")
    monkeypatch.setenv("TUGBOAT_PROVIDER_SMOKE_COMMAND", f"{sys.executable} {smoke}")
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        """
version: 1
llmff:
  allowed_providers:
    - openai
provider_smoke:
  enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = sidecar / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "provider-smoke"]) == 0

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["suite_id"] == "provider-smoke"
    assert report["live_provider_required"] is True
    assert report["trigger_score"] == 1.0
    assert report["held_out_score"] == 1.0
    assert report["governance_passed"] is True
    assert report["recommendation"] == "accept"
    assert report["metrics"] == {
        "provider_smoke_cases": 1,
        "provider_smoke_failures": 0,
        "provider_smoke_skipped": 0,
        "provider_smoke_opted_in": 1,
        "provider_smoke_configured": 1,
        "provider_smoke_missing_credentials": 0,
        "provider_smoke_runner_configured": 1,
        "provider_smoke_exit_code": 0,
    }


def test_eval_provider_smoke_can_be_enabled_by_repo_policy_without_env_flags(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("TUGBOAT_PROVIDER_SMOKE", raising=False)
    monkeypatch.delenv("TUGBOAT_PROVIDER_SMOKE_PROVIDER", raising=False)
    monkeypatch.delenv("TUGBOAT_PROVIDER_SMOKE_COMMAND", raising=False)
    smoke = tmp_path / "provider_smoke.py"
    smoke.write_text("raise SystemExit(0)\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  allowed_providers:
    - grok
provider_smoke:
  enabled: true
  provider: grok
  command: "{sys.executable} {smoke}"
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = sidecar / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "provider-smoke"]) == 0

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["recommendation"] == "accept"
    assert report["metrics"]["provider_smoke_opted_in"] == 1
    assert report["metrics"]["provider_smoke_configured"] == 1
    assert report["metrics"]["provider_smoke_runner_configured"] == 1


def test_eval_provider_smoke_rejects_policy_provider_not_in_llmff_allowlist(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("TUGBOAT_PROVIDER_SMOKE", raising=False)
    monkeypatch.delenv("TUGBOAT_PROVIDER_SMOKE_PROVIDER", raising=False)
    smoke = tmp_path / "provider_smoke.py"
    smoke.write_text("raise SystemExit(0)\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  allowed_providers:
    - openai
provider_smoke:
  enabled: true
  provider: anthropic
  command: "{sys.executable} {smoke}"
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = sidecar / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "provider-smoke"]) == 1

    report_text = (run_dir / "eval-report.json").read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["passed"] is False
    assert report["recommendation"] == "reject"
    assert report["metrics"] == {
        "provider_smoke_cases": 1,
        "provider_smoke_failures": 1,
        "provider_smoke_skipped": 0,
        "provider_smoke_opted_in": 1,
        "provider_smoke_configured": 1,
        "provider_smoke_missing_credentials": 0,
        "provider_smoke_provider_allowed": 0,
    }
    assert "anthropic" not in report_text


def test_eval_provider_smoke_failure_records_sanitized_metrics_without_raw_provider_output(
    tmp_path: Path,
    monkeypatch,
):
    smoke = tmp_path / "provider_smoke.py"
    smoke.write_text(
        "print('provider raw output sk-secret-provider-payload-1234567890')\n"
        "raise SystemExit(42)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TUGBOAT_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("TUGBOAT_PROVIDER_SMOKE_PROVIDER", "anthropic")
    monkeypatch.setenv("TUGBOAT_PROVIDER_SMOKE_COMMAND", f"{sys.executable} {smoke}")
    repo = tmp_path / "repo"
    repo.mkdir()
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        """
version: 1
llmff:
  allowed_providers:
    - anthropic
provider_smoke:
  enabled: true
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = sidecar / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    _write_candidate_json(run_dir)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "provider-smoke"]) == 1

    report_text = (run_dir / "eval-report.json").read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["passed"] is False
    assert report["recommendation"] == "reject"
    assert report["metrics"] == {
        "provider_smoke_cases": 1,
        "provider_smoke_failures": 1,
        "provider_smoke_skipped": 0,
        "provider_smoke_opted_in": 1,
        "provider_smoke_configured": 1,
        "provider_smoke_missing_credentials": 0,
        "provider_smoke_runner_configured": 1,
        "provider_smoke_exit_code": 42,
    }
    assert "sk-secret-provider-payload" not in report_text
