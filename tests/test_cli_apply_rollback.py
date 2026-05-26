from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from tugboat.cli import main


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


def test_apply_rejects_stale_base_hash_before_writing_plan(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    _git(repo, "commit", "--allow-empty", "-m", "unrelated")
    (repo / "CODEX.md").write_text("# Rules\n\nUse tests.\nChanged base.\n", encoding="utf-8")
    _git(repo, "add", "CODEX.md")
    _git(repo, "commit", "-m", "change base")

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

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
    policy_path = repo / ".sidecar" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        f"""
version: 9
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
    }
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
    assert apply_plan["pr_metadata"] == {
        "base_branch": "main",
        "body": apply_plan["pr_metadata"]["body"],
        "branch_name": apply_plan["branch_name"],
        "draft": True,
        "title": "tugboat: apply candidate 7 for CODEX.md",
    }
    assert "Candidate: 7" in apply_plan["pr_metadata"]["body"]


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
