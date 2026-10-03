"""Build a live Dhan adapter from settings + secrets."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from pydantic import SecretStr

from nifbot.config import PROJECT_ROOT, Secrets, Settings
from nifbot.data.adapter import DataError
from nifbot.data.dhan import DhanAdapter, DhanClient, RawHook
from nifbot.data.dhan_auth import TokenManager, TokenStore
from nifbot.data.scrip_master import current_future, download_scrip_master, nifty_futures
from nifbot.timeutil import now_ist

TOKEN_FILE = PROJECT_ROOT / "data" / "secrets" / "dhan_token.json"


def _plain(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def token_manager(
    settings: Settings, secrets: Secrets, store_path: Path | None = None
) -> TokenManager:
    cfg = settings.broker.dhan
    return TokenManager(
        _plain(secrets.dhan_client_id) or "",
        TokenStore(store_path or TOKEN_FILE),
        _plain(secrets.dhan_access_token),
        renew_before=timedelta(hours=cfg.renew_before_hours),
        pin=_plain(secrets.dhan_pin),
        totp_secret=_plain(secrets.dhan_totp_secret),
        auto_login_totp=cfg.auto_login_totp,
    )


def dhan_client(settings: Settings, secrets: Secrets, on_raw: RawHook | None = None) -> DhanClient:
    if secrets.dhan_client_id is None:
        raise DataError("DHAN_CLIENT_ID is not set in .env")
    manager = token_manager(settings, secrets)
    try:
        token = manager.ensure_valid()
    finally:
        manager.close()
    return DhanClient(
        secrets.dhan_client_id.get_secret_value(), token, settings.broker.dhan, on_raw=on_raw
    )


def resolve_futures_id(settings: Settings, cache_dir: Path) -> tuple[str, str]:
    """(security_id, description) of the current-month Nifty future."""
    cfg = settings.broker.dhan
    if cfg.futures_security_id:
        return cfg.futures_security_id, "from settings.yaml"
    cache_dir.mkdir(parents=True, exist_ok=True)
    today = now_ist().date()
    cache = cache_dir / f"scrip_master_{today.isoformat()}.csv"
    if not cache.exists():
        cache.write_text(download_scrip_master(cfg.scrip_master_url), encoding="utf-8")
    fut = current_future(nifty_futures(cache.read_text(encoding="utf-8")), today)
    return fut.security_id, f"{fut.symbol} exp {fut.expiry} lot {fut.lot_size}"


def dhan_adapter(
    settings: Settings, secrets: Secrets, data_dir: Path, on_raw: RawHook | None = None
) -> tuple[DhanAdapter, str]:
    fut_id, desc = resolve_futures_id(settings, data_dir / "reference")
    client = dhan_client(settings, secrets, on_raw)
    return DhanAdapter(client, settings.broker.dhan, fut_id), desc
