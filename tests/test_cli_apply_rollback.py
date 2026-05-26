from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _candidate_run(repo: Path, *, risk_class: str = "instruction_clarification") -> Path:
    run_dir = repo / ".sidecar" / "runs" / "20260525T000000000000Z"
    run_dir.mkdir(parents=True)
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
        "base_file": "CODEX.md",
        "base_hash": _hash(repo / "CODEX.md"),
        "diff_hash": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "risk_class": risk_class,
        "rationale": "Keep rollback provenance visible.",
        "sources": [{"source_id": "audit:1", "trusted": True}],
    }
    (run_dir / "candidate.diff").write_text(diff, encoding="utf-8")
    (run_dir / "candidate.json").write_text(
        json.dumps(candidate, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (run_dir / "policy-gate.json").write_text(
        json.dumps({"allowed": True, "reasons": []}, indent=2) + "\n",
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
            connection.execute(
                """
                INSERT INTO decisions(
                  candidate_id, actor, policy, decision, reason, created_at,
                  applied_commit, rollback_ref, audit_event_sequence
                )
                VALUES (?, 'tugboat', 'deterministic_policy_gate', ?, 'seeded', ?, '', '', NULL)
                """,
                (1000 + index, decision, created_at),
            )
        for index in range(applied):
            connection.execute(
                """
                INSERT INTO decisions(
                  candidate_id, actor, policy, decision, reason, created_at,
                  applied_commit, rollback_ref, audit_event_sequence
                )
                VALUES (?, 'tugboat', 'apply_controller', 'applied', 'seeded', ?, 'abc', '[]', NULL)
                """,
                (2000 + index, created_at),
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
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'apply.planned'"
        ).fetchone()[0] == 1


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
                "branch_name": "tugboat/20260525t000000000000z/candidate-7/codex-md",
                "target_files": ["CODEX.md"],
                "applied_commit": commit_sha,
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
    assert rollback["metadata"]["commands"] == [
        ["git", "switch", "tugboat/20260525t000000000000z/candidate-7/codex-md"],
        ["git", "revert", "--no-edit", commit_sha],
    ]
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'rollback.planned'"
        ).fetchone()[0] == 1


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
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE event_type = 'rollback.applied'"
        ).fetchone()[0] == 1


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
        "rollback_command": apply_plan["rollback_command"],
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
    run_dir = _candidate_run(repo, risk_class="A")
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
    run_dir = _candidate_run(repo, risk_class="A")
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


def test_apply_class_c_requires_explicit_human_review(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="restricted_policy_change")

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
