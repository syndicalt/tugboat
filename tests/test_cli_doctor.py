from tugboat.cli import main


def test_doctor_reports_proposal_only(capsys):
    exit_code = main(["doctor"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "tugboat: ok" in out
    assert "mode: proposal_only" in out
    assert "auto_apply: disabled" in out
