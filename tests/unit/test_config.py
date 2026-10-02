"""Settings validation and secrets handling."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from nifbot.config import CONFIG_DIR, Secrets, insecure_permissions, load_settings


def test_shipped_settings_are_safe_defaults() -> None:
    s = load_settings()
    assert s.order_placement.enabled is False
    assert s.broker.data_only is True
    assert s.risk.max_loss_per_trade_pct == 1.0
    assert s.risk.daily_loss_cap_pct == 3.0
    assert s.news.llm_scorer_enabled is False


def test_unknown_keys_and_bad_times_rejected(tmp_path: Path) -> None:
    raw = yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text())
    raw["surprise"] = 1
    bad = tmp_path / "s.yaml"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError):
        load_settings(bad)
    del raw["surprise"]
    raw["risk"]["no_new_calls_after"] = "25:99"
    bad.write_text(yaml.safe_dump(raw))
    with pytest.raises((ValidationError, ValueError)):
        load_settings(bad)


def test_non_mapping_yaml_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "s.yaml"
    bad.write_text("- a\n- b\n")
    with pytest.raises(ValueError):
        load_settings(bad)


def test_secrets_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok-value-123")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "11, 22")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "dhan-secret")
    s = Secrets(_env_file=None)
    assert s.telegram_allowed_chat_ids == [11, 22]
    assert set(s.secret_values()) == {"tok-value-123", "dhan-secret"}
    assert "tok-value-123" not in repr(s)


def test_single_chat_id_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "5")
    assert Secrets(_env_file=None).telegram_allowed_chat_ids == [5]


def test_insecure_permissions(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert not insecure_permissions(env)
    if os.name == "posix":
        env.write_text("X=1")
        env.chmod(0o644)
        assert insecure_permissions(env)
        env.chmod(0o600)
        assert not insecure_permissions(env)
