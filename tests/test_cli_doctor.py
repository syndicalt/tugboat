from tugboat.cli import main


def test_doctor_reports_proposal_only(tmp_path, capsys):
    exit_code = main(["doctor", "--repo", str(tmp_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "tugboat: ok" in out
    assert "mode: proposal_only" in out
    assert "auto_apply: disabled" in out


def test_doctor_reports_missing_policy_with_actionable_next_steps(tmp_path, capsys):
    exit_code = main(["doctor", "--repo", str(tmp_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "policy: missing" in out
    assert f"recommendation: run `tugboat init --repo {tmp_path.resolve()}`" in out
    assert f"recommendation: run `tugboat index --repo {tmp_path.resolve()} --check`" in out


def test_doctor_reports_existing_policy_posture_and_provider_warning(tmp_path, capsys):
    policy_dir = tmp_path / ".sidecar"
    policy_dir.mkdir()
    (policy_dir / "policy.yaml").write_text(
        """
version: 1
mode: proposal_only
auto_apply:
  enabled: true
llmff:
  allow_network: true
  allowed_providers:
    - openai
""".lstrip(),
        encoding="utf-8",
    )

    exit_code = main(["doctor", "--repo", str(tmp_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "policy: found" in out
    assert "mode: proposal_only" in out
    assert "auto_apply: enabled" in out
    assert "llmff_network: enabled" in out
    assert "allowed_providers: openai" in out
    assert "recommendation: review auto-apply lanes before running `tugboat auto-apply`" in out
    assert "recommendation: confirm provider manifests are reviewed and pinned" in out
