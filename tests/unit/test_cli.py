"""CLI smoke tests (no network)."""

from __future__ import annotations

import pytest

from nifbot import cli


def test_calendar_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["calendar", "2026-10-02"]) == 0
    assert "closed (Mahatma Gandhi Jayanti)" in capsys.readouterr().out
    assert cli.main(["calendar", "2026-10-05"]) == 0
    assert "TRADING DAY" in capsys.readouterr().out
    assert cli.main(["calendar", "2031-01-06"]) == 1


def test_check_never_prints_secret(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token-value")
    cli.main(["check"])
    out = capsys.readouterr().out
    assert "super-secret-token-value" not in out
    assert "telegram_bot_token: set" in out
    assert "order placement enabled: False" in out


def test_tg_commands_fail_cleanly_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.setattr(cli, "ENV_FILE", cli.ENV_FILE.parent / "does-not-exist.env")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert cli.main(["tg-whoami"]) == 1
    assert cli.main(["tg-test"]) == 1
