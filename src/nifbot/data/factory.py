"""Build a live Dhan adapter from settings + secrets."""

from __future__ import annotations

from pathlib import Path

from nifbot.config import Secrets, Settings
from nifbot.data.adapter import DataError
from nifbot.data.dhan import DhanAdapter, DhanClient, RawHook
from nifbot.data.scrip_master import current_future, download_scrip_master, nifty_futures
from nifbot.timeutil import now_ist


def dhan_client(settings: Settings, secrets: Secrets, on_raw: RawHook | None = None) -> DhanClient:
    if secrets.dhan_client_id is None or secrets.dhan_access_token is None:
        raise DataError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN are not set in .env")
    return DhanClient(
        secrets.dhan_client_id.get_secret_value(),
        secrets.dhan_access_token.get_secret_value(),
        settings.broker.dhan,
        on_raw=on_raw,
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
