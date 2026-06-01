from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from shutil import copytree

from tugboat.cli import main
from tugboat.db import Store
from tugboat.policy.gate import CandidatePatch, SourceRef


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


def _write_skill_candidate_preview(run_dir: Path, text: str) -> None:
    preview_root = run_dir / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "SKILL.md").write_text(text, encoding="utf-8")
    preview_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    (run_dir / "candidate-preview.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_file": "SKILL.md",
                "base_hash": "base",
                "diff_hash": "diff",
                "preview_path": f".sidecar/runs/{run_dir.name}/candidate-preview/SKILL.md",
                "preview_hash": preview_hash,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _repo_from_run_dir(run_dir: Path) -> Path:
    return run_dir.parents[2]


def _seed_candidate_row(run_dir: Path) -> tuple[int, int]:
    repo = _repo_from_run_dir(run_dir)
    diff_path = run_dir / "candidate.diff"
    diff = "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use regression tests.\n"
    run_dir.mkdir(parents=True, exist_ok=True)
    diff_path.write_text(diff, encoding="utf-8")
    with Store.open(repo / ".sidecar" / "db.sqlite") as store:
        store.insert_run(
            run_id=run_dir.name,
            stage="proposal",
            manifest_hash="fixture-manifest",
            status="completed",
            run_dir=run_dir,
        )
        audit_id = store.insert_audit(
            run_id=run_dir.name,
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=["ev_fixture"],
            instruction_refs=["CODEX.md"],
        )
        candidate = CandidatePatch(
            audit_id=audit_id,
            base_file="CODEX.md",
            base_hash="base",
            diff=diff,
            risk_class="instruction_clarification",
            rationale="Fixture candidate for offline eval.",
            sources=(SourceRef("ev_fixture", trusted=True),),
        )
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=candidate,
            diff_path=diff_path,
            state="needs_review",
        )
    return audit_id, candidate_id


def _write_candidate_json(run_dir: Path) -> int:
    audit_id, candidate_id = _seed_candidate_row(run_dir)
    (run_dir / "candidate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": candidate_id,
                "audit_id": audit_id,
                "base_file": "CODEX.md",
                "base_hash": "base",
                "diff_hash": "diff",
                "expected_behavior_change": "Clarifies the testing obligation.",
                "evals_required": ["all"],
                "risk_class": "instruction_clarification",
                "rationale": "Fixture candidate for offline eval.",
                "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
                "sources": [{"source_id": "ev_fixture", "trusted": True}],
                "bounded_edit_metadata": [
                    {
                        "operator": "replace",
                        "file": "CODEX.md",
                        "section": "Policy",
                        "changed_lines": 1,
                        "normative_changes": 0,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return candidate_id


def _install_eval_fixture(repo: Path, relative_path: str) -> None:
    target = repo / ".sidecar" / "evals" / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        (FIXTURES / "passing" / relative_path).read_text(encoding="utf-8"),
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
    candidate_id = _write_candidate_json(run_dir)
    _, rejected_candidate_id = _seed_candidate_row(run_dir)
    (repo / ".sidecar").mkdir(exist_ok=True)
    copytree(FIXTURES / "passing", repo / ".sidecar" / "evals")
    with Store.open(repo / ".sidecar" / "db.sqlite") as store:
        store.insert_decision(
            candidate_id=candidate_id,
            actor="reviewer",
            policy="apply_controller",
            decision="applied",
            reason="accepted",
        )
        store.insert_decision(
            candidate_id=rejected_candidate_id,
            actor="reviewer",
            policy="apply_controller",
            decision="rejected",
            reason="too broad",
        )
        store.append_audit_event(
            "apply.applied",
            {"candidate_id": candidate_id, "changed_lines": 6},
        )
        store.append_audit_event("rollback.applied", {"candidate_id": 9})
        for run_id in ("incident-1", "incident-2"):
            store.insert_run(
                run_id=run_id,
                stage="audit",
                manifest_hash="fixture-manifest",
                status="completed",
                run_dir=repo / ".sidecar" / "runs" / run_id,
            )
        store.insert_audit(
            run_id="incident-1",
            failure_class="missing_tests",
            severity="medium",
            confidence=0.9,
            evidence_refs=["ev-1"],
            instruction_refs=[],
        )
        store.insert_audit(
            run_id="incident-2",
            failure_class="missing_tests",
            severity="medium",
            confidence=0.8,
            evidence_refs=["ev-2"],
            instruction_refs=[],
        )
        store.record_harness_finding(
            repo_path=repo,
            finding="Duplicate instruction rule appears 2 times: run tests.",
            severity="duplicate_rule",
        )
        store.record_harness_finding(
            repo_path=repo,
            finding="docs/runbook.md is missing ownership metadata.",
            severity="stale_doc",
        )

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["suite_id"] == "all"
    assert report["trigger_score"] == 1.0
    assert report["held_out_score"] == 1.0
    assert report["governance_passed"] is True
    assert report["recommendation"] == "reject"
    assert report["longitudinal_metrics"] == {
        "acceptance_rate": 0.333333,
        "corpus_growth": 0,
        "duplicate_rule_count": 1,
        "governance_regression_count": 0,
        "mean_changed_lines": 6,
        "recurring_incident_rate": 1,
        "rejection_rate": 0.333333,
        "rollback_rate": 0.333333,
        "stale_doc_count": 1,
        "user_correction_recurrence": 0,
    }
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

    assert eval_run[:4] == (
        candidate_id,
        "all",
        "failed",
        str(run_dir / "eval-report.json"),
    )
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


def test_eval_suite_all_rejects_candidate_when_required_phase4_categories_are_missing(
    tmp_path: Path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "CODEX.md").write_text(
        "# Policy\n\nYou may skip tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nYou must run tests before final answers.\n",
    )
    _write_candidate_json(run_dir)
    for relative_path in (
        "incident-replay/preserve-test-obligation.json",
        "held-out/no-regression.json",
        "cross-agent/codex-claude-shared-obligation.json",
        "adversarial/reject-skip-tests.json",
    ):
        _install_eval_fixture(repo, relative_path)

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["recommendation"] == "reject"
    assert report["metrics"]["phase_4_fixture_categories_missing"] == 3
    assert report["metrics"]["common_obligation_cases"] == 0
    assert report["metrics"]["final_answer_evidence_cases"] == 0
    assert report["metrics"]["tool_permission_boundary_cases"] == 0


def test_eval_suite_all_writes_skill_report_for_skill_preview(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "SKILL.md").write_text(
        "---\n"
        "name: python-review\n"
        "description: Use when reviewing Python changes.\n"
        "---\n"
        "# Python Review\n\n"
        "## When to Use\n\n"
        "Use when reviewing Python changes.\n\n"
        "## Instructions\n\n"
        "You may skip tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_skill_candidate_preview(
        run_dir,
        "---\n"
        "name: python-review\n"
        "description: Use when reviewing Python changes.\n"
        "---\n"
        "# Python Review\n\n"
        "## When to Use\n\n"
        "Use when reviewing Python changes.\n\n"
        "## Instructions\n\n"
        "You must run tests before final answers. Always cite verification evidence.\n",
    )
    _write_candidate_json(run_dir)
    copytree(FIXTURES / "passing", repo / ".sidecar" / "evals")

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 0

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["recommendation"] == "accept"
    assert report["skill_report"]["passed"] is True
    assert report["skill_report"]["skill_path"] == "SKILL.md"
    assert report["skill_report"]["findings"] == []
    assert report["metrics"]["skill_rewrite_cases"] == 1
    assert "skill-rewrite:candidate-preview:SKILL.md" in [
        case["case_id"] for case in report["eval_cases"]
    ]


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


def test_eval_suite_all_uses_scoped_instruction_files(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    scoped = repo / "services" / "web" / "CODEX.md"
    scoped.parent.mkdir(parents=True)
    scoped.write_text(
        "# Policy\n\nYou must run browser fixture tests.\n",
        encoding="utf-8",
    )
    sidecar = repo / ".sidecar"
    sidecar.mkdir()
    (sidecar / "policy.yaml").write_text(
        """
version: 1
instruction_files:
  - path: CODEX.md
    kind: agent_policy
    precedence: 70
    protected: true
    scope_root: services/web
""".lstrip(),
        encoding="utf-8",
    )
    copytree(FIXTURES / "passing", sidecar / "evals")
    run_dir = sidecar / "runs" / "run-1"
    preview = run_dir / "candidate-preview" / "services" / "web" / "CODEX.md"
    preview.parent.mkdir(parents=True)
    preview_text = "# Policy\n\nYou must run browser fixture tests.\n"
    preview.write_text(preview_text, encoding="utf-8")
    diff = (
        "--- a/services/web/CODEX.md\n"
        "+++ b/services/web/CODEX.md\n"
        "@@ -1,3 +1,3 @@\n"
        " # Policy\n"
        " \n"
        "-You must run browser fixture tests.\n"
        "+You must run browser fixture tests.\n"
    )
    (run_dir / "candidate.diff").write_text(diff, encoding="utf-8")
    with Store.open(sidecar / "db.sqlite") as store:
        store.insert_run(
            run_id=run_dir.name,
            stage="proposal",
            manifest_hash="fixture-manifest",
            status="completed",
            run_dir=run_dir,
        )
        audit_id = store.insert_audit(
            run_id=run_dir.name,
            failure_class="instruction_missing",
            severity="medium",
            confidence=0.75,
            evidence_refs=["ev_fixture"],
            instruction_refs=["services/web/CODEX.md"],
        )
        candidate = CandidatePatch(
            audit_id=audit_id,
            base_file="services/web/CODEX.md",
            base_hash="base",
            diff=diff,
            risk_class="instruction_clarification",
            rationale="Fixture candidate for scoped offline eval.",
            scope_root="services/web",
            sources=(SourceRef("ev_fixture", trusted=True),),
            bounded_edit_metadata=(
                {
                    "operator": "replace",
                    "file": "services/web/CODEX.md",
                    "section": "Policy",
                    "changed_lines": 1,
                    "normative_changes": 0,
                    "scope_root": "services/web",
                },
            ),
        )
        candidate_id = store.insert_candidate(
            audit_id=audit_id,
            candidate=candidate,
            diff_path=run_dir / "candidate.diff",
            state="needs_review",
        )
    (run_dir / "candidate.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": candidate_id,
                "audit_id": audit_id,
                "base_file": "services/web/CODEX.md",
                "base_hash": "base",
                "diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                "expected_behavior_change": "Keeps scoped browser test guidance.",
                "evals_required": ["all"],
                "risk_class": "instruction_clarification",
                "rationale": "Fixture candidate for scoped offline eval.",
                "rollback_plan": ["tugboat", "rollback", "--decision", "latest"],
                "scope_root": "services/web",
                "sources": [{"source_id": "ev_fixture", "trusted": True}],
                "bounded_edit_metadata": [
                    {
                        "operator": "replace",
                        "file": "services/web/CODEX.md",
                        "section": "Policy",
                        "changed_lines": 1,
                        "normative_changes": 0,
                        "scope_root": "services/web",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "candidate-preview.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_file": "services/web/CODEX.md",
                "base_hash": "base",
                "diff_hash": "diff",
                "scope_root": "services/web",
                "preview_path": f".sidecar/runs/{run_dir.name}/candidate-preview/services/web/CODEX.md",
                "preview_hash": hashlib.sha256(preview_text.encode("utf-8")).hexdigest(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["eval", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    assert report["metrics"]["candidate_preview_files"] == 1
    case_ids = {case["case_id"] for case in report["eval_cases"]}
    assert "structural:candidate-preview:services/web/CODEX.md" in case_ids


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
