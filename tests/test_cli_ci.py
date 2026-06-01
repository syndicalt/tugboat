from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from shutil import copytree

from tugboat.cli import main
from tugboat.db import Store
from tugboat.paths import sidecar_dir
from tugboat.policy.gate import CandidatePatch, SourceRef


FIXTURES = Path(__file__).parent / "fixtures" / "evals"


def _write_candidate_preview(run_dir: Path, text: str) -> None:
    preview_root = run_dir / "candidate-preview"
    preview_root.mkdir(parents=True)
    (preview_root / "CODEX.md").write_text(text, encoding="utf-8")
    preview_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
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


def _seed_candidate_row(run_dir: Path) -> tuple[int, int]:
    repo = run_dir.parents[2]
    diff = "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+Use regression tests.\n"
    diff_path = run_dir / "candidate.diff"
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
            rationale="Fixture candidate for CI eval.",
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
                "rationale": "Fixture candidate for CI eval.",
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


def test_ci_blocks_instruction_file_budget_failure_without_traceback_or_report(
    tmp_path: Path,
    capsys,
) -> None:
    sidecar = tmp_path / ".sidecar"
    docs = tmp_path / "docs"
    sidecar.mkdir()
    docs.mkdir()
    (docs / "one.md").write_text("# One\n\nFirst.\n", encoding="utf-8")
    (docs / "two.md").write_text("# Two\n\nSecond.\n", encoding="utf-8")
    (sidecar / "policy.yaml").write_text(
        """
version: 1
index:
  max_instruction_files: 1
instruction_files:
  - path: docs/**/*.md
    kind: repo_policy
    precedence: 50
    protected: true
""".lstrip(),
        encoding="utf-8",
    )

    exit_code = main(["ci", "--repo", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "ci blocked: instruction file budget exceeded: 2 discovered, limit 1" in output
    assert "Traceback" not in output
    assert not (sidecar / "ci" / "ci-report.json").exists()


def test_ci_blocks_future_sidecar_schema_before_writing_report(
    tmp_path: Path,
    capsys,
) -> None:
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    (sidecar / "version.json").write_text(
        json.dumps({"schema_version": 999}),
        encoding="utf-8",
    )

    exit_code = main(["ci", "--repo", str(tmp_path)])

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "ci blocked: sidecar schema version 999 is newer than supported" in output
    assert "Traceback" not in output
    assert not (sidecar / "ci" / "ci-report.json").exists()
    assert not (sidecar / "harness-report.json").exists()


def test_ci_check_writes_repo_local_artifact_and_audits_without_mutating(tmp_path: Path, capsys):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    agents = repo / "AGENTS.md"
    original = "# Agent Map\n\nSee [runbook](docs/runbook.md).\n"
    agents.write_text(original, encoding="utf-8")

    previous_umask = os.umask(0o022)
    try:
        assert main(["ci", "--repo", str(repo)]) == 0
    finally:
        os.umask(previous_umask)

    assert "ci: ok" in capsys.readouterr().out
    assert agents.read_text(encoding="utf-8") == original
    report_path = sidecar_dir(repo) / "ci" / "ci-report.json"
    assert report_path.stat().st_mode & 0o777 == 0o600
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report == {
        "schema_version": 1,
        "mode": "ci_check",
        "auto_apply": False,
        "checks": {
            "harness": {
                "passed": True,
                "findings": [],
                "report_path": ".sidecar/harness-report.json",
                "report_sha256": hashlib.sha256(
                    (sidecar_dir(repo) / "harness-report.json").read_bytes()
                ).hexdigest(),
                "doc_gardening_task_count": 0,
            },
            "harness_report": {
                "passed": True,
                "missing_docs": [],
                "stale_docs": [],
                "orphaned_runbooks": [],
                "recurring_failures_without_docs": [],
                "doc_gardening_tasks": [],
            },
            "index": {"passed": True, "indexed_documents": 1},
            "manifest_contracts": {"passed": True, "findings": []},
            "semantic_policy_lint": {"passed": True, "findings": []},
        },
    }
    harness_report = json.loads((sidecar_dir(repo) / "harness-report.json").read_text(encoding="utf-8"))
    assert harness_report["knowledge_map"] == {"AGENTS.md": ["docs/runbook.md"]}
    assert harness_report["doc_gardening_tasks"] == []
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        event = store.connection.execute(
            "SELECT event_type, payload_json FROM audit_events ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
    assert event[0] == "ci.check_completed"
    payload = json.loads(event[1])
    assert payload["artifact"] == ".sidecar/ci/ci-report.json"
    assert payload["artifact_sha256"] == hashlib.sha256(
        (sidecar_dir(repo) / "ci" / "ci-report.json").read_bytes()
    ).hexdigest()


def test_ci_check_blocks_secret_bearing_report_payload_without_writing(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
    )
    _write_candidate_json(run_dir)

    assert (
        main(
            [
                "ci",
                "--repo",
                str(repo),
                "--candidate",
                "run-1",
                "--suite",
                "sk-thissecretkeyvalue1234567890",
            ]
        )
        == 1
    )

    output = capsys.readouterr().out
    assert "ci blocked: secret scan failed" in output
    assert "sk-thissecret" not in output
    assert not (sidecar_dir(repo) / "ci" / "ci-report.json").exists()


def test_ci_check_returns_nonzero_and_reports_harness_findings(tmp_path: Path, capsys):
    repo = tmp_path
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [missing](docs/missing.md).\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo)]) == 1

    output = capsys.readouterr().out
    assert "ci: failed" in output
    assert "AGENTS.md references missing repo-local markdown file docs/missing.md." in output
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["harness"]["passed"] is False
    assert report["checks"]["harness"]["report_path"] == ".sidecar/harness-report.json"
    assert report["checks"]["harness"]["doc_gardening_task_count"] == 1
    assert (sidecar_dir(repo) / "harness-report.json").exists()


def test_ci_check_returns_nonzero_for_report_only_harness_debt(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (docs / "orphan.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Orphan\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo)]) == 1

    output = capsys.readouterr().out
    assert "ci: failed" in output
    assert "harness report failed" in output
    assert "docs/orphan.md" in output
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["harness"]["passed"] is True
    assert report["checks"]["harness_report"] == {
        "passed": False,
        "missing_docs": [],
        "stale_docs": [],
        "orphaned_runbooks": ["docs/orphan.md"],
        "recurring_failures_without_docs": [],
        "doc_gardening_tasks": [
            "Either reference docs/orphan.md from an instruction map or remove/archive it."
        ],
    }


def test_ci_check_returns_nonzero_for_semantic_policy_lint_findings(tmp_path: Path, capsys):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou may skip tests before final answers.\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo)]) == 1

    output = capsys.readouterr().out
    assert "ci: failed" in output
    assert "semantic policy lint failed" in output
    assert "CODEX.md:5 weakens governance term 'test' with permissive language." in output
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["harness"]["passed"] is True
    assert report["checks"]["semantic_policy_lint"] == {
        "passed": False,
        "findings": ["CODEX.md:5 weakens governance term 'test' with permissive language."],
    }


def test_ci_check_returns_nonzero_for_invalid_local_manifest_contract(
    tmp_path: Path,
    capsys,
):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    manifest_dir = repo / ".sidecar" / "manifests"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "episode-audit.yaml").write_text(
        """
name: episode-audit
inputs:
  trace: trace.jsonl
outputs:
  audit_report: audit.raw.json
output_artifacts:
  audit_report: unknown.raw.json
""".lstrip(),
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo)]) == 1

    output = capsys.readouterr().out
    assert "ci: failed" in output
    assert "manifest contract validation failed" in output
    assert "episode-audit.yaml output_artifacts.audit_report references unknown JSON artifact schema unknown.raw.json" in output
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["manifest_contracts"] == {
        "passed": False,
        "findings": [
            "episode-audit.yaml output_artifacts.audit_report references unknown JSON artifact schema unknown.raw.json"
        ],
    }


def test_ci_semantic_policy_lint_allows_restrictive_governance_language(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n\nYou must not skip tests.\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo)]) == 0

    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["semantic_policy_lint"] == {"passed": True, "findings": []}


def test_ci_semantic_policy_lint_flags_could_skip_governance_language(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n\nYou could skip tests.\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo)]) == 1

    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["semantic_policy_lint"]["findings"] == [
        "AGENTS.md:5 weakens governance term 'test' with permissive language."
    ]


def test_ci_semantic_policy_lint_allows_negated_can_skip_language(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n\nYou can't skip tests.\nYou can not skip reviews.\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo)]) == 0

    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["semantic_policy_lint"] == {"passed": True, "findings": []}


def test_ci_semantic_policy_lint_checks_policy_configured_instruction_globs(
    tmp_path: Path,
):
    repo = tmp_path
    skill_dir = repo / ".codex" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("You may skip tests before final answers.\n", encoding="utf-8")

    assert main(["ci", "--repo", str(repo)]) == 1

    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["semantic_policy_lint"] == {
        "passed": False,
        "findings": [
            ".codex/skills/demo/SKILL.md:1 weakens governance term 'test' with permissive language."
        ],
    }


def test_ci_semantic_policy_lint_reports_source_line_numbers_across_headings(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text(
        "# Agent Map\n\nSee [runbook](docs/runbook.md).\n\n## Rules\nYou may skip tests.\n",
        encoding="utf-8",
    )

    assert main(["ci", "--repo", str(repo)]) == 1

    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["semantic_policy_lint"]["findings"] == [
        "AGENTS.md:6 weakens governance term 'test' with permissive language."
    ]


def test_ci_check_runs_requested_eval_suite_and_records_scores(tmp_path: Path, capsys):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
    )
    _write_candidate_json(run_dir)
    (repo / ".sidecar").mkdir(exist_ok=True)
    copytree(FIXTURES / "passing", repo / ".sidecar" / "evals")

    assert main(["ci", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    assert "ci: failed" in capsys.readouterr().out
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["eval"] == {
        "passed": False,
        "candidate": "run-1",
        "suite_id": "all",
        "report_path": ".sidecar/runs/run-1/eval-report.json",
        "trigger_score": 1.0,
        "held_out_score": 1.0,
        "governance_passed": True,
        "recommendation": "reject",
    }


def test_ci_check_with_relative_repo_path_records_relative_eval_report_path(
    tmp_path: Path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
    )
    _write_candidate_json(run_dir)
    monkeypatch.chdir(tmp_path)

    assert main(["ci", "--repo", "repo", "--candidate", "run-1", "--suite", "all"]) == 1

    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["eval"]["report_path"] == ".sidecar/runs/run-1/eval-report.json"


def test_ci_check_fails_when_requested_eval_suite_fails(tmp_path: Path, capsys):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou may skip tests before final answers.\n",
    )
    _write_candidate_json(run_dir)

    assert main(["ci", "--repo", str(repo), "--candidate", "run-1", "--suite", "all"]) == 1

    output = capsys.readouterr().out
    assert "ci: failed" in output
    assert "eval suite all failed" in output
    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["harness"]["passed"] is True
    assert report["checks"]["eval"]["passed"] is False
    assert report["checks"]["eval"]["recommendation"] == "reject"


def test_ci_check_failed_eval_does_not_reuse_stale_eval_report_metrics(tmp_path: Path):
    repo = tmp_path
    docs = repo / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "---\nowner: platform\nverification_status: current\n---\n# Runbook\n",
        encoding="utf-8",
    )
    (repo / "AGENTS.md").write_text("# Agent Map\n\nSee [runbook](docs/runbook.md).\n", encoding="utf-8")
    (repo / "CODEX.md").write_text(
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
        encoding="utf-8",
    )
    run_dir = repo / ".sidecar" / "runs" / "run-1"
    _write_candidate_preview(
        run_dir,
        "# Policy\n\nSee [runbook](docs/runbook.md).\n\nYou must run tests before final answers.\n",
    )
    _write_candidate_json(run_dir)

    (run_dir / "eval-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 7,
                "suite_id": "all",
                "passed": True,
                "metrics": {},
                "trigger_score": 1.0,
                "held_out_score": 1.0,
                "governance_passed": True,
                "recommendation": "accept",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert main(["ci", "--repo", str(repo), "--candidate", "run-1", "--suite", "bogus"]) == 1

    report = json.loads((sidecar_dir(repo) / "ci" / "ci-report.json").read_text(encoding="utf-8"))
    assert report["checks"]["eval"] == {
        "passed": False,
        "candidate": "run-1",
        "suite_id": "bogus",
        "report_path": ".sidecar/runs/run-1/eval-report.json",
        "trigger_score": 0.0,
        "held_out_score": 0.0,
        "governance_passed": False,
        "recommendation": "reject",
    }
