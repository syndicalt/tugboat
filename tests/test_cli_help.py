from __future__ import annotations

import pytest

from tugboat.cli import build_parser


def _help_for(*args: str, capsys) -> str:
    parser = build_parser()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args([*args, "--help"])
    assert raised.value.code == 0
    return capsys.readouterr().out


def _normalized(value: str) -> str:
    return " ".join(value.split())


def test_apply_help_explains_reviewed_mutation_modes(capsys):
    output = _normalized(_help_for("apply", capsys=capsys))

    assert "proposal mode writes review artifacts without changing files" in output
    assert "branch, commit, and pr modes require VCS safety checks" in output
    assert "policy gate, eval report, and rollback plan" in output


def test_auto_apply_help_explains_confirmation_and_non_mutating_modes(capsys):
    output = _normalized(_help_for("auto-apply", capsys=capsys))

    assert "disabled unless repo policy and CLI confirmation pass" in output
    assert "preflight and shadow record evidence without applying patches" in output
    assert "read-only kill switch blocks writes" in output


def test_rollback_help_explains_execute_safety_boundary(capsys):
    output = _normalized(_help_for("rollback", capsys=capsys))

    assert "without --execute, rollback writes a reviewable plan" in output
    assert "--execute performs the recorded VCS revert" in output
    assert "read-only mode blocks execution" in output


def test_daemon_help_explains_local_worker_and_kill_switch(capsys):
    output = _normalized(_help_for("daemon", capsys=capsys))

    assert "local sidecar worker" in output
    assert "read-only kill switch blocks write jobs" in output


def test_ops_help_explains_evidence_and_destructive_action_boundaries(capsys):
    output = _normalized(_help_for("ops", capsys=capsys))

    assert "backup, restore, migration, observability, and release evidence" in output
    assert "destructive operations require explicit execute or apply flags" in output


def test_ops_subcommand_help_explains_dry_run_boundaries(capsys):
    backup = _normalized(_help_for("ops", "backup", capsys=capsys))
    migrate = _normalized(_help_for("ops", "migrate", capsys=capsys))
    restore = _normalized(_help_for("ops", "restore", capsys=capsys))

    assert "plans backup unless --execute is supplied" in backup
    assert "dry-run migration unless --apply is supplied" in migrate
    assert "plans restore unless --execute is supplied" in restore
