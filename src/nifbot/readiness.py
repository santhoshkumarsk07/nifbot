"""`nifbot data-check`: is everything needed for backtesting / training present?

Each check reports OK / WARN / MISSING / INFO plus the command that fixes it.
Nothing here fetches data; it only inspects what is on this machine.
"""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

import pandas as pd

from nifbot.config import CONFIG_DIR, Secrets, load_yaml
from nifbot.data.dhan_auth import TokenStore, token_expiry
from nifbot.flows.store import FlowStore
from nifbot.timeutil import now_ist
from nifbot.trading_calendar import CalendarError, TradingCalendar

Status = Literal["OK", "WARN", "MISSING", "INFO"]
MIN_YEARS = 2.0  # minimum history for walk-forward training to be meaningful


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


def _trading_days(cal: TradingCalendar, start: date, end: date) -> set[date]:
    out: set[date] = set()
    d = start
    while d <= end:
        try:
            if cal.is_trading_day(d):
                out.add(d)
        except CalendarError:
            pass
        d += timedelta(days=1)
    return out


def _load_dates(files: list[Path]) -> set[date]:
    days: set[date] = set()
    for f in files:
        col = pd.read_parquet(f, columns=["available_at"])["available_at"]
        days |= set(pd.to_datetime(col).dt.date)
    return days


def check_history(
    hist_dir: Path, cal: TradingCalendar, today: date
) -> tuple[list[Check], set[date]]:
    checks: list[Check] = []
    spot_files = sorted(hist_dir.glob("spot_*.parquet"))
    fix = "make prepare-data   (or: uv run nifbot fetch-history --options)"
    if not spot_files:
        checks.append(Check("Nifty 1-min history", "MISSING", "no spot files", fix))
        return checks, set()
    days = _load_dates(spot_files)
    first, last = min(days), max(days)
    years = (last - first).days / 365.25
    expected = _trading_days(cal, first, last)
    gaps = sorted(expected - days)
    status: Status = "OK" if years >= MIN_YEARS and len(gaps) <= len(expected) * 0.02 else "WARN"
    detail = (
        f"{first} -> {last} ({years:.1f} y, {len(days)} days, {len(gaps)} trading days missing)"
    )
    if last < today - timedelta(days=7):
        status, detail = "WARN", detail + "; older than a week"
    checks.append(Check("Nifty 1-min history", status, detail, fix if status != "OK" else ""))

    vix_files = sorted(hist_dir.glob("vix_*.parquet"))
    vdays = _load_dates(vix_files) if vix_files else set()
    checks.append(
        Check(
            "India VIX 1-min history",
            "OK" if len(vdays) >= 0.95 * len(days) else ("WARN" if vdays else "MISSING"),
            f"{len(vdays)} days" + ("" if vdays else "; check vix_security_id in settings.yaml"),
            "" if vdays else fix,
        )
    )

    opt_files = sorted(hist_dir.glob("options_*.parquet"))
    if not opt_files:
        checks.append(Check("Expired options history", "MISSING", "no option files", fix))
    else:
        odays = _load_dates(opt_files)
        rel = {f.name.split("_")[2] for f in opt_files if f.name.count("_") >= 3}
        share = len(odays & days) / len(days) if days else 0.0
        checks.append(
            Check(
                "Expired options history",
                "OK" if share >= 0.9 and len(rel) >= 11 else "WARN",
                f"{len(opt_files)} files, {len(rel)} relative strikes, covers {share:.0%} of days",
                "" if share >= 0.9 else fix,
            )
        )
    checks.append(
        Check(
            "Futures 1-min history",
            "INFO",
            "not available from Dhan's free history endpoints for expired contracts; futures "
            "features (basis, VWAP, build-up) are live-only and empty in training",
        )
    )
    return checks, days


def check_features(path: Path) -> Check:
    if not path.exists():
        return Check(
            "Training feature table", "MISSING", str(path), "uv run nifbot features-history"
        )
    feats = pd.read_parquet(path)
    filled = feats.notna().mean()
    empty = [c for c, v in filled.items() if v < 0.5]
    idx = pd.DatetimeIndex(feats.index)
    detail = f"{len(feats):,} rows, {idx.min():%Y-%m-%d} -> {idx.max():%Y-%m-%d}"
    if empty:
        detail += f"; mostly empty: {', '.join(map(str, empty))}"
    return Check("Training feature table", "OK", detail)


def check_flows(conn: sqlite3.Connection, hist_days: set[date]) -> list[Check]:
    store = FlowStore(conn)
    pdays = store.participant_days()
    need = len(hist_days) or 1
    share = len(pdays & hist_days) / need if hist_days else 0.0
    out = [
        Check(
            "FII participant OI history",
            "OK" if share >= 0.9 else ("WARN" if pdays else "MISSING"),
            f"{len(pdays)} days stored, covers {share:.0%} of history days",
            "" if share >= 0.9 else "uv run nifbot flows-backfill",
        )
    ]
    cdays = store.cash_days()
    out.append(
        Check(
            "FII/DII cash flows",
            "INFO" if not cdays else "OK",
            f"{len(cdays)} days (optional; connector off by default, see README)",
        )
    )
    n_news = conn.execute("SELECT COUNT(*), MIN(fetched_at) FROM news").fetchone()
    since = str(n_news[1])[:10] if n_news[1] else "-"
    out.append(
        Check(
            "News archive",
            "INFO",
            f"{n_news[0]} items since {since}; free sources have "
            "no back-history, so news_score is only usable from this date on",
            "run `make news-watch` every trading day to build it up",
        )
    )
    return out


def check_config(cal: TradingCalendar, first: date | None, today: date) -> list[Check]:
    years = range((first or today - timedelta(days=3 * 365)).year, today.year + 1)
    missing = [y for y in years if y not in cal.years]
    unverified = [y for y in years if y in cal.years and not cal.years[y].verified]
    out = [
        Check(
            "NSE holiday lists",
            "MISSING" if missing else ("WARN" if unverified else "OK"),
            (f"missing years {missing}; " if missing else "")
            + (f"unverified years {unverified}" if unverified else "all verified"),
            "add/verify years in config/holidays.yaml against NSE circulars",
        )
    ]
    contracts = load_yaml(CONFIG_DIR / "contracts.yaml")
    out.append(
        Check(
            "Expiry weekday rules",
            "OK" if contracts.get("verified") else "WARN",
            "config/contracts.yaml " + ("verified" if contracts.get("verified") else "unverified"),
            "check Thu->Tue change date against NSE circular, then set verified: true",
        )
    )
    charges = CONFIG_DIR / "charges.yaml"
    out.append(
        Check(
            "Lot sizes and charges by date",
            "OK" if charges.exists() else "INFO",
            "config/charges.yaml" + (" present" if charges.exists() else " arrives in milestone 5"),
        )
    )
    return out


def check_environment(
    secrets: Secrets, data_root: Path, recorded_dir: Path, token_file: Path | None = None
) -> list[Check]:
    store = TokenStore(token_file) if token_file else None
    tok = (store.load() if store else None) or (
        secrets.dhan_access_token.get_secret_value() if secrets.dhan_access_token else None
    )
    exp = token_expiry(tok) if tok else None
    if not secrets.dhan_client_id or not tok:
        status: Status = "MISSING"
        detail = "DHAN_CLIENT_ID or access token not set"
    elif exp is not None and exp <= now_ist():
        status, detail = "MISSING", f"token expired {exp:%d %b %H:%M}"
    else:
        status = "OK"
        detail = f"token valid until {exp:%d %b %H:%M}" if exp else "token set"
    out = [Check("Dhan credentials", status, detail, "uv run nifbot dhan-login")]
    days = (
        sorted(p.name for p in recorded_dir.iterdir() if p.is_dir())
        if recorded_dir.exists()
        else []
    )
    out.append(
        Check(
            "Recorded live sessions",
            "OK" if days else "WARN",
            f"{len(days)} days" + (f" (latest {days[-1]})" if days else ""),
            "" if days else "run `make record` on a trading day (needed for replay tests)",
        )
    )
    finbert = importlib.util.find_spec("transformers") is not None
    out.append(
        Check(
            "FinBERT sentiment",
            "OK" if finbert else "INFO",
            "installed" if finbert else "not installed; word-list fallback in use",
            "" if finbert else "uv sync --extra nlp",
        )
    )
    probe = data_root if data_root.exists() else data_root.parent
    free_gb = shutil.disk_usage(probe).free / 1e9
    out.append(Check("Free disk space", "OK" if free_gb >= 5 else "WARN", f"{free_gb:.0f} GB free"))
    return out


def training_ready(checks: list[Check]) -> bool:
    must = {
        "Nifty 1-min history",
        "Expired options history",
        "Training feature table",
        "NSE holiday lists",
    }
    return all(c.status in ("OK", "WARN") for c in checks if c.name in must)
