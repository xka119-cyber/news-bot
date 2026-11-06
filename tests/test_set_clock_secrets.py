"""scripts/set_clock_secrets.py 검사: 값은 wrangler의 표준 입력으로만 가고, 화면에는 이름과 ****만 나와야 한다."""

import subprocess

import pytest

from scripts import set_clock_secrets as clock_secrets

FAKE_TELEGRAM = "123456789" + ":" + "b" * 35


def forbid_wrangler(monkeypatch, why):
    monkeypatch.setattr(clock_secrets, "wrangler", lambda *a, **k: pytest.fail(why))


def test_mask_hides_every_known_value():
    assert clock_secrets.mask("a=SECRET1 b=SECRET2", ["SECRET1", "SECRET2", ""]) == "a=**** b=****"


def test_check_mode_reports_names_only_and_never_calls_wrangler(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TELEGRAM}\nTELEGRAM_CHAT_ID=4242\n", encoding="utf-8")
    monkeypatch.setattr(clock_secrets, "ROOT", tmp_path)
    forbid_wrangler(monkeypatch, "--check는 wrangler를 부르면 안 된다 (진짜 Cloudflare 비밀값을 덮어쓸 수 있음)")
    assert clock_secrets.main(["--check"]) == 1          # GH_DISPATCH_TOKEN이 없으니 1
    out = capsys.readouterr().out
    assert "❌ .env GH_DISPATCH_TOKEN: 없음" in out
    assert "✅ .env TELEGRAM_BOT_TOKEN: 있음" in out and "✅ .env TELEGRAM_CHAT_ID: 있음" in out
    assert FAKE_TELEGRAM not in out and "4242" not in out


def test_unknown_option_stops_before_touching_anything(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("GH_DISPATCH_TOKEN=tok-secret-value\n", encoding="utf-8")
    monkeypatch.setattr(clock_secrets, "ROOT", tmp_path)
    forbid_wrangler(monkeypatch, "잘못 쓴 옵션으로는 저장이 실행되면 안 된다")
    with pytest.raises(SystemExit) as stop:
        clock_secrets.main(["--chek"])
    assert stop.value.code == 2
    assert "tok-secret-value" not in capsys.readouterr().err


def test_put_masks_value_even_if_wrangler_echoes_it(monkeypatch, capsys):
    def fake_wrangler(args, value=None):
        return subprocess.CompletedProcess(args, 0, stdout=f"Success! Uploaded secret {args[2]} ({value})\n", stderr="")

    monkeypatch.setattr(clock_secrets, "wrangler", fake_wrangler)
    assert clock_secrets.put("GH_DISPATCH_TOKEN", "tok-secret-value", ["tok-secret-value"]) is True
    out = capsys.readouterr().out
    assert "GH_DISPATCH_TOKEN" in out and "****" in out and "tok-secret-value" not in out


def test_put_failure_is_reported_without_the_value(monkeypatch, capsys):
    def fake_wrangler(args, value=None):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=f"✘ [ERROR] Not logged in. value was {value}\n")

    monkeypatch.setattr(clock_secrets, "wrangler", fake_wrangler)
    assert clock_secrets.put("TELEGRAM_BOT_TOKEN", "bot-secret", ["bot-secret"]) is False
    out = capsys.readouterr().out
    assert "❌ TELEGRAM_BOT_TOKEN: 저장 실패" in out and "bot-secret" not in out and "****" in out


def test_main_sends_values_only_through_stdin_and_lists_names(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "GH_DISPATCH_TOKEN=tok-secret-value\nTELEGRAM_BOT_TOKEN=bot-secret\nTELEGRAM_CHAT_ID=4242\n", encoding="utf-8"
    )
    monkeypatch.setattr(clock_secrets, "ROOT", tmp_path)
    calls = []

    def fake_wrangler(args, value=None):
        calls.append((tuple(args), value))
        if args[:2] == ["secret", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout='[{"name":"GH_DISPATCH_TOKEN","type":"secret_text"}]', stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="Success!", stderr="")

    monkeypatch.setattr(clock_secrets, "wrangler", fake_wrangler)
    assert clock_secrets.main([]) == 0
    assert [c[0] for c in calls] == [
        ("secret", "put", "GH_DISPATCH_TOKEN"),
        ("secret", "put", "TELEGRAM_BOT_TOKEN"),
        ("secret", "put", "TELEGRAM_CHAT_ID"),
        ("secret", "list"),
    ]
    assert calls[0][1] == "tok-secret-value" and calls[3][1] is None   # 값은 표준 입력으로만, list에는 값 없음
    out = capsys.readouterr().out
    assert "tok-secret-value" not in out and "bot-secret" not in out and "4242" not in out
    assert "GH_DISPATCH_TOKEN" in out


def test_main_returns_1_when_a_put_fails(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "GH_DISPATCH_TOKEN=tok-secret-value\nTELEGRAM_BOT_TOKEN=bot-secret\nTELEGRAM_CHAT_ID=4242\n", encoding="utf-8"
    )
    monkeypatch.setattr(clock_secrets, "ROOT", tmp_path)

    def fake_wrangler(args, value=None):
        if args[:2] == ["secret", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
        failed = args[2] == "TELEGRAM_CHAT_ID"
        return subprocess.CompletedProcess(args, 1 if failed else 0, stdout="" if failed else "ok", stderr="boom" if failed else "")

    monkeypatch.setattr(clock_secrets, "wrangler", fake_wrangler)
    assert clock_secrets.main([]) == 1
    out = capsys.readouterr().out
    assert "❌ TELEGRAM_CHAT_ID: 저장 실패" in out and "tok-secret-value" not in out


def test_main_without_token_sets_the_rest_but_returns_1(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=bot-secret\nTELEGRAM_CHAT_ID=4242\n", encoding="utf-8")
    monkeypatch.setattr(clock_secrets, "ROOT", tmp_path)
    puts = []

    def fake_wrangler(args, value=None):
        if args[1] == "put":
            puts.append(args[2])
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")

    monkeypatch.setattr(clock_secrets, "wrangler", fake_wrangler)
    assert clock_secrets.main([]) == 1
    assert puts == ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"]
    assert "GH_DISPATCH_TOKEN이 .env에 없어" in capsys.readouterr().out
