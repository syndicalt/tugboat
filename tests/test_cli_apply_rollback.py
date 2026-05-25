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
    assert apply_plan["review_actor"] == "alice"
