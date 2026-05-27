from __future__ import annotations

import io
import json
from pathlib import Path

import yaml

from tugboat.cli import main
from tugboat.models import DEFAULT_FIXTURE_LLMFF_BINARY
from tugboat.mcp import run_stdio_server


def test_init_bootstraps_proposal_only_policy_and_sidecar_gitignore(
    tmp_path: Path,
    capsys,
):
    (tmp_path / "AGENTS.md").write_text("# Agent Map\n", encoding="utf-8")

    assert main(["init", "--repo", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "initialized: .sidecar/policy.yaml" in output
    policy_path = tmp_path / ".sidecar" / "policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert policy == {
        "version": 1,
        "mode": "proposal_only",
        "instruction_files": [
            {"path": "AGENTS.md", "kind": "repo_policy", "precedence": 80, "protected": True},
            {"path": "CODEX.md", "kind": "agent_policy", "precedence": 70, "protected": True},
            {"path": "CLAUDE.md", "kind": "agent_policy", "precedence": 70, "protected": True},
            {"path": "SKILL.md", "kind": "skill", "precedence": 60, "protected": False},
            {
                "path": ".codex/skills/**/SKILL.md",
                "kind": "skill",
                "precedence": 60,
                "protected": False,
            },
        ],
        "auto_apply": {
            "enabled": False,
            "max_changed_lines": 30,
            "minimum_burn_in_days": 14,
            "maximum_rejection_rate": 0.10,
            "maximum_rollback_rate": 0.02,
        },
        "llmff": {
            "binary": DEFAULT_FIXTURE_LLMFF_BINARY,
            "require_inspect": True,
            "allow_network": False,
        },
        "mcp": {"allowed_repositories": [str(tmp_path.resolve())]},
    }
    assert (tmp_path / ".sidecar" / ".gitignore").read_text(encoding="utf-8") == (
        "*\n"
        "!.gitignore\n"
        "!policy.yaml\n"
        "!manifests/\n"
        "!manifests/**\n"
    )


def test_init_policy_supports_bound_read_only_mcp_status(tmp_path: Path):
    assert main(["init", "--repo", str(tmp_path)]) == 0

    output = io.StringIO()
    assert (
        run_stdio_server(
            io.StringIO(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "tugboat_status", "arguments": {}},
                    }
                )
                + "\n"
            ),
            output,
            repo=tmp_path,
            read_only=True,
        )
        == 0
    )

    response = json.loads(output.getvalue())
    assert response["result"]["content"][0]["json"]["mode"] == "proposal_only"


def test_init_refuses_to_overwrite_existing_policy(tmp_path: Path, capsys):
    sidecar = tmp_path / ".sidecar"
    sidecar.mkdir()
    policy_path = sidecar / "policy.yaml"
    policy_path.write_text("version: 99\nmode: custom\n", encoding="utf-8")

    assert main(["init", "--repo", str(tmp_path)]) == 1

    assert "init blocked: .sidecar/policy.yaml already exists" in capsys.readouterr().out
    assert policy_path.read_text(encoding="utf-8") == "version: 99\nmode: custom\n"
    assert not (sidecar / ".gitignore").exists()
