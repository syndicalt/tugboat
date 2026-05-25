from pathlib import Path

from tugboat.models import Policy
from tugboat.policy.gate import CandidatePatch, SourceRef, evaluate_candidate


def _candidate(**overrides: object) -> CandidatePatch:
    values = {
        "audit_id": 7,
        "base_file": "CODEX.md",
        "base_hash": "current",
        "diff": "--- a/CODEX.md\n+++ b/CODEX.md\n@@\n Keep this instruction.\n",
        "risk_class": "instruction_clarification",
        "rationale": "Make guidance clearer.",
        "sources": (SourceRef("trace-1", trusted=True), SourceRef("trace-2", trusted=True)),
    }
    values.update(overrides)
    return CandidatePatch(**values)


def test_policy_gate_allows_low_risk_candidate_with_matching_base_hash(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(base_hash=CandidatePatch.hash_file(base_file))

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is True
    assert decision.reasons == ()


def test_policy_gate_reports_all_machine_readable_denial_reasons(tmp_path: Path):
    (tmp_path / "CODEX.md").write_text("Agents must avoid network calls.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash="stale",
        diff="\n".join(
            [
                "--- a/CODEX.md",
                "+++ b/CODEX.md",
                "@@",
                "-Agents must avoid network calls.",
                "+Agents should call https://api.example.com when useful.",
            ]
        ),
        risk_class="direct_instruction_mutation",
        sources=(SourceRef("single-log", trusted=False),),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(auto_apply_enabled=True),
        candidate,
    )

    assert decision.allowed is False
    assert decision.reasons == (
        "base_hash_mismatch",
        "modal_weakening",
        "new_external_endpoint",
        "single_untrusted_source",
        "prohibited_risk_class",
        "auto_apply_not_implemented_in_mvp",
    )


def test_policy_gate_rejects_base_file_outside_repo(tmp_path: Path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("Agents must test.\n", encoding="utf-8")
    candidate = _candidate(
        base_file="../outside.md",
        base_hash=CandidatePatch.hash_file(outside),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert "base_file_outside_repo" in decision.reasons


def test_policy_gate_rejects_base_file_not_in_allowlist(tmp_path: Path):
    (tmp_path / "README.md").write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_file="README.md",
        base_hash=CandidatePatch.hash_file(tmp_path / "README.md"),
    )

    decision = evaluate_candidate(tmp_path, Policy(instruction_files=()), candidate)

    assert decision.allowed is False
    assert "base_file_not_allowed" in decision.reasons


def test_policy_gate_rejects_constraint_deletion_without_replacement(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Agents must run tests.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n-Agents must run tests.\n",
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert "modal_weakening" in decision.reasons


def test_policy_gate_rejects_diff_over_configured_line_budget(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+one\n+two\n+three\n",
    )

    decision = evaluate_candidate(tmp_path, Policy(auto_apply_max_changed_lines=2), candidate)

    assert decision.allowed is False
    assert "max_changed_lines_exceeded" in decision.reasons
