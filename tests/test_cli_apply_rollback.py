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
from tugboat.daemon.queue import DaemonQueue, JobState
from tugboat.db import Store
from tugboat.paths import sidecar_dir
from tugboat.vcs import VcsAdapter, VcsStateError

SECRET_VALUE = "sk-abcdefghijklmnopqrstuvwx"


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
    (repo / ".sidecar").mkdir()
    (repo / ".sidecar" / ".gitignore").write_text(
        "*\n!.gitignore\n!policy.yaml\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


def _write_pr_policy(
    repo: Path,
    *,
    provider: str = "github_cli",
    remote: str = "origin",
    base_branch: str = "main",
    draft: bool = True,
) -> None:
    (repo / ".sidecar" / "policy.yaml").write_text(
        f"""
version: 1
vcs:
  pull_request:
    enabled: true
    provider: {provider}
    remote: {remote}
    base_branch: {base_branch}
    draft: {str(draft).lower()}
""".lstrip(),
        encoding="utf-8",
    )
    _git(repo, "add", ".sidecar/policy.yaml")
    _git(repo, "commit", "-m", "configure pull requests")


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
    recorded_provenance: bool = True,
) -> Path:
    sidecar = repo / ".sidecar"
    sidecar.mkdir(exist_ok=True)
    (sidecar / ".gitignore").write_text("*\n!.gitignore\n!policy.yaml\n", encoding="utf-8")
    run_dir = repo / ".sidecar" / "runs" / "20260525T000000000000Z"
    run_dir.mkdir(parents=True)
    if diff is None:
        if base_file == "CODEX.md" and bounded_section not in {None, "Rules"}:
            section_text = (
                "# Rules\n\n"
                "Use tests.\n\n"
                f"# {bounded_section}\n\n"
                f"Keep {bounded_section.lower()} guidance.\n"
            )
            (repo / "CODEX.md").write_text(section_text, encoding="utf-8")
            _git(repo, "add", "CODEX.md")
            _git(repo, "commit", "-m", "fixture section")
            diff = (
                "--- a/CODEX.md\n"
                "+++ b/CODEX.md\n"
                "@@ -5,3 +5,4 @@\n"
                f" # {bounded_section}\n"
                " \n"
                f" Keep {bounded_section.lower()} guidance.\n"
                "+Record rollback notes.\n"
            )
        else:
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
                "metrics": {
                    "governance_regressions": 0,
                    "incident_replay_cases": 1,
                    "instruction_token_delta": 0,
                },
                "validation_splits": {
                    "trigger": ["incident_replay:regression"],
                    "held_out": ["held-out:no-regression"],
                    "governance": ["governance:policy"],
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if recorded_provenance:
        _seed_apply_candidate_provenance(repo, run_dir, candidate, diff)
    return run_dir


def _seed_apply_candidate_provenance(
    repo: Path,
    run_dir: Path,
    candidate: dict[str, object],
    diff: str,
) -> None:
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        audit_event = store.append_audit_event(
            "audit.recorded",
            {"audit_id": int(candidate["audit_id"]), "run_id": run_dir.name},
        )
        store.connection.execute(
            """
            INSERT INTO audits(
              id, run_id, failure_class, severity, confidence, evidence_json,
              instruction_refs_json, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(candidate["audit_id"]),
                run_dir.name,
                "instruction_conflict",
                "high",
                0.9,
                json.dumps(["audit:1"], sort_keys=True),
                json.dumps(["CODEX.md#rules"], sort_keys=True),
                audit_event.sequence,
            ),
        )
        candidate_event = store.append_audit_event(
            "candidate.recorded",
            {
                "audit_id": int(candidate["audit_id"]),
                "candidate_id": int(candidate["candidate_id"]),
            },
        )
        store.connection.execute(
            """
            INSERT INTO candidates(
              id, audit_id, base_file, base_hash, diff_hash, diff_path, risk_class,
              rationale, state, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(candidate["candidate_id"]),
                int(candidate["audit_id"]),
                str(candidate["base_file"]),
                str(candidate["base_hash"]),
                hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                str(run_dir / "candidate.diff"),
                str(candidate["risk_class"]),
                str(candidate["rationale"]),
                "needs_review",
                candidate_event.sequence,
            ),
        )
        eval_event = store.append_audit_event(
            "eval.recorded",
            {"eval_id": 1, "candidate_id": int(candidate["candidate_id"])},
        )
        store.connection.execute(
            """
            INSERT INTO evals(
              id, candidate_id, suite_id, report_path, passed, metrics_json,
              audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                int(candidate["candidate_id"]),
                "all",
                str(run_dir / "eval-report.json"),
                1,
                json.dumps({"governance_regressions": 0}, sort_keys=True),
                eval_event.sequence,
            ),
        )
        decision_event = store.append_audit_event(
            "decision.recorded",
            {"decision_id": 1, "candidate_id": int(candidate["candidate_id"])},
        )
        store.connection.execute(
            """
            INSERT INTO decisions(
              id, candidate_id, actor, policy, decision, reason, created_at,
              applied_commit, rollback_ref, audit_event_sequence
            )
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
            """,
            (
                1,
                int(candidate["candidate_id"]),
                "tugboat",
                "deterministic_policy_gate",
                "needs_review",
                "",
                "",
                "",
                decision_event.sequence,
            ),
        )
        store.connection.commit()


def _seed_daemon_waiting_review_job(repo: Path, run_dir: Path, *, candidate_id: int = 7) -> int:
    payload = {"candidate_id": str(candidate_id), "run_id": run_dir.name, "suite": "all"}
    with DaemonQueue.open_sidecar(repo) as queue:
        job = queue.enqueue(kind="eval", payload=payload)
        queue.transition(job.id, JobState.INSPECTING)
        queue.transition(job.id, JobState.RUNNING)
        queue.transition(job.id, JobState.EVALUATING)
        queue.transition(job.id, JobState.WAITING_REVIEW)
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_daemon_job(
            job_id=str(job.id),
            repo_path=repo,
            state=JobState.WAITING_REVIEW.value,
            payload=payload,
        )
    return job.id


def _write_auto_apply_policy(
    repo: Path,
    *,
    version: int = 9,
    allowed_risk_classes: tuple[str, ...] = ("A",),
    max_instruction_token_delta: int = 50,
) -> None:
    policy_path = repo / ".sidecar" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    allowed_risk_classes_yaml = "\n".join(
        f"    - {risk_class}" for risk_class in allowed_risk_classes
    )
    policy_path.write_text(
        f"""
version: {version}
auto_apply:
  enabled: true
  allowed_risk_classes:
{allowed_risk_classes_yaml}
  allowed_repositories:
    - {repo}
  minimum_burn_in_days: 14
  maximum_rejection_rate: 0.10
  maximum_rollback_rate: 0.02
  max_instruction_token_delta: {max_instruction_token_delta}
""",
        encoding="utf-8",
    )


def _seed_auto_apply_history(
    repo: Path,
    *,
    days_ago: int = 15,
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


def _auto_apply_decision_payloads(repo: Path) -> list[dict[str, object]]:
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        rows = connection.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE event_type = 'auto_apply.decided'
            ORDER BY sequence
            """
        ).fetchall()
    return [json.loads(row[0]) for row in rows]


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


def test_apply_rejects_artifact_only_candidate_without_recorded_provenance(
    tmp_path: Path,
    capsys,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, recorded_provenance=False)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    output = capsys.readouterr().out
    assert "apply blocked: candidate provenance is not recorded" in output
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "provenance-bundle.json").exists()


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
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
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


def test_apply_rejects_candidate_diff_mutated_after_eval(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_branch = _git(repo, "branch", "--show-current")
    run_dir = _candidate_run(repo)
    mutated_diff = (
        "--- a/CODEX.md\n"
        "+++ b/CODEX.md\n"
        "@@ -1,3 +1,4 @@\n"
        " # Rules\n"
        " \n"
        " Use tests.\n"
        "+Post-eval mutated instruction.\n"
    )
    (run_dir / "candidate.diff").write_text(mutated_diff, encoding="utf-8")

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "branch"]) == 1

    assert _git(repo, "branch", "--show-current") == original_branch
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    assert "tugboat/20260525t000000000000z/candidate-7/codex-md" not in _git(repo, "branch")


def test_apply_restores_original_branch_when_vcs_apply_fails_after_branch_creation(
    tmp_path: Path,
    monkeypatch,
):
    repo = _init_repo(tmp_path)
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_branch = _git(repo, "branch", "--show-current")
    run_dir = _candidate_run(repo)

    class FailingApplyAdapter(VcsAdapter):
        def apply_diff(self, diff_path: Path, *, allowed_paths: tuple[str, ...]) -> None:
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


def test_apply_rejects_candidate_matching_prior_rejected_edit_memory(
    tmp_path: Path,
    capsys,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, bounded_section="Repeated Direction")
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    fingerprint = hashlib.sha256(b"add\nCODEX.md\nRepeated Direction").hexdigest()
    with Store.open(sidecar_dir(repo) / "db.sqlite") as store:
        store.record_optimizer_memory(
            repo_path=str(repo.resolve()),
            memory_type="rejected_edit",
            key=fingerprint,
            payload={
                "future_proposal_suppression_signal": "suppress_matching_bounded_edit_fingerprint",
                "semantic_fingerprint": fingerprint,
                "rejection_reason": "held_out_not_improved",
                "source_refs": ["audit:1"],
            },
        )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    output = capsys.readouterr().out
    assert "apply blocked: policy gate rejected candidate: suppressed_by_rejected_edit_memory" in output
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_sidecar_policy_self_apply_even_when_stored_gate_passed(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    policy_path = repo / ".sidecar" / "policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
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
    sidecar.mkdir(parents=True, exist_ok=True)
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


def test_apply_rejects_eval_report_with_regression_degradation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report["metrics"] = {
        "baseline_regression_score": 0.05,
        "governance_regressions": 0,
        "regression_score": 0.20,
        "regression_tolerance": 0.05,
    }
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_eval_report_without_validation_split_provenance(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report.pop("validation_splits")
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "proposal"]) == 1

    assert not (run_dir / "apply-plan.json").exists()


def test_apply_rejects_overlapping_trigger_and_held_out_validation_splits(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report["validation_splits"] = {
        "trigger": ["case:shared"],
        "held_out": ["case:shared"],
    }
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
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    commit_sha = apply_plan["applied_commit"]

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


def test_rollback_rejects_apply_plan_that_no_longer_matches_provenance(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    apply_plan["review_actor"] = "tampered-reviewer"
    (run_dir / "apply-plan.json").write_text(
        json.dumps(apply_plan, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["rollback", "--repo", str(repo), "--decision", "latest"]) == 1

    assert not (run_dir / "rollback-plan.json").exists()


def test_rollback_rejects_missing_apply_plan_provenance_bundle(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    apply_plan["provenance_bundle"] = ".sidecar/runs/20260525T000000000000Z/missing.json"
    (run_dir / "apply-plan.json").write_text(
        json.dumps(apply_plan, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["rollback", "--repo", str(repo), "--decision", "latest"]) == 1

    assert not (run_dir / "rollback-plan.json").exists()


def test_rollback_rejects_provenance_with_wrong_apply_plan_ref(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    provenance_bundle = json.loads((run_dir / "provenance-bundle.json").read_text(encoding="utf-8"))
    provenance_bundle["source_artifacts"]["apply_plan"]["path"] = (
        ".sidecar/runs/20260525T000000000000Z/other-apply-plan.json"
    )
    (run_dir / "provenance-bundle.json").write_text(
        json.dumps(provenance_bundle, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert main(["rollback", "--repo", str(repo), "--decision", "latest"]) == 1

    assert not (run_dir / "rollback-plan.json").exists()


def test_apply_rejects_secret_in_final_apply_plan_before_writing_authority_artifacts(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)

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
                "--review-actor",
                f"reviewer-{SECRET_VALUE}",
            ]
        )
        == 1
    )

    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "provenance-bundle.json").exists()


def test_apply_branch_mode_rejects_secret_metadata_before_vcs_mutation(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_branch = _git(repo, "branch", "--show-current")
    run_dir = _candidate_run(repo)

    assert (
        main(
            [
                "apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--mode",
                "branch",
                "--review-actor",
                f"reviewer-{SECRET_VALUE}",
            ]
        )
        == 1
    )

    assert _git(repo, "branch", "--show-current") == original_branch
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert _git(repo, "status", "--porcelain=v1", "--untracked-files=all") == ""
    assert "tugboat/20260525t000000000000z/candidate-7/codex-md" not in _git(repo, "branch")
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "provenance-bundle.json").exists()


def test_rollback_rejects_secret_in_final_rollback_plan_before_writing_artifact(
    tmp_path: Path,
):
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
                "branch_name": f"tugboat/{SECRET_VALUE}",
                "commit_message": "Apply Tugboat candidate 7",
                "target_files": ["CODEX.md"],
                "pre_hashes": {"CODEX.md": _hash(repo / "CODEX.md")},
                "post_hashes": {"CODEX.md": _hash(repo / "CODEX.md")},
                "applied_commit": commit_sha,
                "rollback_command": [
                    ["git", "switch", f"tugboat/{SECRET_VALUE}"],
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
    assert main(["inspect-decision", "--repo", str(repo), "--decision", "latest"]) == 0
    decision_trace = json.loads((run_dir / "decision-trace.json").read_text(encoding="utf-8"))
    assert decision_trace["decision"]["decision"] == "applied"
    assert decision_trace["decision"]["applied_commit"] == apply_plan["applied_commit"]
    assert decision_trace["decision"]["rollback_ref"] == json.dumps(
        apply_plan["rollback_command"],
        sort_keys=True,
    )
    assert decision_trace["artifacts"]["apply_plan"] == (
        ".sidecar/runs/20260525T000000000000Z/apply-plan.json"
    )
    assert decision_trace["artifacts"]["provenance_bundle"] == (
        ".sidecar/runs/20260525T000000000000Z/provenance-bundle.json"
    )
    assert decision_trace["artifacts"]["rollback_plan"] == (
        ".sidecar/runs/20260525T000000000000Z/rollback-plan.json"
    )
    assert decision_trace["rollbacks"][0]["executed"] is True
    assert decision_trace["rollbacks"][0]["revert_commit"] == rollback["revert_commit"]
    assert decision_trace["rollbacks"][0]["post_rollback_eval_result"] == {
        "executed": True,
        "restored_pre_hashes": True,
        "target_files": ["CODEX.md"],
    }
    assert rollback_row == (
        str(decision_trace["decision"]["decision_id"]),
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


def test_rollback_execute_transitions_originating_daemon_job_to_rolled_back(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    job_id = _seed_daemon_waiting_review_job(repo, run_dir)
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0

    assert main(["rollback", "--repo", str(repo), "--decision", "latest", "--execute"]) == 0

    with DaemonQueue.open_sidecar(repo) as queue:
        job = queue.get_job(job_id)
    assert job is not None
    assert job.state is JobState.ROLLED_BACK
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            "SELECT state FROM daemon_jobs WHERE job_id = ? AND repo_path = ?",
            (str(job_id), str(repo)),
        ).fetchone()
        event = connection.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE event_type = 'daemon_job.state_changed'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
    assert row == (JobState.ROLLED_BACK.value,)
    assert event is not None
    assert json.loads(event[0])["state"] == JobState.ROLLED_BACK.value


def test_rollback_execute_is_blocked_by_read_only_kill_switch_before_revert(
    tmp_path: Path,
    capsys,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    applied_text = (repo / "CODEX.md").read_text(encoding="utf-8")
    applied_head = _git(repo, "rev-parse", "HEAD")
    (repo / ".sidecar" / "read-only.kill").write_text("enabled\n", encoding="utf-8")

    assert main(["rollback", "--repo", str(repo), "--decision", "latest", "--execute"]) == 1

    assert "rollback blocked: read-only kill switch is enabled" in capsys.readouterr().out
    assert _git(repo, "rev-parse", "HEAD") == applied_head
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == applied_text
    assert not (run_dir / "rollback-plan.json").exists()


def test_rollback_execute_handles_git_revert_conflict_without_success_artifacts(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0
    (repo / "CODEX.md").write_text(
        "# Rules\n\nUse tests.\nRecord rollback notes and keep them.\n",
        encoding="utf-8",
    )
    _git(repo, "add", "CODEX.md")
    _git(repo, "commit", "-m", "intervening rollback-note edit")

    assert main(["rollback", "--repo", str(repo), "--decision", "latest", "--execute"]) == 1

    assert not (run_dir / "rollback-plan.json").exists()
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event_type = 'rollback.applied'"
            ).fetchone()[0]
            == 0
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


def test_apply_commit_transitions_originating_daemon_job_to_applied(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    job_id = _seed_daemon_waiting_review_job(repo, run_dir)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0

    with DaemonQueue.open_sidecar(repo) as queue:
        job = queue.get_job(job_id)
    assert job is not None
    assert job.state is JobState.APPLIED
    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            "SELECT state FROM daemon_jobs WHERE job_id = ? AND repo_path = ?",
            (str(job_id), str(repo)),
        ).fetchone()
        event = connection.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE event_type = 'daemon_job.state_changed'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
    assert row == (JobState.APPLIED.value,)
    assert event is not None
    payload = json.loads(event[0])
    assert payload["job_id"] == str(job_id)
    assert payload["state"] == JobState.APPLIED.value
    assert apply_plan["candidate_id"] == 7


def test_apply_without_daemon_origin_does_not_create_queue_state(tmp_path: Path):
    repo = _init_repo(tmp_path)
    _candidate_run(repo)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "commit"]) == 0

    assert not (repo / ".sidecar" / "daemon.sqlite").exists()
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daemon_jobs").fetchone()[0] == 0


def test_daemon_payload_matching_recognizes_run_candidate_and_nested_shapes():
    assert not cli_module._daemon_payload_matches_run_candidate("not-json", "run-1", 7)
    assert cli_module._daemon_payload_matches_run_candidate({"run_id": "run-1"}, "run-1", 7)
    assert cli_module._daemon_payload_matches_run_candidate({"candidate_id": 7}, "run-1", 7)
    assert cli_module._daemon_payload_matches_run_candidate(
        {"execution_payload": {"candidate_id": "7"}},
        "run-1",
        7,
    )
    assert cli_module._daemon_payload_matches_run_candidate(
        {"resume": {"run_id": "run-1"}},
        "run-1",
        7,
    )
    assert cli_module._daemon_payload_matches_run_candidate(
        {"payload": {"run_id": "run-1"}},
        "run-1",
        7,
    )
    assert not cli_module._daemon_payload_matches_run_candidate(
        {"candidate_id": "latest"},
        "run-1",
        7,
    )


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
        "recorded_provenance": {
            "audit_id": 1,
            "eval_id": 1,
            "policy_decision_id": 1,
            "audit_event_sequences": {
                "audit": 1,
                "candidate": 2,
                "eval": 3,
                "policy_decision": 4,
            },
        },
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
            ]
        )
        == 1
    )

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()


def test_auto_apply_preflight_reports_ineligible_candidate_without_mutation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="General")
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_head = _git(repo, "rev-parse", "HEAD")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--actor",
                "operator@example.com",
                "--preflight",
            ]
        )
        == 0
    )

    report = json.loads((run_dir / "auto-apply-preflight.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["run_id"] == run_dir.name
    assert report["candidate_id"] == 7
    assert report["eligible"] is False
    assert report["lane"] is None
    assert report["reasons"] == [
        "cli_confirmation_required",
        "auto_apply_change_type_not_allowed",
    ]
    assert report["mode"] == "commit"
    assert report["would_apply"] is False
    assert report["checks"]["policy_gate"] == {"allowed": True, "reasons": []}
    assert report["checks"]["stored_policy_gate"] == {"allowed": True, "reasons": []}
    assert report["checks"]["eval_report"] == {
        "candidate_id_matches": True,
        "passed": True,
        "recommendation": "accept",
        "suite_id": "all",
    }
    assert report["checks"]["vcs"]["base_hashes_match"] is True
    assert report["readiness_metrics"]["reviewed_count"] == 20
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert _git(repo, "status", "--porcelain=v1", "--", "CODEX.md") == ""
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()
    assert _auto_apply_decision_payloads(repo) == []


def test_auto_apply_preflight_reports_eligible_confirmed_candidate(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--actor",
                "operator@example.com",
                "--preflight",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
            ]
        )
        == 0
    )

    report = json.loads((run_dir / "auto-apply-preflight.json").read_text(encoding="utf-8"))
    assert report["eligible"] is True
    assert report["lane"] == "docs_hygiene"
    assert report["reasons"] == []
    assert report["would_apply"] is True
    assert report["approval_bundle"]["actor"] == "operator@example.com"
    assert report["approval_bundle"]["rollback_command"] == [
        "tugboat",
        "rollback",
        "--repo",
        str(repo.resolve()),
        "--decision",
        run_dir.name,
        "--execute",
    ]
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()


def test_auto_apply_shadow_records_would_apply_without_mutation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_head = _git(repo, "rev-parse", "HEAD")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--actor",
                "operator@example.com",
                "--shadow",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
            ]
        )
        == 0
    )

    report = json.loads((run_dir / "auto-apply-shadow.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert report["run_id"] == run_dir.name
    assert report["candidate_id"] == 7
    assert report["shadow_mode"] is True
    assert report["eligible"] is True
    assert report["would_apply"] is True
    assert report["lane"] == "docs_hygiene"
    assert report["reasons"] == []
    assert report["approval_bundle"]["actor"] == "operator@example.com"
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()
    assert _auto_apply_decision_payloads(repo) == []
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE event_type = 'auto_apply.shadowed'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    shadow_payload = json.loads(row[0])
    assert shadow_payload["candidate_id"] == 7
    assert shadow_payload["run_id"] == run_dir.name
    assert shadow_payload["actor"] == "operator@example.com"
    assert shadow_payload["eligible"] is True
    assert shadow_payload["would_apply"] is True
    assert shadow_payload["lane"] == "docs_hygiene"
    assert shadow_payload["reasons"] == []
    assert shadow_payload["report_path"] == (
        ".sidecar/runs/20260525T000000000000Z/auto-apply-shadow.json"
    )


def test_auto_apply_shadow_records_ineligible_candidate_without_mutation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report["passed"] = False
    eval_report["recommendation"] = "reject"
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_head = _git(repo, "rev-parse", "HEAD")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--actor",
                "operator@example.com",
                "--shadow",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
            ]
        )
        == 0
    )

    report = json.loads((run_dir / "auto-apply-shadow.json").read_text(encoding="utf-8"))
    assert report["shadow_mode"] is True
    assert report["eligible"] is False
    assert report["would_apply"] is False
    assert "eval_report_rejected" in report["reasons"]
    assert report["approval_bundle"] is None
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()
    assert _auto_apply_decision_payloads(repo) == []
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        row = connection.execute(
            """
            SELECT payload_json FROM audit_events
            WHERE event_type = 'auto_apply.shadowed'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
    assert row is not None
    shadow_payload = json.loads(row[0])
    assert shadow_payload["eligible"] is False
    assert shadow_payload["would_apply"] is False
    assert "eval_report_rejected" in shadow_payload["reasons"]


def test_auto_apply_preflight_reports_eval_rejection_without_mutation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report["passed"] = False
    eval_report["recommendation"] = "reject"
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--actor",
                "operator@example.com",
                "--preflight",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
            ]
        )
        == 0
    )

    report = json.loads((run_dir / "auto-apply-preflight.json").read_text(encoding="utf-8"))
    assert report["eligible"] is False
    assert report["would_apply"] is False
    assert "eval_report_rejected" in report["reasons"]
    assert report["checks"]["eval_report"]["acceptance_reason"] == "eval report did not pass"
    assert report["approval_bundle"] is None
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()


def test_auto_apply_preflight_reports_vcs_failure_without_mutation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    original_head = _git(repo, "rev-parse", "HEAD")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)
    (repo / "CODEX.md").write_text("# Rules\n\nUse dirty tests.\n", encoding="utf-8")

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--actor",
                "operator@example.com",
                "--preflight",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
            ]
        )
        == 0
    )

    report = json.loads((run_dir / "auto-apply-preflight.json").read_text(encoding="utf-8"))
    assert report["eligible"] is False
    assert "vcs_preflight_failed" in report["reasons"]
    assert report["checks"]["vcs"]["preflight_passed"] is False
    assert report["checks"]["vcs"]["worktree_clean"] is False
    assert report["checks"]["vcs"]["target_files_clean"] is False
    assert report["checks"]["vcs"]["base_hashes_match"] is False
    assert report["checks"]["vcs"]["dirty_paths"] == ["CODEX.md"]
    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()


def test_auto_apply_preflight_reports_policy_pause_controls(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    _write_auto_apply_policy(repo, version=9)
    policy_path = repo / ".sidecar" / "policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8")
        + """
  paused_repositories:
    - {repo}
  paused_lanes:
    - docs_hygiene
  paused_categories:
    - typo_fix
  pause_for_incident: true
""".format(repo=repo),
        encoding="utf-8",
    )
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--actor",
                "operator@example.com",
                "--preflight",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
            ]
        )
        == 0
    )

    report = json.loads((run_dir / "auto-apply-preflight.json").read_text(encoding="utf-8"))
    assert report["eligible"] is False
    assert report["lane"] == "docs_hygiene"
    assert report["reasons"] == [
        "auto_apply_repository_paused",
        "auto_apply_lane_paused",
        "auto_apply_category_paused",
        "auto_apply_incident_pause_active",
    ]
    policy_snapshot = report["checks"]["auto_apply"]["policy"]
    assert policy_snapshot["paused_repositories"] == [str(repo.resolve())]
    assert policy_snapshot["paused_lanes"] == ["docs_hygiene"]
    assert policy_snapshot["paused_categories"] == ["typo_fix"]
    assert policy_snapshot["pause_for_incident"] is True
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()


def test_auto_apply_commit_blocks_policy_paused_candidate_before_apply(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    original_head = _git(repo, "rev-parse", "HEAD")
    _write_auto_apply_policy(repo, version=9)
    policy_path = repo / ".sidecar" / "policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8")
        + """
  paused_lanes:
    - docs_hygiene
""",
        encoding="utf-8",
    )
    _seed_auto_apply_history(repo)

    assert (
        main(
            [
                "auto-apply",
                "--repo",
                str(repo),
                "--candidate",
                "latest",
                "--actor",
                "operator@example.com",
                "--confirm-auto-apply",
                "--auto-apply-policy-version",
                "9",
            ]
        )
        == 1
    )

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert _git(repo, "rev-parse", "HEAD") == original_head
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()
    decisions = _auto_apply_decision_payloads(repo)
    assert len(decisions) == 1
    assert decisions[0]["eligible"] is False
    assert decisions[0]["lane"] == "docs_hygiene"
    assert decisions[0]["reasons"] == ["auto_apply_lane_paused"]
    assert decisions[0]["policy"]["paused_lanes"] == ["docs_hygiene"]


@pytest.mark.parametrize("command", ("apply", "auto-apply"))
@pytest.mark.parametrize("removed_flag", ("--burn-in-days", "--rejection-rate", "--rollback-rate"))
def test_auto_apply_thresholds_are_policy_owned_not_runtime_cli_knobs(
    command: str,
    removed_flag: str,
):
    args = [command, "--repo", ".", "--candidate", "latest"]
    if command == "auto-apply":
        args.extend(["--actor", "operator@example.com"])
    args.extend([removed_flag, "1"])

    with pytest.raises(SystemExit) as error:
        cli_module.build_parser().parse_args(args)

    assert error.value.code == 2


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
        "lane": "docs_hygiene",
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
    assert approval["readiness_metrics"]["burn_in_days"] >= 14
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
    decisions = _auto_apply_decision_payloads(repo)
    assert [payload["phase"] for payload in decisions] == ["precheck", "final"]
    assert [payload["eligible"] for payload in decisions] == [True, True]
    assert [payload["lane"] for payload in decisions] == ["docs_hygiene", "docs_hygiene"]
    assert decisions[0]["candidate"]["change_class"] == "A"
    assert decisions[0]["candidate"]["categories"] == ["A", "typo_fix"]
    assert decisions[0]["candidate"]["changed_lines"] == 1
    assert decisions[0]["vcs"]["commit_sha"] == "pending"
    assert decisions[1]["vcs"]["commit_sha"] == apply_plan["applied_commit"]
    assert decisions[1]["readiness_metrics"] == approval["readiness_metrics"]
    serialized_decisions = json.dumps(decisions, sort_keys=True)
    assert "candidate.diff" not in serialized_decisions
    assert "eval-report" not in serialized_decisions
    assert "rationale" not in serialized_decisions


def test_auto_apply_commit_executes_recorded_rollback_and_restores_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
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
            ]
        )
        == 0
    )
    assert "Record rollback notes." in (repo / "CODEX.md").read_text(encoding="utf-8")
    approval = json.loads((run_dir / "auto-apply-approval.json").read_text(encoding="utf-8"))

    assert main(list(approval["rollback_command"][1:])) == 0

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    rollback_plan = json.loads((run_dir / "rollback-plan.json").read_text(encoding="utf-8"))
    assert rollback_plan["executed"] is True
    assert rollback_plan["restored_pre_hashes"] is True
    assert rollback_plan["revert_commit"] == _git(repo, "rev-parse", "HEAD")
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        events = connection.execute(
            """
            SELECT event_type FROM audit_events
            WHERE event_type IN ('auto_apply.applied', 'rollback.applied')
            ORDER BY sequence
            """
        ).fetchall()
        rollback_row = connection.execute(
            """
            SELECT r.decision_id, d.policy, r.executed, r.rollback_plan
            FROM rollbacks r
            JOIN decisions d ON d.id = CAST(r.decision_id AS INTEGER)
            WHERE r.candidate_id = 7
            """
        ).fetchone()

    assert [row[0] for row in events] == ["auto_apply.applied", "rollback.applied"]
    assert rollback_row == (
        rollback_row[0],
        "auto_apply_controller",
        1,
        ".sidecar/runs/20260525T000000000000Z/rollback-plan.json",
    )


def test_auto_apply_final_gate_failure_cleans_committed_branch_without_silent_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    _write_auto_apply_policy(repo, version=9)
    _seed_auto_apply_history(repo)
    base_branch = _git(repo, "branch", "--show-current")
    base_head = _git(repo, "rev-parse", "HEAD")
    generated_branch = VcsAdapter(repo).branch_name(
        run_id=run_dir.name,
        candidate_id=7,
        base_file="CODEX.md",
    )

    def fail_final_gate(*args: object, **kwargs: object) -> dict[str, object]:
        raise ValueError("simulated final gate failure")

    monkeypatch.setattr(cli_module, "_assert_auto_apply_final", fail_final_gate)

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
            ]
        )
        == 1
    )

    assert _git(repo, "branch", "--show-current") == base_branch
    assert _git(repo, "rev-parse", "HEAD") == base_head
    assert _git(repo, "status", "--porcelain=v1", "--", "CODEX.md") == ""
    assert _git(repo, "branch", "--list", generated_branch) == ""
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        silent_apply_events = connection.execute(
            """
            SELECT COUNT(*) FROM audit_events
            WHERE event_type IN ('apply.applied', 'auto_apply.applied')
            """
        ).fetchone()[0]
        auto_apply_decision = connection.execute(
            """
            SELECT COUNT(*) FROM decisions
            WHERE candidate_id = 7 AND policy = 'auto_apply_controller'
            """
        ).fetchone()[0]

    assert silent_apply_events == 0
    assert auto_apply_decision == 0


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
    assert event_payload["lane"] is None
    assert event_payload["reasons"] == ["auto_apply_change_type_not_allowed"]
    assert event_payload["phase"] == "precheck"
    assert event_payload["candidate"]["change_class"] == "A"
    assert event_payload["candidate"]["categories"] == ["A", "general"]
    assert event_payload["policy"] == {
        "allowed_change_classes": ["A"],
        "allowed_repositories": [str(repo)],
        "enabled": True,
        "lanes": [
            {
                "allowed_categories": [
                    "broken_internal_link",
                    "duplicate_sentence_removal",
                    "formatting_normalization",
                    "stale_command_reference",
                    "typo_fix",
                ],
                "allowed_change_classes": ["A"],
                "enabled": True,
                "max_changed_lines": 50,
                "max_instruction_token_delta": 50,
                "maximum_rejection_rate": 0.2,
                "maximum_rollback_rate": 0.05,
                "minimum_burn_in_days": 3,
                "name": "docs_hygiene",
            },
            {
                "allowed_categories": ["skill_improvement"],
                "allowed_change_classes": ["A"],
                "enabled": True,
                "max_changed_lines": 30,
                "max_instruction_token_delta": 30,
                "maximum_rejection_rate": 0.15,
                "maximum_rollback_rate": 0.03,
                "minimum_burn_in_days": 7,
                "name": "skill_improvement",
            },
        ],
        "max_changed_lines": 50,
        "max_instruction_token_delta": 50,
        "maximum_rejection_rate": 0.10,
        "maximum_rollback_rate": 0.02,
        "minimum_burn_in_days": 14,
        "pause_for_incident": False,
        "paused_categories": [],
        "paused_lanes": [],
        "paused_repositories": [],
        "version": 9,
    }
    assert event_payload["readiness_metrics"] == {
        "applied_count": 20,
        "burn_in_days": event_payload["readiness_metrics"]["burn_in_days"],
        "rejected_count": 0,
        "rejection_rate": 0.0,
        "reviewed_count": 20,
        "rollback_count": 0,
        "rollback_rate": 0.0,
        "source_audit_range": event_payload["readiness_metrics"]["source_audit_range"],
    }
    assert event_payload["readiness_metrics"]["burn_in_days"] >= 14
    assert event_payload["readiness_metrics"]["source_audit_range"]["first_sequence"] is not None
    assert event_payload["readiness_metrics"]["source_audit_range"]["last_sequence"] is not None
    serialized_payload = json.dumps(event_payload, sort_keys=True)
    assert "candidate.diff" not in serialized_payload
    assert "eval-report" not in serialized_payload
    assert "rationale" not in serialized_payload


def test_auto_apply_blocks_candidate_over_policy_token_growth_limit(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report["metrics"]["instruction_token_delta"] = 6
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    original = (repo / "CODEX.md").read_text(encoding="utf-8")
    _write_auto_apply_policy(repo, version=9, max_instruction_token_delta=5)
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
            ]
        )
        == 1
    )

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()
    decision = _auto_apply_decision_payloads(repo)[-1]
    assert decision["eligible"] is False
    assert decision["lane"] == "docs_hygiene"
    assert decision["reasons"] == ["max_instruction_token_delta_exceeded"]
    assert decision["candidate"]["instruction_token_delta"] == 6
    assert decision["policy"]["max_instruction_token_delta"] == 5
    assert decision["policy"]["lanes"][0]["max_instruction_token_delta"] == 50


def test_auto_apply_blocks_when_eval_report_omits_token_growth_metric(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    del eval_report["metrics"]["instruction_token_delta"]
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
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
            ]
        )
        == 1
    )

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    decision = _auto_apply_decision_payloads(repo)[-1]
    assert decision["eligible"] is False
    assert decision["reasons"] == ["instruction_token_delta_missing"]
    assert decision["candidate"]["instruction_token_delta"] is None


def test_auto_apply_blocks_underclassified_class_a_candidate_touching_policy_domain(
    tmp_path: Path,
    capsys,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Provider Routing")
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
            ]
        )
        == 1
    )

    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original
    assert not (run_dir / "apply-plan.json").exists()
    assert not (run_dir / "auto-apply-approval.json").exists()
    assert "apply blocked: Class C candidates require explicit human review" in capsys.readouterr().out


def test_auto_apply_respects_policy_allowed_risk_classes(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    _write_auto_apply_policy(repo, allowed_risk_classes=("B",))
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
            ]
        )
        == 1
    )

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
    assert "change_class_not_allowed" in event_payload["reasons"]


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
            ]
        )
        == 0
    )

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    approval = json.loads((run_dir / "auto-apply-approval.json").read_text(encoding="utf-8"))
    assert apply_plan["mode"] == "commit"
    assert apply_plan["auto_apply"] is True
    assert approval["actor"] == "operator@example.com"


def test_auto_apply_uses_ledger_burn_in_without_cli_override(tmp_path: Path):
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
            ]
        )
        == 1
    )

    assert not (run_dir / "apply-plan.json").exists()


def test_auto_apply_uses_ledger_rejection_and_rollback_rates_without_cli_overrides(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A")
    _write_auto_apply_policy(repo, version=5)
    _seed_auto_apply_history(repo, reviewed=20, rejected=5, applied=20, rollbacks=2)

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
            ]
        )
        == 1
    )

    assert not (run_dir / "apply-plan.json").exists()
    decisions = _auto_apply_decision_payloads(repo)
    assert len(decisions) == 1
    event_payload = decisions[0]
    assert event_payload["phase"] == "precheck"
    assert event_payload["eligible"] is False
    assert event_payload["readiness_metrics"]["reviewed_count"] == 20
    assert event_payload["readiness_metrics"]["rejected_count"] == 5
    assert event_payload["readiness_metrics"]["rejection_rate"] == 0.25
    assert event_payload["readiness_metrics"]["rollback_count"] == 2
    assert event_payload["readiness_metrics"]["rollback_rate"] == 0.1
    assert event_payload["readiness_metrics"]["source_audit_range"]["first_sequence"] is not None
    assert event_payload["readiness_metrics"]["source_audit_range"]["last_sequence"] is not None
    assert "rejection_rate_too_high" in event_payload["reasons"]
    assert "rollback_rate_too_high" in event_payload["reasons"]


def test_auto_apply_precheck_blocks_without_eval_evidence(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    (run_dir / "eval-report.json").unlink()
    _write_auto_apply_policy(repo, version=6)
    _seed_auto_apply_history(repo)
    candidate = cli_module._candidate_from_artifacts(run_dir)

    with pytest.raises(
        ValueError,
        match="held_out_eval_failed, governance_regression_failed",
    ):
        cli_module._assert_auto_apply_precheck(
            repo,
            run_dir,
            candidate_id=7,
            candidate=candidate,
            mode="commit",
            branch_name="tugboat/candidate-7",
            review_actor="operator@example.com",
            confirmed=True,
            policy_version=6,
        )

    decision = _auto_apply_decision_payloads(repo)[-1]
    assert decision["phase"] == "precheck"
    assert decision["eligible"] is False
    assert decision["reasons"] == [
        "instruction_token_delta_missing",
        "held_out_eval_failed",
        "governance_regression_failed",
    ]
    assert decision["candidate"]["held_out_eval_passed"] is False
    assert decision["candidate"]["governance_regression_passed"] is False
    assert decision["candidate"]["instruction_token_delta"] is None


def test_auto_apply_final_uses_eval_report_governance_result(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report["governance_passed"] = False
    eval_report["passed"] = True
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_auto_apply_policy(repo, version=7)
    _seed_auto_apply_history(repo)
    candidate = cli_module._candidate_from_artifacts(run_dir)

    with pytest.raises(ValueError, match="governance_regression_failed"):
        cli_module._assert_auto_apply_final(
            repo,
            run_dir,
            candidate_id=7,
            candidate=candidate,
            mode="commit",
            branch_name="tugboat/candidate-7",
            applied_commit="abc123",
            review_actor="operator@example.com",
            confirmed=True,
            policy_version=7,
        )

    decision = _auto_apply_decision_payloads(repo)[-1]
    assert decision["phase"] == "final"
    assert decision["eligible"] is False
    assert decision["reasons"] == ["governance_regression_failed"]
    assert decision["candidate"]["held_out_eval_passed"] is True
    assert decision["candidate"]["governance_regression_passed"] is False


def test_auto_apply_eval_evidence_rejects_candidate_mismatch_and_malformed_artifacts(
    tmp_path: Path,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report["candidate_id"] = 99
    (run_dir / "eval-report.json").write_text(
        json.dumps(eval_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    assert cli_module._auto_apply_eval_evidence(run_dir, candidate_id=7) == (
        False,
        False,
    )

    (run_dir / "eval-report.json").write_text("{not-json\n", encoding="utf-8")
    assert cli_module._auto_apply_eval_evidence(run_dir, candidate_id=7) == (
        False,
        False,
    )


@pytest.mark.parametrize(
    ("patch", "expected"),
    [
        ({"trigger_score": 0.9, "held_out_score": 0.9}, False),
        ({"trigger_score": None}, False),
        ({"validation_splits": {"trigger": ["same"], "held_out": ["same"]}}, False),
        ({"metrics": {"regression_score": 2, "baseline_regression_score": 1}}, False),
        ({"metrics": {"regression_score": 1, "baseline_regression_score": 1}}, True),
        ({"trigger_score": "bad"}, False),
    ],
)
def test_auto_apply_held_out_eval_evidence_checks_validation_shape(
    tmp_path: Path,
    patch: dict[str, object],
    expected: bool,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo, risk_class="A", bounded_section="Typo Fix")
    eval_report = json.loads((run_dir / "eval-report.json").read_text(encoding="utf-8"))
    eval_report.update(patch)

    assert cli_module._auto_apply_held_out_eval_passed(eval_report) is expected


def test_apply_branch_mode_creates_branch_and_applies_patch_without_commit(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "branch"]) == 0

    apply_plan = json.loads((run_dir / "apply-plan.json").read_text(encoding="utf-8"))
    assert _git(repo, "branch", "--show-current") == apply_plan["branch_name"]
    assert apply_plan["mode"] == "branch"
    assert apply_plan["applied_commit"] == ""
    assert apply_plan["rollback_command"] == [
        ["git", "restore", "--worktree", "--staged", "--", "CODEX.md"],
        ["git", "switch", "main"],
        ["git", "branch", "-D", apply_plan["branch_name"]],
    ]
    assert "Record rollback notes." in (repo / "CODEX.md").read_text(encoding="utf-8")
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        applied_event = connection.execute(
            """
            SELECT payload_json
            FROM audit_events
            WHERE event_type = 'apply.applied'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
        decision = connection.execute(
            "SELECT decision, applied_commit FROM decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert applied_event is not None
    applied_payload = json.loads(applied_event[0])
    assert applied_payload["mode"] == "branch"
    assert applied_payload["applied_commit"] == ""
    assert applied_payload["post_hashes"] == apply_plan["post_hashes"]
    assert decision == ("applied", "")


def test_apply_pr_mode_rejects_without_pull_request_config(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    original_branch = _git(repo, "branch", "--show-current")
    original_text = (repo / "CODEX.md").read_text(encoding="utf-8")

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "pr"]) == 1

    assert _git(repo, "branch", "--show-current") == original_branch
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original_text
    assert "tugboat/20260525t000000000000z/candidate-7/codex-md" not in _git(repo, "branch")
    assert not (run_dir / "apply-plan.json").exists()


def test_apply_pr_mode_rejects_unsupported_provider_before_vcs_mutation(tmp_path: Path):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    _write_pr_policy(repo, provider="webhook")
    original_branch = _git(repo, "branch", "--show-current")
    original_text = (repo / "CODEX.md").read_text(encoding="utf-8")

    assert main(["apply", "--repo", str(repo), "--candidate", "latest", "--mode", "pr"]) == 1

    assert _git(repo, "branch", "--show-current") == original_branch
    assert (repo / "CODEX.md").read_text(encoding="utf-8") == original_text
    assert "tugboat/20260525t000000000000z/candidate-7/codex-md" not in _git(repo, "branch")
    assert not (run_dir / "apply-plan.json").exists()


def test_apply_pr_mode_creates_configured_pull_request_and_records_result(
    tmp_path: Path,
    monkeypatch,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    _write_pr_policy(repo, remote="upstream", base_branch="trunk", draft=False)
    calls: dict[str, object] = {}

    def record_push(self, remote: str, branch_name: str) -> None:
        calls["push"] = {"remote": remote, "branch_name": branch_name}

    def record_pr(self, metadata, *, provider: str):
        calls["create"] = {"provider": provider, "metadata": metadata.to_json_dict()}
        return cli_module.PullRequestResult(
            provider=provider,
            created=True,
            url="https://github.com/syndicalt/tugboat/pull/42",
            number=42,
        )

    monkeypatch.setattr(cli_module.VcsAdapter, "push_branch", record_push)
    monkeypatch.setattr(cli_module.VcsAdapter, "create_pull_request", record_pr)

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
        "base_branch": "trunk",
        "body": apply_plan["pr_metadata"]["body"],
        "branch_name": apply_plan["branch_name"],
        "draft": False,
        "title": "tugboat: apply candidate 7 for CODEX.md",
    }
    assert calls["push"] == {"remote": "upstream", "branch_name": apply_plan["branch_name"]}
    assert calls["create"] == {
        "provider": "github_cli",
        "metadata": apply_plan["pr_metadata"],
    }
    assert apply_plan["pr_result"] == {
        "created": True,
        "number": 42,
        "provider": "github_cli",
        "url": "https://github.com/syndicalt/tugboat/pull/42",
    }
    assert "Candidate: 7" in apply_plan["pr_metadata"]["body"]
    with closing(sqlite3.connect(repo / ".sidecar" / "db.sqlite")) as connection:
        event = connection.execute(
            """
            SELECT payload_json
            FROM audit_events
            WHERE event_type = 'apply.applied'
            ORDER BY sequence DESC
            LIMIT 1
            """
        ).fetchone()
    assert event is not None
    assert json.loads(event[0])["pr_result"] == apply_plan["pr_result"]


def test_apply_pr_mode_cleans_generated_branch_when_commit_fails(
    tmp_path: Path,
    monkeypatch,
):
    repo = _init_repo(tmp_path)
    run_dir = _candidate_run(repo)
    _write_pr_policy(repo)
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
