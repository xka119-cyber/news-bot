import pytest

from newsbot import __main__ as cli

ENV_NAMES = ["ANTHROPIC_API_KEY", "IG_ACCESS_TOKEN", "IG_USER_ID", "TELEGRAM_BOT_TOKEN"]


@pytest.fixture(autouse=True)
def no_real_env(monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda *args, **kwargs: None)
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_help_works(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    assert "generate" in capsys.readouterr().out


def test_check_reports_empty_values():
    assert cli.main(["check"]) == 1


def test_check_never_prints_values(monkeypatch, capsys):
    for name in ENV_NAMES:
        monkeypatch.setenv(name, f"secret-value-{name}")
    assert cli.main(["check"]) == 0
    assert "secret-value" not in capsys.readouterr().out
