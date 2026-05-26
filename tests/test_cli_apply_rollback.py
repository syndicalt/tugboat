from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tugboat.cli as cli_module
from tugboat.cli import main
from tugboat.db import Store
from tugboat.paths import sidecar_dir
from tugboat.vcs import VcsAdapter, VcsStateError


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tugboat@example.test")
    _git(repo, "config", "user.name", "Tugboat Tests")
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_run(
    repo: Path,
    *,
    risk_class: str = "instruction_clarification",
    bounded_section: str | None = None,
    base_file: str = "CODEX.md",
    diff: str | None = None,
    pending_eval_definition_paths: tuple[str, ...] = (),
) -> Path:
    run_dir = repo / ".sidecar" / "runs" / "20260525T000000000000Z"
    run_dir.mkdir(parents=True)
    if diff is None:
        diff = (
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@ -1,3 +1,4 @@\n"
            " # Rules\n"
            " \n"
            " Use tests.\n"
            "+Record rollback notes.\n"
        )
    candidate = {
        "schema_version": 1,
        "audit_id": 1,
        "candidate_id": 7,
        "base_file": base_file,
        "base_hash": _hash(repo / base_file),
        "diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "risk_class": risk_class,
        "rationale": "Keep rollback provenance visible.",
        "sources": [{"source_id": "audit:1", "trusted": True}],
    }
    if pending_eval_definition_paths:
        candidate["pending_audit_eval_definition_paths"] = list(pending_eval_definition_paths)
    if bounded_section is not None:
        candidate["bounded_edit_metadata"] = [
            {
                "operator": "add",
                "file": base_file,
                "section": bounded_section,
                "changed_lines": 1,
                "normative_changes": 0,
            }
        ]
    (run_dir / "candidate.diff").write_text(diff, encoding="utf-8")
    (run_dir / "candidate.json").write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True, "reasons": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "eval-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 7,
                "suite_id": "all",
                "passed": True,
                "trigger_score": 0.80,
                "held_out_score": 0.90,
                "governance_passed": True,
                "recommendation": "accept",
                "metrics": {"governance_regressions": 0},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def _write_auto_apply_policy(repo: Path, *, version: int = 9) -> None:
    policy_path = repo / ".sidecar" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        f"""
version: {version}
auto_apply:
  enabled: true
  allowed_repositories:
    - {repo}
  minimum_burn_in_days: 30
  maximum_rejection_rate: 0.05
  maximum_rollback_rate: 0.01
""",
        encoding="utf-8",
    )


def _seed_auto_apply_history(
    repo: Path,
    *,
    days_ago: int = 31,
    reviewed: int = 20,
    rejected: int = 0,
    applied: int = 20,
    rollbacks: int = 0,
) -> None:
    created_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    db_path = repo / ".sidecar" / "db.sqlite"
    with Store.open(sidecar_dir(repo) / "db.sqlite"):
        pass
    with closing(sqlite3.connect(db_path)) as connection:
        for index in range(reviewed):
            decision = "rejected" if index < rejected else "needs_review"
            cursor = connection.execute(
                """
                INSERT INTO audit_events(event_type, payload_json, previous_hash, event_hash)
                VALUES ('decision.recorded', ?, '', ?)
                """,
                (
                    json.dumps({"candidate_id": 1000 + index, "seed": index}, sort_keys=True),
                    f"seeded-review-decision-{index}",
                ),
            )
            connection.execute(
                """
                INSERT INTO decisions(
                  candidate_id, actor, policy, decision, reason, created_at,
                  applied_commit, rollback_ref, audit_event_sequence
                )
                VALUES (?, 'tugboat', 'deterministic_policy_gate', ?, 'seeded', ?, '', '', ?)
                """,
                (1000 + index, decision, created_at, int(cursor.lastrowid)),
            )
        for index in range(applied):
            cursor = connection.execute(
                """
                INSERT INTO audit_events(event_type, payload_json, previous_hash, event_hash)
                VALUES ('decision.recorded', ?, '', ?)
                """,
                (
                    json.dumps({"candidate_id": 2000 + index, "seed": index}, sort_keys=True),
                    f"seeded-apply-decision-{index}",
                ),
            )
            connection.execute(
                """
                INSERT INTO decisions(
                  candidate_id, actor, policy, decision, reason, created_at,
                  applied_commit, rollback_ref, audit_event_sequence
                )
                VALUES (?, 'tugboat', 'apply_controller', 'applied', 'seeded', ?, 'abc', '[]', ?)
                """,
                (2000 + index, created_at, int(cursor.lastrowid)),
            )
        for index in range(rollbacks):
            connection.execute(
                """
                INSERT INTO audit_events(event_type, payload_json, previous_hash, event_hash)
                VALUES ('rollback.applied', ?, '', ?)
                """,
                (json.dumps({"seed": index}, sort_keys=True), f"rollback-{index}"),
            )
        connection.commit()


def test_apply_proposal_mode_writes_plan_without_mutating_instruction_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    run_dir = _candidate_run(repo)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 0

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    assert apply_plan["mode"] == "proposal"
    assert apply_plan["candidate_id"] == 7
    assert apply_plan["target_files"] == ["CODEX.md"]
    assert apply_plan["branch_name"] == "tugboat/20260525t000000000000z/candidate-7/codex-md"
    assert apply_plan["pre_hashes"] == {"CODEX.md": _hash(repo / "CODEX.md")}
    assert apply_plan["post_hashes"] == {}
    assert apply_plan["rollback_command"] == []
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = 'apply.planned'"
        ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["provenance_bundle"] == (
        ".sidecar/runs/20260525T000000000000Z/provenance-bundle.json"
    )


def test_apply_is_blocked_by_read_only_kill_switch_before_writing_plan(
    tmp_path: Path,
    capsys,
):
    repo = _init_repo(tmp_path)
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    run_dir = _candidate_run(repo)
    (repo / ".sidecar" / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    assert main(["apply", "--repo", str(repo), "--candidate", "latest"]) == 1

    assert "apply blocked: read-only kill switch is enabled" in capsys.readouterr().out
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()


def test_auto_apply_is_blocked_by_read_only_kill_switch_before_writing_plan(
    tmp_path: Path,
    capsys,
):
    repo = _init_repo(tmp_path)
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)
    (repo / ".sidecar" / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
                "--actor",
                "operator@example.com",
                "--burn-in-days",
                "30",
                "--rejection-rate",
                "0.02",
                "--rollback-rate",
                "0.001",
            ]
        )
        == 1
    )

    assert "auto-apply blocked: read-only kill switch is enabled" in capsys.readouterr().out
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()


def test_apply_rejects_dirty_target_before_writing_plan(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    (repo / "CODEX.md").write_text("# Rules\n\nlocal edit\n", encoding="utf-8")

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_unrelated_dirty_worktree_before_creating_branch(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    original_branch = _git(repo, "branch", "--show-current")
    (repo / "README.md").write_text("# local notes\n", encoding="utf-8")

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "branch"]) == 1

    assert _git(repo, "branch", "--show-current") == original_branch
    assert not (run_dir / "apply-plan.json").exists()
    assert "tugboat/20260525t000000000000z/candidate-7/codex-md" not in _git(repo, "branch")


def test_apply_rejects_diff_that_touches_file_outside_candidate_base_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "README.md").write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "add readme")
    original_codex = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_readme = (repo / "README.md").read_text(encoding="utf-8")
    original_branch = _git(repo, "branch", "--show-current")
    run_dir = _candidate_run(repo)
    mismatched_diff = (
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,3 +1,4 @@\n"
        " # Rules\n"
        " \n"
        " Use tests.\n"
        "+Unauthorized readme edit.\n"
    )
    (run_dir / "candidate.diff").write_text(mismatched_diff, encoding="utf-8")
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate["diff_hash"] = hashlib.sha256(mismatched_diff.encode("utf-8")).hexdigest()
    (run_dir / "candidate.json").write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "branch"]) == 1

    assert _git(repo, "branch", "--show-current") == original_branch
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original_codex
    assert (repo / "README.md").read_text(encoding="utf-8") == original_readme
    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_stale_base_hash_before_writing_plan(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    _git(repo, "commit", "--allow-empty", "-m", "unrelated")
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\nChanged base.\n", encoding="utf-8")
    _git(repo, "add", "CODEX.md")
    _git(repo, "commit", "-m", "change base")

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_policy_invalid_patch_without_mutation_or_branch_change(tmp_path: Path):
    repo = _init_repo(tmp_path)
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_branch = _git(repo, "branch", "--show-current")
    run_dir = _candidate_run(repo)
    conflicting_diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,3 +1,4 @@\n"
        " # Rules\n"
        " \n"
        " Use a different line.\n"
        "+Record rollback notes.\n"
    )
    (run_dir / "candidate.diff").write_text(conflicting_diff, encoding="utf-8")
    candidate = json.loads((run_dir / "candidate.json").read_text(encoding="utf-8"))
    candidate["diff_hash"] = hashlib.sha256(conflicting_diff.encode("utf-8")).hexdigest()
    (run_dir / "candidate.json").write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "branch"]) == 1

    assert _git(repo, "branch", "--show-current") == original_branch
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    db_path = repo / ".sidecar" / "db.sqlite"
    if db_path.exists():
        with closing(sqlite3.connect(db_path)) as connection:
            table_exists = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'audit_events'"
            ).fetchone()[0]
            if table_exists:
                assert connection.execute(
                    "SELECT COUNT(*) FROM audit_events WHERE event_type = 'apply.applied'"
                ).fetchone()[0] == 0


def test_apply_restores_original_branch_when_vcs_apply_fails_after_branch_creation(
    tmp_path: Path,
    monkeypatch,
):
    repo = _init_repo(tmp_path)
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_branch = _git(repo, "branch", "--show-current")
    run_dir = _candidate_run(repo)

    class FailingApplyAdapter(VcsAdapter):
        def apply_diff(self, diff_path: Path) -> None:
            raise VcsStateError("git apply failed: simulated conflict")

    monkeypatch.setattr(cli_module, "VcsAdapter", FailingApplyAdapter)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "branch"]) == 1

    assert _git(repo, "branch", "--show-current") == original_branch
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_prohibited_risk_class(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="secret_exposure")

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_sidecar_policy_self_apply_even_when_stored_gate_passed(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    policy_path = repo / ".sidecar" / "policy.yaml"
    policy_path.parent.mkdir(parents=True)
    original_policy = (
        "version: 1\n"
        "mode: proposal_only\n"
        "instruction_files:\n"
        "  - path: .sidecar/policy.yaml\n"
        "    kind: repo_policy\n"
        "    precedence: 90\n"
        "    protected: true\n"
        "auto_apply:\n"
        "  enabled: false\n"
    )
    policy_path.write_text(original_policy, encoding="utf-8")
    run_dir = repo / ".sidecar" / "runs" / "20260525T000000000001Z"
    run_dir.mkdir(parents=True)
    diff = (
        "--- a/.sidecar/policy.yaml\n"
        "+++ b/.sidecar/policy.yaml\n"
        "@@\n"
        "-  enabled: false\n"
        "+  enabled: true\n"
    )
    candidate = {
        "schema_version": 1,
        "audit_id": 1,
        "candidate_id": 8,
        "base_file": ".sidecar/policy.yaml",
        "base_hash": _hash(policy_path),
        "diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "risk_class": "A",
        "rationale": "Simulate a misclassified sidecar approval policy edit.",
        "sources": [{"source_id": "audit:policy", "trusted": True}],
    }
    (run_dir / "candidate.diff").write_text(diff, encoding="utf-8")
    (run_dir / "candidate.json").write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True, "reasons": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "eval-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 8,
                "suite_id": "all",
                "passed": True,
                "trigger_score": 0.80,
                "held_out_score": 0.90,
                "governance_passed": True,
                "recommendation": "accept",
                "metrics": {"governance_regressions": 0},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert policy_path.read_text(encoding="utf-8") == original_policy
    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_sidecar_audit_record_edit_even_when_stored_gate_passed(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    sidecar = repo / ".sidecar"
    sidecar.mkdir(parents=True)
    policy_path = sidecar / "policy.yaml"
    policy_path.write_text(
        "version: 1\n"
        "mode: proposal_only\n"
        "instruction_files:\n"
        "  - path: .sidecar/db.sqlite\n"
        "    kind: audit_record\n"
        "    precedence: 100\n"
        "    protected: true\n",
        encoding="utf-8",
    )
    audit_db = sidecar / "db.sqlite"
    original_audit_db = "sqlite audit history\n"
    audit_db.write_text(original_audit_db, encoding="utf-8")
    run_dir = sidecar / "runs" / "20260525T000000000002Z"
    run_dir.mkdir(parents=True)
    diff = (
        "--- a/.sidecar/db.sqlite\n"
        "+++ b/.sidecar/db.sqlite\n"
        "@@\n"
        "-sqlite audit history\n"
        "+rewritten audit history\n"
    )
    candidate = {
        "schema_version": 1,
        "audit_id": 1,
        "candidate_id": 9,
        "base_file": ".sidecar/db.sqlite",
        "base_hash": _hash(audit_db),
        "diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "risk_class": "A",
        "rationale": "Simulate a misclassified sidecar audit record edit.",
        "sources": [{"source_id": "audit:history", "trusted": True}],
    }
    (run_dir / "candidate.diff").write_text(diff, encoding="utf-8")
    (run_dir / "candidate.json").write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"schema_version": 1, "allowed": True, "reasons": []}, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "eval-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 9,
                "suite_id": "all",
                "passed": True,
                "trigger_score": 0.80,
                "held_out_score": 0.90,
                "governance_passed": True,
                "recommendation": "accept",
                "metrics": {"governance_regressions": 0},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert audit_db.read_text(encoding="utf-8") == original_audit_db
    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_passing_eval_without_held_out_improvement(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    (run_dir / "eval-report.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_id": 7,
                "suite_id": "all",
                "passed": True,
                "metrics": {
                    "governance_regressions": 0,
                    "trigger_score": 0.90,
                    "held_out_score": 0.80,
                    "recommendation": "reject",
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_eval_report_for_different_candidate(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report["candidate_id"] = 999
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "provenance-bundle.json").exists()


def test_apply_rejects_malformed_eval_report_artifact(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report.pop("schema_version")
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "provenance-bundle.json").exists()


def test_apply_rejects_equal_trigger_and_held_out_scores(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report["trigger_score"] = 0.90
    eval_report["held_out_score"] = 0.90
    eval_report["governance_passed"] = True
    eval_report["recommendation"] = "accept"
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_eval_report_without_validation_scores(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report.pop("trigger_score")
    eval_report.pop("held_out_score")
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()


def test_rollback_writes_revert_plan_from_apply_decision(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    commit_sha = _git(repo, "rev-parse", "HEAD")
    (run_dir / "apply-plan.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "commit",
                "candidate_id": 7,
                "run_id": "20260525T000000000000Z",
                "branch_name": "tugboat/20260525t000000000000z/candidate-7/codex-md",
                "commit_message": "Apply Tugboat candidate 7",
                "target_files": ["CODEX.md"],
                "pre_hashes": {"CODEX.md": _hash(repo / "CODEX.md")},
                "post_hashes": {"CODEX.md": _hash(repo / "CODEX.md")},
                "applied_commit": commit_sha,
                "rollback_command": [
                    ["git", "switch", "tugboat/20260525t000000000000z/candidate-7/codex-md"],
                    ["git", "revert", "--no-edit", commit_sha],
                ],
                "provenance_bundle": ".sidecar/runs/20260525T000000000000Z/provenance-bundle.json",
                "pr_metadata": {},
                "review_actor": "tugboat",
                "auto_apply": False,
                "explicit_human_review": False,
                "review_required_reasons": [],
                "decision_rationale": "manual test fixture",
                "decision_id": "20260525T000000000000Z",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["rollback", "--repo", str(repo), "--decision", "latest"]) == 0

    rollback = json.loads((run_dir / "rollback-plan.json").read_text(encoding="utf-8"))
    assert rollback["decision_id"] == "20260525T000000000000Z"
    assert rollback["metadata"]["commit_sha"] == commit_sha
    assert rollback["source_artifacts"]["apply_plan"] == {
        "path": ".sidecar/runs/20260525T000000000000Z/apply-plan.json",
        "sha256": _hash(run_dir / "apply-plan.json"),
    }
    assert rollback["metadata"]["commands"] == [
        ["git", "switch", "tugboat/20260525t000000000000z/candidate-7/codex-md"],
        ["git", "revert", "--no-edit", commit_sha],
    ]
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = 'rollback.planned'"
        ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["rollback_plan"] == ".sidecar/runs/20260525T000000000000Z/rollback-plan.json"


def test_rollback_rejects_malformed_apply_plan_before_writing_plan(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    apply_plan.pop("schema_version")
    (run_dir / "apply-plan.json").write_text(
        json.dumps(apply_plan, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["rollback", "--repo", str(repo), "--decision", "latest"]) == 1

    assert not (run_dir / "rollback-plan.json").exists()


def test_rollback_execute_reverts_applied_commit_and_audits_change(tmp_path: Path):
    repo = _init_repo(tmp_path)
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    run_dir = _candidate_run(repo)
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    assert "Record rollback notes." in (repo / "CODEX.md").read_text(encoding="utf-8")

    assert main(["rollback", "--repo", str(repo), "--decision", "latest", "--execute"]) == 0

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    rollback = json.loads((run_dir / "rollback-plan.json").read_text(encoding="utf-8"))
    assert rollback["executed"] is True
    assert rollback["revert_commit"] == _git(repo, "rev-parse", "HEAD")
    assert rollback["source_artifacts"]["provenance_bundle"] == {
        "path": ".sidecar/runs/20260525T000000000000Z/provenance-bundle.json",
        "sha256": _hash(run_dir / "provenance-bundle.json"),
    }
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = 'rollback.applied'"
        ).fetchone()
        rollback_row = connection.execute(
            """
            SELECT decision_id, candidate_id, reason, revert_commit,
                   post_rollback_eval_result_json, rollback_plan, executed
            FROM rollbacks
            """
        ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload["rollback_plan"] == ".sidecar/runs/20260525T000000000000Z/rollback-plan.json"
    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    assert payload["pre_hashes"] == apply_plan["pre_hashes"]
    assert payload["post_rollback_hashes"] == {"CODEX.md": _hash(repo / "CODEX.md")}
    assert payload["restored_pre_hashes"] is True
    assert payload["source_artifacts"]["apply_plan"]["sha256"] == _hash(run_dir / "apply-plan.json")
    assert payload["source_artifacts"]["provenance_bundle"]["sha256"] == _hash(
        run_dir / "provenance-bundle.json"
    )
    assert rollback["pre_hashes"] == apply_plan["pre_hashes"]
    assert rollback["post_rollback_hashes"] == {"CODEX.md": _hash(repo / "CODEX.md")}
    assert rollback["restored_pre_hashes"] is True
    assert rollback_row == (
        "20260525T000000000000Z",
        7,
        "rollback decision 20260525T000000000000Z",
        rollback["revert_commit"],
        json.dumps(
            {
                "executed": True,
                "restored_pre_hashes": True,
                "target_files": ["CODEX.md"],
            },
            sort_keys=True,
        ),
        ".sidecar/runs/20260525T000000000000Z/rollback-plan.json",
        1,
    )


def test_rollback_execute_rejects_dirty_worktree_before_revert(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    applied_text = (repo / "CODEX.md").read_text(encoding="utf-8")
    (repo / "scratch.txt").write_text("local work in progress\n", encoding="utf-8")

    assert main(["rollback", "--repo", str(repo), "--decision", "latest", "--execute"]) == 1

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == applied_text
    assert (repo / "scratch.txt").read_text(encoding="utf-8") == "local work in progress\n"
    assert not (run_dir / "rollback-plan.json").exists()


def test_apply_commit_mode_creates_branch_commit_and_rollback_command(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    assert _git(repo, "branch", "--show-current") == apply_plan["branch_name"]
    assert apply_plan["mode"] == "commit"
    assert apply_plan["applied_commit"] == _git(repo, "rev-parse", "HEAD")
    assert apply_plan["post_hashes"]["CODEX.md"] == _hash(repo / "CODEX.md")
    assert "Record rollback notes." in (repo / "CODEX.md").read_text(encoding="utf-8")
    assert apply_plan["rollback_command"] == [
        ["git", "switch", apply_plan["branch_name"]],
        ["git", "revert", "--no-edit", apply_plan["applied_commit"]],
    ]


def test_apply_commit_mode_records_applied_audit_proof(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    provenance_bundle = json.loads((run_dir / "provenance-bundle.json").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE event_type = 'apply.applied'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    payload = json.loads(row[0])
    assert payload == {
        "candidate_id": 7,
        "mode": "commit",
        "run_id": run_dir.name,
        "target_files": ["CODEX.md"],
        "applied_commit": apply_plan["applied_commit"],
        "eval_report": {
            "path": ".sidecar/runs/20260525T000000000000Z/eval-report.json",
            "suite_id": "all",
            "passed": True,
        },
        "policy_gate": {
            "path": ".sidecar/runs/20260525T000000000000Z/policy-gate.json",
            "allowed": True,
            "reasons": [],
        },
        "pre_hashes": apply_plan["pre_hashes"],
        "post_hashes": apply_plan["post_hashes"],
        "provenance_bundle": ".sidecar/runs/20260525T000000000000Z/provenance-bundle.json",
        "rollback_command": apply_plan["rollback_command"],
    }
    assert apply_plan["provenance_bundle"] == (
        ".sidecar/runs/20260525T000000000000Z/provenance-bundle.json"
    )
    assert provenance_bundle == {
        "schema_version": 1,
        "run_id": run_dir.name,
        "candidate_id": 7,
        "mode": "commit",
        "target_files": ["CODEX.md"],
        "applied_commit": apply_plan["applied_commit"],
        "rollback_command": apply_plan["rollback_command"],
        "pre_hashes": apply_plan["pre_hashes"],
        "post_hashes": apply_plan["post_hashes"],
        "source_artifacts": {
            "apply_plan": {
                "path": ".sidecar/runs/20260525T000000000000Z/apply-plan.json",
                "sha256": _hash(run_dir / "apply-plan.json"),
            },
            "candidate_diff": {
                "path": ".sidecar/runs/20260525T000000000000Z/candidate.diff",
                "sha256": _hash(run_dir / "candidate.diff"),
            },
            "candidate_metadata": {
                "path": ".sidecar/runs/20260525T000000000000Z/candidate.json",
                "sha256": _hash(run_dir / "candidate.json"),
            },
            "eval_report": {
                "path": ".sidecar/runs/20260525T000000000000Z/eval-report.json",
                "sha256": _hash(run_dir / "eval-report.json"),
            },
            "policy_gate": {
                "path": ".sidecar/runs/20260525T000000000000Z/policy-gate.json",
                "sha256": _hash(run_dir / "policy-gate.json"),
            },
        },
    }


def test_apply_commit_mode_records_apply_decision_row(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            """
            SELECT actor, policy, decision, reason, applied_commit, rollback_ref, audit_event_sequence
            FROM decisions
            WHERE candidate_id = 7 AND policy = 'apply_controller'
            """
        ).fetchone()
        event_type = connection.execute(
            "SELECT event_type FROM audit_events WHERE sequence = ?",
            (row[6],),
        ).fetchone()[0]

    assert row == (
        "tugboat",
        "apply_controller",
        "applied",
        "policy gate and eval report passed",
        apply_plan["applied_commit"],
        json.dumps(apply_plan["rollback_command"], sort_keys=True),
        row[6],
    )
    assert row[6] is not None
    assert event_type == "decision.recorded"


def test_auto_apply_commit_blocks_without_enabled_policy_or_confirmation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A")
    original = (repo / "CODEX.md").read_text(encoding="utf-8")

    assert (
        main(
            [
                "apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--mode",
                "commit",
                "--auto-apply",
                "--burn-in-days",
                "30",
            ]
        )
        == 1
    )

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()


def test_auto_apply_commit_requires_policy_confirmation_and_records_reversible_audit(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--mode",
                "commit",
                "--auto-apply",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
                "--review-actor",
                "operator@example.com",
                "--burn-in-days",
                "30",
                "--rejection-rate",
                "0.02",
                "--rollback-rate",
                "0.001",
            ]
        )
        == 0
    )

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    approval = json.loads((run_dir / "auto-apply-approval.json").read_text(encoding="utf-8"))
    assert apply_plan["mode"] == "commit"
    assert apply_plan["auto_apply"] is True
    assert apply_plan["applied_commit"] == _git(repo, "rev-parse", "HEAD")
    assert approval == {
        "actor": "operator@example.com",
        "candidate_id": "7",
        "change_class": "A",
        "policy_version": 9,
        "repository": str(repo.resolve()),
        "rollback_command": [
            "tugboat",
            "rollback",
            "--repo",
            str(repo.resolve()),
            "--decision",
            run_dir.name,
            "--execute",
        ],
        "vcs": {
            "branch_name": apply_plan["branch_name"],
            "commit_sha": apply_plan["applied_commit"],
            "mode": "commit",
        },
        "readiness_metrics": {
            "applied_count": 20,
            "burn_in_days": approval["readiness_metrics"]["burn_in_days"],
            "rejected_count": 0,
            "rejection_rate": 0.0,
            "reviewed_count": 20,
            "rollback_count": 0,
            "rollback_rate": 0.0,
            "source_audit_range": approval["readiness_metrics"]["source_audit_range"],
        },
    }
    assert approval["readiness_metrics"]["burn_in_days"] >= 30
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        event = connection.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE event_type = 'auto_apply.applied'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
        decision = connection.execute(
            """
            SELECT actor, policy, decision, applied_commit, rollback_ref
            FROM decisions
            WHERE candidate_id = 7 AND policy = 'auto_apply_controller'
            """
        ).fetchone()

    assert event is not None
    event_payload = json.loads(event[0])
    assert event_payload["approval_bundle"] == approval
    assert event_payload["reasons"] == []
    assert decision == (
        "operator@example.com",
        "auto_apply_controller",
        "applied",
        apply_plan["applied_commit"],
        json.dumps(approval["rollback_command"], sort_keys=True),
    )


def test_auto_apply_rejects_class_a_candidate_without_allowed_change_category(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="General")
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--mode",
                "commit",
                "--auto-apply",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
                "--review-actor",
                "operator@example.com",
                "--burn-in-days",
                "30",
                "--rejection-rate",
                "0.02",
                "--rollback-rate",
                "0.001",
            ]
        )
        == 1
    )

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        event = connection.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE event_type = 'auto_apply.decided'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()

    assert event is not None
    event_payload = json.loads(event[0])
    assert event_payload["eligible"] is False
    assert event_payload["reasons"] == ["auto_apply_change_type_not_allowed"]


def test_auto_apply_rejects_class_a_candidate_touching_forbidden_policy_domain(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A")
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    candidate_path = run_dir / "candidate.json"
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidate["bounded_edit_metadata"] = [
        {
            "operator": "add",
            "file": "CODEX.md",
            "section": "Provider Routing",
            "changed_lines": 1,
            "normative_changes": 0,
        }
    ]
    candidate_path.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--mode",
                "commit",
                "--auto-apply",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
                "--review-actor",
                "operator@example.com",
                "--burn-in-days",
                "30",
                "--rejection-rate",
                "0.02",
                "--rollback-rate",
                "0.001",
            ]
        )
        == 1
    )

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        event = connection.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE event_type = 'auto_apply.decided'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()

    assert event is not None
    event_payload = json.loads(event[0])
    assert event_payload["eligible"] is False
    assert "forbidden_category:provider_routing" in event_payload["reasons"]


def test_auto_apply_confirmation_requires_matching_policy_version(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A")
    (repo / ".sidecar" / "policy.yaml").write_text(
        f"""
version: 9
auto_apply:
  enabled: true
  allowed_repositories:
    - {repo}
""",
        encoding="utf-8",
    )

    assert (
        main(
            [
                "apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--mode",
                "commit",
                "--auto-apply",
                "--confirm-auto-apply",
                "--review-actor",
                "operator@example.com",
                "--burn-in-days",
                "30",
                "--rejection-rate",
                "0.02",
                "--rollback-rate",
                "0.001",
            ]
        )
        == 1
    )

    assert not (run_dir / "apply-plan.json").exists()


def test_auto_apply_command_delegates_to_confirmed_commit_lane(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    _write_auto_apply_policy(repo, version=3)
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "3",
                "--actor",
                "operator@example.com",
                "--burn-in-days",
                "30",
                "--rejection-rate",
                "0.02",
                "--rollback-rate",
                "0.001",
            ]
        )
        == 0
    )

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    approval = json.loads((run_dir / "auto-apply-approval.json").read_text(encoding="utf-8"))
    assert apply_plan["mode"] == "commit"
    assert apply_plan["auto_apply"] is True
    assert approval["actor"] == "operator@example.com"


def test_auto_apply_uses_ledger_burn_in_instead_of_favorable_cli_value(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A")
    _write_auto_apply_policy(repo, version=4)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "4",
                "--actor",
                "operator@example.com",
                "--burn-in-days",
                "999",
                "--rejection-rate",
                "0",
                "--rollback-rate",
                "0",
            ]
        )
        == 1
    )

    assert not (run_dir / "apply-plan.json").exists()


def test_auto_apply_uses_ledger_rejection_and_rollback_rates_instead_of_cli_values(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A")
    _write_auto_apply_policy(repo, version=5)
    _seed_auto_apply_history(repo, reviewed=20, rejected=2, applied=20, rollbacks=1)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "5",
                "--actor",
                "operator@example.com",
                "--burn-in-days",
                "999",
                "--rejection-rate",
                "0",
                "--rollback-rate",
                "0",
            ]
        )
        == 1
    )

    assert not (run_dir / "apply-plan.json").exists()


def test_apply_branch_mode_creates_branch_and_applies_patch_without_commit(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "branch"]) == 0

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    assert _git(repo, "branch", "--show-current") == apply_plan["branch_name"]
    assert apply_plan["mode"] == "branch"
    assert apply_plan["applied_commit"] == ""
    assert "Record rollback notes." in (repo / "CODEX.md").read_text(encoding="utf-8")


def test_apply_pr_mode_writes_pr_metadata_bundle(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "pr"]) == 0

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    assert apply_plan["mode"] == "pr"
    assert _git(repo, "branch", "--show-current") == apply_plan["branch_name"]
    assert apply_plan["applied_commit"] == _git(repo, "rev-parse", "HEAD")
    assert apply_plan["rollback_command"] == [
        ["git", "switch", apply_plan["branch_name"]],
        ["git", "revert", "--no-edit", apply_plan["applied_commit"]],
    ]
    assert apply_plan["pr_metadata"] == {
        "base_branch": "main",
        "body": apply_plan["pr_metadata"]["body"],
        "branch_name": apply_plan["branch_name"],
        "draft": True,
        "title": "tugboat: apply candidate 7 for CODEX.md",
    }
    assert "Candidate: 7" in apply_plan["pr_metadata"]["body"]


def test_apply_pr_mode_cleans_generated_branch_when_commit_fails(
    tmp_path: Path,
    monkeypatch,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    original_branch = _git(repo, "branch", "--show-current")
    original_text = (repo / "CODEX.md").read_text(encoding="utf-8")

    def fail_commit(self, files: tuple[str, ...], message: str) -> str:
        raise VcsStateError("git commit failed: simulated hook rejection")

    monkeypatch.setattr(cli_module.VcsAdapter, "commit_files", fail_commit)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "pr"]) == 1

    assert _git(repo, "branch", "--show-current") == original_branch
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original_text
    assert "tugboat/20260525t000000000000z/candidate-7/codex-md" not in _git(repo, "branch")
    assert not (run_dir / "apply-plan.json").exists()


@pytest.mark.parametrize("risk_class", ["restricted_policy_change", "network_access"])
def test_apply_class_c_requires_explicit_human_review(tmp_path: Path, risk_class: str):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class=risk_class)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1
    assert not (run_dir / "apply-plan.json").exists()

    assert (
        main(
            [
                "apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--mode",
                "proposal",
                "--human-review",
                "--review-actor",
                "alice",
            ]
        )
        == 0
    )
    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    assert apply_plan["explicit_human_review"] is True
    assert apply_plan["review_required_reasons"] == [
        "class_c_explicit_human_review_required"
    ]
    assert apply_plan["review_actor"] == "alice"
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        review_action = connection.execute(
            """
            SELECT candidate_id, actor, action, reason, audit_event_sequence
            FROM review_actions
            """
        ).fetchone()
        event_type = connection.execute(
            "SELECT event_type FROM audit_events WHERE sequence = ?",
            (review_action[4],),
        ).fetchone()[0]

    assert review_action == (
        7,
        "alice",
        "approved",
        "class_c_explicit_human_review_required",
        review_action[4],
    )
    assert review_action[4] is not None
    assert event_type == "review_action.recorded"


def test_apply_rejects_candidate_editing_pending_eval_definition_after_artifact_reload(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    eval_file = repo / "tests" / "fixtures" / "evals" / "regression.json"
    eval_file.parent.mkdir(parents=True)
    eval_file.write_text('{"suite": "regression"}\n', encoding="utf-8")
    _git(repo, "add", "tests/fixtures/evals/regression.json")
    _git(repo, "commit", "-m", "add eval fixture")
    policy_path = repo / ".sidecar" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        """
version: 1
instruction_files:
  - path: tests/fixtures/evals/regression.json
    kind: eval_definition
    precedence: 100
    protected: false
""".lstrip(),
        encoding="utf-8",
    )
    run_dir = _candidate_run(
        repo,
        base_file="tests/fixtures/evals/regression.json",
        diff=(
            "--- a/tests/fixtures/evals/regression.json\n"
            "+++ b/tests/fixtures/evals/regression.json\n"
            "@@\n"
            '-{"suite": "regression"}\n'
            '+{"suite": "easier-regression"}\n'
        ),
        pending_eval_definition_paths=("tests/fixtures/evals/*.json",),
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()
    assert eval_file.read_text(encoding="utf-8") == '{"suite": "regression"}\n'
