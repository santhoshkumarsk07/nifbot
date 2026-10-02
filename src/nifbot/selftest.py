"""`nifbot selftest`: try every live connection once and print PASS/FAIL.

Small requests only. Test downloads go to data/selftest/, never mixed with real history.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from nifbot import DISCLAIMER
from nifbot.config import Secrets, Settings
from nifbot.data.factory import dhan_adapter, dhan_client
from nifbot.data.history import DhanHistory, RollingSpec
from nifbot.data.recorder import Recorder
from nifbot.flows import participant_oi
from nifbot.flows.global_cues import URL as FRED_URL
from nifbot.flows.global_cues import latest_cue
from nifbot.net import Fetcher
from nifbot.news.rss import parse_feed
from nifbot.news.sources import NewsConfig
from nifbot.notify.telegram import TelegramBot
from nifbot.timeutil import now_ist
from nifbot.trading_calendar import TradingCalendar


@dataclass(frozen=True)
class Result:
    name: str
    ok: bool
    detail: str


def _run(name: str, fn: Callable[[], str]) -> Result:
    try:
        return Result(name, True, fn())
    except Exception as exc:
        return Result(name, False, f"{type(exc).__name__}: {str(exc)[:200]}")


def run_selftest(
    settings: Settings,
    secrets: Secrets,
    news_cfg: NewsConfig,
    cal: TradingCalendar,
    work_dir: Path,
    fetcher: Fetcher,
    telegram: bool = True,
) -> list[Result]:
    results: list[Result] = []
    today = now_ist().date()

    def calendar() -> str:
        return f"today {today} trading day: {cal.is_trading_day(today)}"

    results.append(_run("calendar", calendar))

    if telegram:

        def tg() -> str:
            token = (
                secrets.telegram_bot_token.get_secret_value() if secrets.telegram_bot_token else ""
            )
            bot = TelegramBot(token, secrets.telegram_allowed_chat_ids)
            sent = bot.broadcast(f"nifbot selftest {now_ist():%H:%M} IST\n\n{DISCLAIMER}")
            bot.close()
            return f"sent to {len(sent)} chat(s)"

        results.append(_run("telegram", tg))

    def dhan_live() -> str:
        adapter, fut = dhan_adapter(settings, secrets, work_dir)
        snap = Recorder(adapter, work_dir / "recorded").snapshot()
        if snap.errors:
            raise RuntimeError("; ".join(snap.errors))
        if not (snap.spot and snap.vix and snap.futures and snap.chain):
            raise RuntimeError("incomplete snapshot")
        return (
            f"spot {snap.spot.ltp:,.2f}, VIX {snap.vix.ltp:.2f}, {fut} at "
            f"{snap.futures.ltp:,.2f} OI {snap.futures.oi:,}, chain {snap.chain.expiry} "
            f"{len(snap.chain.rows)} rows. COMPARE with your Dhan terminal."
        )

    results.append(_run("dhan live data", dhan_live))

    def dhan_history() -> str:
        hist = DhanHistory(dhan_client(settings, secrets), work_dir / "history")
        end = today - timedelta(days=1)
        start = end - timedelta(days=7)
        spot = hist.intraday(
            settings.broker.dhan.nifty_security_id, "IDX_I", "INDEX", start, end, oi=False
        )
        opt = hist.rolling_option(
            settings.broker.dhan.nifty_security_id,
            RollingSpec("WEEK", 1, "ATM", "CALL"),
            start,
            end,
        )
        if spot.empty:
            raise RuntimeError("no spot candles returned")
        if opt.empty:
            raise RuntimeError(f"spot OK ({len(spot)} rows) but expired-options returned nothing")
        cols = sorted(set(opt.columns) & {"oi", "iv", "spot", "strike", "volume"})
        return f"spot {len(spot)} rows; ATM call {len(opt)} rows with {', '.join(cols)}"

    results.append(_run("dhan history", dhan_history))

    def nse_flows() -> str:
        d = today - timedelta(days=1)
        for _ in range(10):
            if cal.is_trading_day(d):
                break
            d -= timedelta(days=1)
        fii = participant_oi.fetch(fetcher, d)["FII"]
        return f"{d}: FII index futures long {fii.fut_long_ratio:.0%}"

    results.append(_run("NSE participant OI", nse_flows))

    def fred() -> str:
        text = fetcher.get(FRED_URL.format(series="SP500")).decode("utf-8", errors="replace")
        cue = latest_cue("S&P 500", "SP500", "pct", text)
        if cue is None:
            raise RuntimeError("no observations")
        return cue.render(today)

    results.append(_run("FRED global cues", fred))

    def news() -> str:
        ok, bad = 0, []
        for src in [s for s in news_cfg.sources if s.enabled][:4]:
            try:
                ok += len(parse_feed(fetcher.get(src.url)))
            except Exception as exc:
                bad.append(f"{src.id} ({type(exc).__name__})")
        if not ok:
            raise RuntimeError("no headlines from first 4 sources: " + ", ".join(bad))
        return f"{ok} headlines" + (f"; failed: {', '.join(bad)}" if bad else "")

    results.append(_run("news feeds", news))
    return results
