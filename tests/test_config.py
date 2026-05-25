from pathlib import Path

import pytest

from tugboat.config import load_policy
from tugboat.paths import sidecar_dir


def test_load_policy_defaults_to_proposal_only(tmp_path: Path):
    policy = load_policy(tmp_path)

    assert policy.mode == "proposal_only"
    assert policy.auto_apply_enabled is False
    assert policy.llmff_allow_network is False
    assert policy.raw_traces_retention_days == 14
    assert policy.checkpoints_retention_days == 7
    assert [entry.path for entry in policy.instruction_files] == [
        "AGENTS.md",
        "CODEX.md",
        "CLAUDE.md",
        "SKILL.md",
        ".codex/skills/**/SKILL.md",
    ]


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


def test_load_policy_yaml_reads_retention_policy(tmp_path: Path):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
retention:
  raw_traces_days: 30
  checkpoints_days: 10
""".lstrip(),
        encoding="utf-8",
    )

    policy = load_policy(tmp_path)

    assert policy.raw_traces_retention_days == 30
    assert policy.checkpoints_retention_days == 10


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
