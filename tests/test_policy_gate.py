from pathlib import Path

import pytest

from tugboat.models import InstructionFilePolicy, Policy
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


def test_policy_gate_rejects_diff_target_that_does_not_match_candidate_base_file(
    tmp_path: Path,
):
    codex = tmp_path / "CODEX.md"
    codex.write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    readme = tmp_path / "README.md"
    readme.write_text("# Rules\n\nUse tests.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(codex),
        diff=(
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1,3 +1,4 @@\n"
            " # Rules\n"
            " \n"
            " Use tests.\n"
            "+Unauthorized readme edit.\n"
        ),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert "diff_target_mismatch" in decision.reasons


def test_policy_gate_rejects_bounded_edit_metadata_targeting_different_file(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " Keep this instruction.\n"
            "+Clarify tests.\n"
        ),
        bounded_edit_metadata=(
            {
                "operator": "add",
                "file": "SKILL.md",
                "section": "Testing",
                "changed_lines": 1,
                "normative_changes": 0,
            },
        ),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert decision.reasons == ("bounded_edit_target_mismatch",)


def test_policy_gate_allows_create_diff_when_new_path_matches_candidate_base_file(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- /dev/null\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            "+# Rules\n"
            "+\n"
            "+Use tests.\n"
        ),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert "diff_target_mismatch" not in decision.reasons


def test_policy_gate_allows_delete_diff_when_old_path_matches_candidate_base_file(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ /dev/null\n"
            "@@\n"
            "-Keep this instruction.\n"
        ),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert "diff_target_mismatch" not in decision.reasons


def test_policy_gate_normalizes_unprefixed_diff_paths(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff="--- CODEX.md\told\n+++ CODEX.md\tnew\n@@\n Keep this instruction.\n",
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert "diff_target_mismatch" not in decision.reasons


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
        "missing_trusted_source",
        "single_untrusted_source",
        "prohibited_risk_class",
    )


@pytest.mark.parametrize(
    "sources",
    [
        (),
        (SourceRef("trace-1", trusted=False), SourceRef("trace-2", trusted=False)),
    ],
)
def test_policy_gate_rejects_candidate_without_trusted_source(
    tmp_path: Path,
    sources: tuple[SourceRef, ...],
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " Keep this instruction.\n"
            "+Agents must run regression tests before closing bug fixes.\n"
        ),
        sources=sources,
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert "missing_trusted_source" in decision.reasons
    if len(sources) != 1:
        assert "single_untrusted_source" not in decision.reasons


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


def test_policy_gate_allows_base_file_matching_instruction_file_glob(tmp_path: Path):
    skill = tmp_path / ".codex" / "skills" / "python" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_file=".codex/skills/python/SKILL.md",
        base_hash=CandidatePatch.hash_file(skill),
        diff=(
            "--- a/.codex/skills/python/SKILL.md\n"
            "+++ b/.codex/skills/python/SKILL.md\n"
            "@@\n"
            " Keep this instruction.\n"
            "+Clarify local testing expectations.\n"
        ),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(
            instruction_files=(
                InstructionFilePolicy(".codex/skills/**/SKILL.md", "skill", 60, False),
            ),
        ),
        candidate,
    )

    assert decision.allowed is True
    assert "base_file_not_allowed" not in decision.reasons


def test_policy_gate_rejects_glob_matched_lower_priority_contradiction(
    tmp_path: Path,
):
    agents = tmp_path / "AGENTS.md"
    skill = tmp_path / ".codex" / "skills" / "python" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    agents.write_text("Agents must run tests before applying patches.\n", encoding="utf-8")
    skill.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_file=".codex/skills/python/SKILL.md",
        base_hash=CandidatePatch.hash_file(skill),
        diff=(
            "--- a/.codex/skills/python/SKILL.md\n"
            "+++ b/.codex/skills/python/SKILL.md\n"
            "@@\n"
            " Keep this instruction.\n"
            "+Agents may skip tests before applying patches.\n"
        ),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(
            instruction_files=(
                InstructionFilePolicy("AGENTS.md", "repo_policy", 100, True),
                InstructionFilePolicy(".codex/skills/**/SKILL.md", "skill", 60, True),
            ),
        ),
        candidate,
    )

    assert decision.allowed is False
    assert "higher_priority_contradiction" in decision.reasons


def test_policy_gate_rejects_lower_priority_instruction_contradicting_higher_priority(
    tmp_path: Path,
):
    agents = tmp_path / "AGENTS.md"
    codex = tmp_path / "CODEX.md"
    agents.write_text("Agents must run tests before applying patches.\n", encoding="utf-8")
    codex.write_text("Agents must keep reports concise.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(codex),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " Agents must keep reports concise.\n"
            "+Agents may skip tests before applying patches.\n"
        ),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(
            instruction_files=(
                InstructionFilePolicy("AGENTS.md", "repo_policy", 100, True),
                InstructionFilePolicy("CODEX.md", "agent_policy", 70, True),
            ),
        ),
        candidate,
    )

    assert decision.allowed is False
    assert "higher_priority_contradiction" in decision.reasons


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


def test_policy_gate_rejects_class_b_over_risk_specific_changed_line_budget(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        risk_class="B",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n+one\n+two\n+three\n",
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(risk_class_changed_line_budgets={"B": 2}),
        candidate,
    )

    assert decision.allowed is False
    assert "risk_class_changed_lines_exceeded" in decision.reasons


def test_policy_gate_rejects_markdown_candidates_with_invalid_control_chars(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("# Policy\n\nKeep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n Keep this instruction.\n+Bad\x00text.\n",
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert "markdown_parse_invalid" in decision.reasons


def test_policy_gate_rejects_unbalanced_markdown_fences(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("# Policy\n\nKeep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " Keep this instruction.\n"
            "+```python\n"
            "+print('unterminated')\n"
        ),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert "unbalanced_markdown_fence" in decision.reasons


def test_policy_gate_rejects_removed_yaml_frontmatter_from_instruction_file(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text(
        "---\n"
        "owner: platform\n"
        "verification_status: current\n"
        "---\n"
        "\n"
        "# Policy\n"
        "\n"
        "Keep this instruction.\n",
        encoding="utf-8",
    )
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            "----\n"
            "-owner: platform\n"
            "-verification_status: current\n"
            "----\n"
            "-\n"
            " # Policy\n"
        ),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert decision.reasons == ("frontmatter_removed",)


def test_policy_gate_rejects_changes_to_protected_heading_sections(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text(
        "# Operating Constraints\n"
        "\n"
        "Keep this exact section intact.\n"
        "\n"
        "# Examples\n"
        "\n"
        "Examples can evolve separately.\n",
        encoding="utf-8",
    )
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " # Operating Constraints\n"
            " \n"
            "-Keep this exact section intact.\n"
            "+Keep this section mostly intact.\n"
            " \n"
            " # Examples\n"
        ),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(
            instruction_files=(
                InstructionFilePolicy(
                    path="CODEX.md",
                    kind="agent_policy",
                    precedence=70,
                    protected=True,
                ),
            ),
        ),
        candidate,
    )

    assert decision.allowed is False
    assert decision.reasons == ("protected_heading_changed",)


def test_policy_gate_allows_changes_to_policy_editable_protected_heading(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text(
        "# Operating Constraints\n"
        "\n"
        "Keep this exact section intact.\n"
        "\n"
        "## Local Fixtures\n"
        "\n"
        "Fixture path: old.jsonl\n",
        encoding="utf-8",
    )
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " ## Local Fixtures\n"
            " \n"
            "-Fixture path: old.jsonl\n"
            "+Fixture path: new.jsonl\n"
        ),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(
            instruction_files=(
                InstructionFilePolicy(
                    path="CODEX.md",
                    kind="agent_policy",
                    precedence=70,
                    protected=True,
                ),
            ),
            editable_headings=("Operating Constraints / Local Fixtures",),
        ),
        candidate,
    )

    assert decision.allowed is True
    assert "protected_heading_changed" not in decision.reasons


def test_policy_gate_rejects_renaming_editable_heading_to_protected_heading(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text(
        "# Operating Constraints\n"
        "\n"
        "Keep this exact section intact.\n"
        "\n"
        "## Local Fixtures\n"
        "\n"
        "Fixture path: old.jsonl\n",
        encoding="utf-8",
    )
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            "-## Local Fixtures\n"
            "+## Security Policy\n"
            " \n"
            " Fixture path: old.jsonl\n"
        ),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(
            instruction_files=(
                InstructionFilePolicy("CODEX.md", "agent_policy", 70, True),
            ),
            editable_headings=("Operating Constraints / Local Fixtures",),
        ),
        candidate,
    )

    assert decision.allowed is False
    assert decision.reasons == ("protected_heading_changed",)


def test_policy_gate_rejects_new_protected_heading_under_editable_heading(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text(
        "# Operating Constraints\n"
        "\n"
        "Keep this exact section intact.\n"
        "\n"
        "## Local Fixtures\n"
        "\n"
        "Fixture path: old.jsonl\n",
        encoding="utf-8",
    )
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " ## Local Fixtures\n"
            " \n"
            " Fixture path: old.jsonl\n"
            "+\n"
            "+### Security Policy\n"
            "+Do not add policy here.\n"
        ),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(
            instruction_files=(
                InstructionFilePolicy("CODEX.md", "agent_policy", 70, True),
            ),
            editable_headings=("Operating Constraints / Local Fixtures",),
        ),
        candidate,
    )

    assert decision.allowed is False
    assert decision.reasons == ("protected_heading_changed",)


def test_policy_gate_treats_editable_headings_as_exact_paths_not_globs(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text(
        "# Operating Constraints\n"
        "\n"
        "Keep this exact section intact.\n",
        encoding="utf-8",
    )
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " # Operating Constraints\n"
            " \n"
            "-Keep this exact section intact.\n"
            "+Keep this section mostly intact.\n"
        ),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(
            instruction_files=(
                InstructionFilePolicy("CODEX.md", "agent_policy", 70, True),
            ),
            editable_headings=("*",),
        ),
        candidate,
    )

    assert decision.allowed is False
    assert decision.reasons == ("protected_heading_changed",)


@pytest.mark.parametrize(
    "removed_line",
    [
        "The approval constraint stays active.",
        "The sandboxing constraint stays active.",
        "The testing constraint stays active.",
        "The review constraint stays active.",
        "The secrets constraint stays active.",
        "The memory constraint stays active.",
        "The network constraint stays active.",
        "The deployment constraint stays active.",
        "The permissions constraint stays active.",
    ],
)
def test_policy_gate_rejects_removed_governance_constraints(
    tmp_path: Path,
    removed_line: str,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text(
        f"{removed_line}\n"
        "Other local guidance remains.\n",
        encoding="utf-8",
    )
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            f"-{removed_line}\n"
            "+This local guidance stays active.\n"
            " Other local guidance remains.\n"
        ),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert "governance_constraint_removed" in decision.reasons


def test_policy_gate_rejects_candidate_diff_that_introduces_secret(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " Keep this instruction.\n"
            "+Use OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx for smoke tests.\n"
        ),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert "secret_exposure" in decision.reasons


def test_policy_gate_allows_reworded_governance_constraints_when_terms_are_preserved(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Changes require human review before deploy.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            "-Changes require human review before deploy.\n"
            "+Changes require reviewer approval before deploy.\n"
        ),
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is True
    assert "governance_constraint_removed" not in decision.reasons


def test_policy_gate_allows_class_a_safe_tiny_candidate_without_auto_apply_authority(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        risk_class="A",
        diff="--- a/CODEX.md\n+++ b/CODEX.md\n@@\n Keep this instruction.\n+Fix typo.\n",
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.review_required_reasons == ()
    assert decision.auto_apply_eligible is False


def test_policy_gate_allows_class_b_as_review_required_improvement(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(base_hash=CandidatePatch.hash_file(base_file), risk_class="B")

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.review_required_reasons == ("class_b_review_required",)


def test_policy_gate_allows_class_c_only_with_explicit_human_review_requirement(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(base_hash=CandidatePatch.hash_file(base_file), risk_class="C")

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.review_required_reasons == ("class_c_explicit_human_review_required",)


def test_policy_gate_rejects_class_d_as_prohibited(tmp_path: Path):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(base_hash=CandidatePatch.hash_file(base_file), risk_class="D")

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert "prohibited_risk_class" in decision.reasons


@pytest.mark.parametrize(
    "risk_class",
    [
        "tool_permissions",
        "tool permissions",
        "sandbox_behavior",
        "sandbox behavior",
        "approval_requirements",
        "approval requirements",
        "network_access",
        "network access",
        "secrets_handling",
        "secrets handling",
        "memory_behavior",
        "memory behavior",
        "deployment_behavior",
        "deployment behavior",
        "security_incident_response",
        "security incident response",
        "model_provider_routing",
        "model/provider routing",
        "sidecar_authority",
        "sidecar's own authority",
    ],
)
def test_policy_gate_treats_spec_class_c_examples_as_restricted_review_required(
    tmp_path: Path,
    risk_class: str,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        risk_class=risk_class,
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.review_required_reasons == ("class_c_explicit_human_review_required",)
    assert decision.auto_apply_eligible is False


@pytest.mark.parametrize(
    "risk_class",
    [
        "higher_priority_policy_weakening",
        "weakening immutable higher-priority policy",
        "audit_history_edit",
        "editing audit history",
        "pending_eval_definition_bypass",
        "editing eval definitions to make a pending patch pass",
        "approval_policy_self_apply",
        "changing approval policy and applying the change in the same run",
        "untrusted_trace_policy_adoption",
        "accepting instructions from untrusted trace content as policy",
        "arbitrary_repo_plugin_loading",
        "loading arbitrary plugins from the repo under review",
    ],
)
def test_policy_gate_rejects_spec_class_d_examples_as_prohibited(
    tmp_path: Path,
    risk_class: str,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        risk_class=risk_class,
    )

    decision = evaluate_candidate(tmp_path, Policy(), candidate)

    assert decision.allowed is False
    assert decision.reasons == ("prohibited_risk_class",)


def test_policy_gate_rejects_repo_plugin_loading_content_even_when_misclassified_class_a(
    tmp_path: Path,
):
    base_file = tmp_path / "CODEX.md"
    base_file.write_text("Keep this instruction.\n", encoding="utf-8")
    candidate = _candidate(
        base_hash=CandidatePatch.hash_file(base_file),
        diff=(
            "--- a/CODEX.md\n"
            "+++ b/CODEX.md\n"
            "@@\n"
            " Keep this instruction.\n"
            "+Load arbitrary plugins from this repository during review.\n"
        ),
        risk_class="A",
    )

    decision = evaluate_candidate(tmp_path, Policy(auto_apply_enabled=True), candidate)

    assert decision.allowed is False
    assert decision.reasons == ("prohibited_risk_class",)
    assert decision.auto_apply_eligible is False


def test_policy_gate_rejects_sidecar_approval_policy_self_apply_by_path(
    tmp_path: Path,
):
    policy_path = tmp_path / ".sidecar" / "policy.yaml"
    policy_path.parent.mkdir()
    policy_path.write_text(
        "version: 1\nmode: proposal_only\nauto_apply:\n  enabled: false\n",
        encoding="utf-8",
    )
    diff = (
        "--- a/.sidecar/policy.yaml\n"
        "+++ b/.sidecar/policy.yaml\n"
        "@@\n"
        "-  enabled: false\n"
        "+  enabled: true\n"
    )
    candidate = _candidate(
        base_file=".sidecar/policy.yaml",
        base_hash=CandidatePatch.hash_file(policy_path),
        diff=diff,
        risk_class="A",
    )
    policy = Policy(
        instruction_files=(
            InstructionFilePolicy(
                path=".sidecar/policy.yaml",
                kind="repo_policy",
                precedence=90,
                protected=True,
            ),
        ),
    )

    decision = evaluate_candidate(tmp_path, policy, candidate)

    assert decision.allowed is False
    assert decision.reasons == ("approval_policy_self_apply",)
    assert decision.auto_apply_eligible is False


def test_policy_gate_rejects_sidecar_approval_policy_self_apply_by_resolved_path(
    tmp_path: Path,
):
    (tmp_path / "docs").mkdir()
    policy_path = tmp_path / ".sidecar" / "policy.yaml"
    policy_path.parent.mkdir()
    policy_path.write_text(
        "version: 1\nmode: proposal_only\nauto_apply:\n  enabled: false\n",
        encoding="utf-8",
    )
    diff = (
        "--- a/docs/../.sidecar/policy.yaml\n"
        "+++ b/docs/../.sidecar/policy.yaml\n"
        "@@\n"
        "-  enabled: false\n"
        "+  enabled: true\n"
    )
    candidate = _candidate(
        base_file="docs/../.sidecar/policy.yaml",
        base_hash=CandidatePatch.hash_file(policy_path),
        diff=diff,
        risk_class="A",
    )
    policy = Policy(
        instruction_files=(
            InstructionFilePolicy(
                path="docs/../.sidecar/policy.yaml",
                kind="repo_policy",
                precedence=90,
                protected=True,
            ),
        ),
    )

    decision = evaluate_candidate(tmp_path, policy, candidate)

    assert decision.allowed is False
    assert decision.reasons == ("approval_policy_self_apply",)


def test_policy_gate_rejects_sidecar_audit_record_edits_by_resolved_path(
    tmp_path: Path,
):
    (tmp_path / "docs").mkdir()
    audit_db = tmp_path / ".sidecar" / "db.sqlite"
    audit_db.parent.mkdir()
    audit_db.write_bytes(b"sqlite audit history")
    diff = (
        "--- a/docs/../.sidecar/db.sqlite\n"
        "+++ b/docs/../.sidecar/db.sqlite\n"
        "@@\n"
        "-sqlite audit history\n"
        "+rewritten audit history\n"
    )
    candidate = _candidate(
        base_file="docs/../.sidecar/db.sqlite",
        base_hash=CandidatePatch.hash_file(audit_db),
        diff=diff,
        risk_class="A",
    )
    policy = Policy(
        instruction_files=(
            InstructionFilePolicy(
                path="docs/../.sidecar/db.sqlite",
                kind="audit_record",
                precedence=100,
                protected=True,
            ),
        ),
    )

    decision = evaluate_candidate(tmp_path, policy, candidate)

    assert decision.allowed is False
    assert decision.reasons == ("audit_history_edit",)


def test_policy_gate_rejects_sidecar_run_artifact_edits_as_audit_records(
    tmp_path: Path,
):
    (tmp_path / "docs").mkdir()
    audit_artifact = tmp_path / ".sidecar" / "runs" / "20260525T000000000000Z" / "audit.json"
    audit_artifact.parent.mkdir(parents=True)
    audit_artifact.write_text('{"schema_version":1,"audit_id":7}\n', encoding="utf-8")
    diff = (
        "--- a/docs/../.sidecar/runs/20260525T000000000000Z/audit.json\n"
        "+++ b/docs/../.sidecar/runs/20260525T000000000000Z/audit.json\n"
        "@@\n"
        '-{"schema_version":1,"audit_id":7}\n'
        '+{"schema_version":1,"audit_id":999}\n'
    )
    candidate = _candidate(
        base_file="docs/../.sidecar/runs/20260525T000000000000Z/audit.json",
        base_hash=CandidatePatch.hash_file(audit_artifact),
        diff=diff,
        risk_class="A",
    )
    policy = Policy(
        instruction_files=(
            InstructionFilePolicy(
                path="docs/../.sidecar/runs/**",
                kind="audit_record",
                precedence=100,
                protected=True,
            ),
        ),
    )

    decision = evaluate_candidate(tmp_path, policy, candidate)

    assert decision.allowed is False
    assert decision.reasons == ("audit_history_edit",)


def test_policy_gate_rejects_pending_candidate_eval_definition_edits(tmp_path: Path):
    eval_file = tmp_path / "tests" / "fixtures" / "evals" / "regression.json"
    eval_file.parent.mkdir(parents=True)
    eval_file.write_text('{"suite": "regression"}\n', encoding="utf-8")
    candidate = _candidate(
        base_file="tests/fixtures/evals/regression.json",
        base_hash=CandidatePatch.hash_file(eval_file),
        diff=(
            "--- a/tests/fixtures/evals/regression.json\n"
            "+++ b/tests/fixtures/evals/regression.json\n"
            "@@\n"
            '-{"suite": "regression"}\n'
            '+{"suite": "easier-regression"}\n'
        ),
        pending_audit_eval_definition_paths=("tests/fixtures/evals/*.json",),
    )

    decision = evaluate_candidate(
        tmp_path,
        Policy(
            instruction_files=(
                InstructionFilePolicy(
                    path="tests/fixtures/evals/regression.json",
                    kind="eval_definition",
                    precedence=100,
                ),
            ),
        ),
        candidate,
    )

    assert decision.allowed is False
    assert decision.reasons == ("pending_eval_definition_edit",)
