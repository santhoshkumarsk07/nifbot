"""CLI smoke tests (no network)."""

from __future__ import annotations

from pathlib import Path

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


def test_record_once_and_compact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    from nifbot.config import load_settings
    from nifbot.data.dhan import DhanAdapter, DhanClient
    from tests.dhan_fakes import FUT_ID, FakeDhan

    cfg = load_settings().broker.dhan
    fake = FakeDhan()

    def fake_adapter(
        settings: object, secrets: object, data_dir: Path, on_raw: object = None
    ) -> tuple[DhanAdapter, str]:
        client = DhanClient(
            "1", "tok-tests", cfg, transport=httpx.MockTransport(fake.handler), sleep=lambda s: None
        )
        return DhanAdapter(client, cfg, FUT_ID), "NIFTY-TEST-FUT"

    monkeypatch.setattr(cli, "dhan_adapter", fake_adapter)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    assert cli.main(["record-once"]) == 0
    out = capsys.readouterr().out
    assert "spot=25000.00" in out and "rows=18" in out
    day = next(p.name for p in (tmp_path / "data" / "recorded").iterdir())
    assert cli.main(["compact", day]) == 0
    assert "chain.parquet" in capsys.readouterr().out
    assert cli.main(["compact", "2020-01-01"]) == 1


def test_dhan_commands_fail_cleanly_without_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "ENV_FILE", cli.ENV_FILE.parent / "does-not-exist.env")
    monkeypatch.delenv("DHAN_CLIENT_ID", raising=False)
    monkeypatch.delenv("DHAN_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(cli, "resolve_futures_id", lambda *a: ("1", "x"), raising=False)
    from nifbot.data import factory

    monkeypatch.setattr(factory, "resolve_futures_id", lambda *a: ("1", "x"))
    assert cli.main(["record-once"]) == 1
    assert cli.main(["record"]) == 1
    assert cli.main(["fetch-history", "--years", "1"]) == 1
