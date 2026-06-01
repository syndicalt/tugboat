from pathlib import Path

import pytest

from tugboat.config import load_policy
from tugboat.models import DEFAULT_FIXTURE_LLMFF_BINARY
from tugboat.paths import sidecar_dir


def test_load_policy_defaults_to_proposal_only(tmp_path: Path):
    policy = load_policy(tmp_path)

    assert policy.mode == "proposal_only"
    assert policy.auto_apply_enabled is False
    assert policy.auto_apply_max_changed_lines == 50
    assert policy.auto_apply_minimum_burn_in_days == 14
    assert policy.auto_apply_maximum_rejection_rate == 0.10
    assert policy.auto_apply_maximum_rollback_rate == 0.02
    assert policy.auto_apply_max_instruction_token_delta == 50
    assert [(lane.name, lane.max_changed_lines) for lane in policy.auto_apply_lanes] == [
        ("docs_hygiene", 50),
        ("skill_improvement", 30),
    ]
    assert policy.roadmap_learning_rate_max_files_touched == 2
    assert policy.roadmap_learning_rate_max_sections_touched == 4
    assert policy.roadmap_learning_rate_max_changed_lines == 20
    assert policy.roadmap_learning_rate_max_normative_changes == 2
    assert policy.roadmap_learning_rate_operator_risk_limits == {}
    assert policy.roadmap_drift_cluster_max_evidence_refs == 8
    assert policy.risk_class_changed_line_budgets == {}
    assert policy.editable_headings == ()
    assert policy.llmff_allow_network is False
    assert policy.llmff_timeout_ms == 60_000
    assert policy.llmff_retry_attempts == 0
    assert policy.llmff_retry_backoff_ms == 0
    assert policy.vcs_pull_request_enabled is False
    assert policy.vcs_pull_request_provider == ""
    assert policy.vcs_pull_request_remote == "origin"
    assert policy.vcs_pull_request_base_branch == ""
    assert policy.vcs_pull_request_draft is True
    assert policy.raw_traces_retention_days == 14
    assert policy.checkpoints_retention_days == 7
    assert policy.retention_scan_file_budget == 100_000
    assert [entry.path for entry in policy.instruction_files] == [
        "AGENTS.md",
        "CODEX.md",
        "CLAUDE.md",
        "SKILL.md",
        ".codex/skills/**/SKILL.md",
    ]


def test_load_policy_yaml_defaults_missing_llmff_binary_to_fixture_backend(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
mode: proposal_only
llmff:
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.llmff_binary == DEFAULT_FIXTURE_LLMFF_BINARY


def test_load_policy_yaml_rejects_non_mapping_payload(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text("- nope\n", encoding="utf-8")

    with pytest.raises(ValueError, match=".sidecar/policy.yaml must contain a mapping"):
        load_policy(tmp_path)


@pytest.mark.parametrize("version", ("true", "0"))
def test_load_policy_yaml_rejects_invalid_policy_version(
    tmp_path: Path,
    version: str,
):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(f"version: {version}\n", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match=".sidecar/policy.yaml version must be a positive integer",
    ):
        load_policy(tmp_path)


def test_load_policy_yaml_overrides_instruction_files(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
mode: proposal_only
instruction_files:
  - path: CODEX.md
    kind: agent_policy
    precedence: 70
    protected: true
auto_apply:
  enabled: false
  max_changed_lines: 12
  max_instruction_token_delta: 9
llmff:
  binary: llmff
  require_inspect: true
  allow_network: false
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert len(policy.instruction_files) == 1
    assert policy.instruction_files[0].path == "CODEX.md"
    assert policy.auto_apply_max_changed_lines == 12
    assert policy.auto_apply_max_instruction_token_delta == 9


def test_load_policy_yaml_reads_auto_apply_allowed_risk_classes(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
auto_apply:
  enabled: true
  allowed_risk_classes:
    - A
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.auto_apply_allowed_risk_classes == ("A",)


def test_load_policy_yaml_reads_auto_apply_lanes(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
auto_apply:
  lanes:
    docs_hygiene:
      enabled: true
      allowed_categories:
        - typo_fix
      allowed_risk_classes:
        - A
      max_changed_lines: 50
      max_instruction_token_delta: 6
      minimum_burn_in_days: 3
      maximum_rejection_rate: 0.20
      maximum_rollback_rate: 0.05
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert len(policy.auto_apply_lanes) == 1
    lane = policy.auto_apply_lanes[0]
    assert lane.name == "docs_hygiene"
    assert lane.allowed_categories == ("typo_fix",)
    assert lane.max_changed_lines == 50
    assert lane.max_instruction_token_delta == 6
    assert lane.minimum_burn_in_days == 3
    assert lane.maximum_rejection_rate == 0.20
    assert lane.maximum_rollback_rate == 0.05


def test_load_policy_yaml_reads_auto_apply_pause_controls(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
auto_apply:
  paused_repositories:
    - {tmp_path}
  paused_lanes:
    - docs_hygiene
  paused_categories:
    - typo-fix
  pause_for_incident: true
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.auto_apply_paused_repositories == (str(tmp_path.resolve()),)
    assert policy.auto_apply_paused_lanes == ("docs_hygiene",)
    assert policy.auto_apply_paused_categories == ("typo-fix",)
    assert policy.auto_apply_pause_for_incident is True


def test_load_policy_yaml_reads_drift_cluster_limit(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
roadmap:
  drift_cluster:
    max_evidence_refs: 3
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.roadmap_drift_cluster_max_evidence_refs == 3


def test_load_policy_yaml_rejects_zero_drift_cluster_limit(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
roadmap:
  drift_cluster:
    max_evidence_refs: 0
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="roadmap.drift_cluster.max_evidence_refs"):
        load_policy(tmp_path)


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    (
        ("paused_repositories", "repo"),
        ("paused_lanes", "docs_hygiene"),
        ("paused_categories", "typo_fix"),
    ),
)
def test_load_policy_yaml_rejects_malformed_auto_apply_pause_lists(
    tmp_path: Path,
    field_name: str,
    field_value: str,
):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
auto_apply:
  {field_name}: {field_value}
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=f"auto_apply.{field_name} must be a list"):
        load_policy(tmp_path)


def test_load_policy_yaml_rejects_negative_auto_apply_token_growth_limit(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
auto_apply:
  max_instruction_token_delta: -1
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="auto_apply.max_instruction_token_delta"):
        load_policy(tmp_path)


def test_load_policy_yaml_rejects_negative_lane_token_growth_limit(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
auto_apply:
  lanes:
    docs_hygiene:
      max_instruction_token_delta: -1
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="auto_apply.lanes.docs_hygiene.max_instruction_token_delta"):
        load_policy(tmp_path)


def test_load_policy_yaml_reads_allowed_manifest_hashes(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
allowed_manifest_hashes:
  - abc123
  - def456
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.allowed_manifest_hashes == ("abc123", "def456")


def test_load_policy_yaml_reads_learning_rate_budget(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
roadmap:
  learning_rate_budget:
    max_files_touched: 1
    max_sections_touched: 2
    max_changed_lines: 4
    max_normative_changes: 1
    operator_risk_limits:
      delete: 0
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.roadmap_learning_rate_max_files_touched == 1
    assert policy.roadmap_learning_rate_max_sections_touched == 2
    assert policy.roadmap_learning_rate_max_changed_lines == 4
    assert policy.roadmap_learning_rate_max_normative_changes == 1
    assert policy.roadmap_learning_rate_operator_risk_limits == {"delete": 0}


def test_load_policy_yaml_reads_risk_class_changed_line_budgets(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
risk_class_changed_line_budgets:
  A: 1
  B: 3
  class_c: 5
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.risk_class_changed_line_budgets == {"A": 1, "B": 3, "class_c": 5}


def test_load_policy_yaml_reads_editable_headings(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
editable_headings:
  - Operating Constraints / Local Fixtures
  - Examples
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.editable_headings == (
        "Operating Constraints / Local Fixtures",
        "Examples",
    )


def test_load_policy_yaml_rejects_non_list_editable_headings(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
editable_headings: Operating Constraints
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="editable_headings"):
        load_policy(tmp_path)


def test_load_policy_yaml_rejects_non_string_editable_heading_items(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
editable_headings:
  - 123
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="editable_headings"):
        load_policy(tmp_path)


def test_load_policy_yaml_reads_llmff_allowed_manifest_hashes(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
llmff:
  allowed_manifest_hashes:
    - abc123
    - def456
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.allowed_manifest_hashes == ("abc123", "def456")


def test_load_policy_yaml_reads_llmff_allowed_providers(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
llmff:
  allowed_providers:
    - openai
    - anthropic
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.llmff_allowed_providers == ("openai", "anthropic")


def test_load_policy_yaml_reads_pull_request_config(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
vcs:
  pull_request:
    enabled: true
    provider: github_cli
    remote: upstream
    base_branch: trunk
    draft: false
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.vcs_pull_request_enabled is True
    assert policy.vcs_pull_request_provider == "github_cli"
    assert policy.vcs_pull_request_remote == "upstream"
    assert policy.vcs_pull_request_base_branch == "trunk"
    assert policy.vcs_pull_request_draft is False


def test_load_policy_yaml_rejects_malformed_llmff_allowed_providers(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
llmff:
  allowed_providers:
    - openai
    - 123
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="llmff.allowed_providers"):
        load_policy(tmp_path)


def test_load_policy_yaml_reads_llmff_runtime_knobs(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
llmff:
  timeout_ms: 12345
  retry_attempts: 2
  retry_backoff_ms: 250
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.llmff_timeout_ms == 12345
    assert policy.llmff_retry_attempts == 2
    assert policy.llmff_retry_backoff_ms == 250


def test_load_policy_yaml_rejects_negative_llmff_runtime_knobs(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
llmff:
  timeout_ms: -1
  retry_attempts: 0
  retry_backoff_ms: 0
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="llmff.timeout_ms"):
        load_policy(tmp_path)


def test_load_policy_yaml_rejects_zero_llmff_timeout(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
llmff:
  timeout_ms: 0
  retry_attempts: 0
  retry_backoff_ms: 0
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="llmff.timeout_ms"):
        load_policy(tmp_path)


@pytest.mark.parametrize("raw_value", ["true", "1.5"])
def test_load_policy_yaml_rejects_non_integer_llmff_runtime_knobs(
    tmp_path: Path,
    raw_value: str,
):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
llmff:
  timeout_ms: {raw_value}
  retry_attempts: 0
  retry_backoff_ms: 0
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="llmff.timeout_ms"):
        load_policy(tmp_path)


def test_load_policy_yaml_reads_retention_policy(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
retention:
  raw_traces_days: 30
  checkpoints_days: 10
  max_scan_files: 5000
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.raw_traces_retention_days == 30
    assert policy.checkpoints_retention_days == 10
    assert policy.retention_scan_file_budget == 5000


def test_load_policy_yaml_rejects_negative_retention_days(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
retention:
  raw_traces_days: -1
  checkpoints_days: 7
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="retention.raw_traces_days"):
        load_policy(tmp_path)


@pytest.mark.parametrize("value", ["0", "-1", '"many"'])
def test_load_policy_yaml_rejects_invalid_retention_scan_budget(
    tmp_path: Path,
    value: str,
):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        f"""
version: 1
retention:
  max_scan_files: {value}
""".lstrip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="retention.max_scan_files"):
        load_policy(tmp_path)


def test_load_policy_yaml_reads_mcp_allowlist_and_tool_policy(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
mcp:
  allowed_repositories:
    - /workspace/allowed
  tool_policy:
    tugboat_status: allow
    tugboat_request_audit: deny
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.mcp_allowed_repositories == ("/workspace/allowed",)
    assert policy.mcp_tool_policy == {
        "tugboat_status": "allow",
        "tugboat_request_audit": "deny",
    }


def test_sidecar_dir_is_repo_local(tmp_path: Path):
    assert sidecar_dir(tmp_path) == tmp_path / ".sidecar"
