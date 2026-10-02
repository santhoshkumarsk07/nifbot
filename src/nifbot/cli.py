"""Command-line entry point: ``nifbot <command>``."""

from __future__ import annotations

import argparse
import sys
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from nifbot import DISCLAIMER
from nifbot.config import PROJECT_ROOT, Secrets, insecure_permissions, load_settings
from nifbot.data.adapter import DataError
from nifbot.data.factory import dhan_adapter, dhan_client
from nifbot.data.history import DhanHistory, RollingSpec, relative_strikes
from nifbot.data.models import Snapshot
from nifbot.data.recorder import RawWriter, Recorder, compact, day_dir
from nifbot.logging_setup import setup_logging
from nifbot.notify.telegram import TelegramBot, TelegramError, discover_chat_ids
from nifbot.timeutil import now_ist
from nifbot.trading_calendar import CalendarError, TradingCalendar

ENV_FILE = PROJECT_ROOT / ".env"


def _secrets() -> Secrets:
    if insecure_permissions(ENV_FILE):
        print("WARNING: .env is readable by other users. Run: chmod 600 .env", file=sys.stderr)
    return Secrets(_env_file=ENV_FILE)


def _init_logging(secrets: Secrets) -> None:
    settings = load_settings()
    setup_logging(
        PROJECT_ROOT / settings.logging.dir,
        secrets.secret_values(),
        level=settings.logging.level,
        max_bytes=settings.logging.max_bytes,
        backup_count=settings.logging.backup_count,
        console=False,
    )


def cmd_check(_: argparse.Namespace) -> int:
    """Validate config, calendar and that secrets are present (values never printed)."""
    settings = load_settings()
    secrets = _secrets()
    cal = TradingCalendar.load()
    today = now_ist().date()
    print(f"settings ok: broker={settings.broker.name}, capital_inr={settings.capital_inr:,.0f}")
    print(f"order placement enabled: {settings.order_placement.enabled}")
    for name in ("telegram_bot_token", "dhan_client_id", "dhan_access_token"):
        print(f"{name}: {'set' if getattr(secrets, name) else 'MISSING'}")
    print(f"telegram allowed chats: {len(secrets.telegram_allowed_chat_ids)}")
    try:
        print(
            f"{today} trading day: {cal.is_trading_day(today)} (verified={cal.is_verified(today)})"
        )
    except CalendarError as exc:
        print(f"calendar FAIL: {exc}")
        return 1
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    """Print whether a date is an NSE trading day."""
    cal = TradingCalendar.load()
    day = date.fromisoformat(args.date) if args.date else now_ist().date()
    try:
        trading = cal.is_trading_day(day)
    except CalendarError as exc:
        print(f"FAIL: {exc}")
        return 1
    holiday = cal.holiday_name(day)
    note = f" ({holiday})" if holiday else ""
    print(f"{day} {day:%A}: {'TRADING DAY' if trading else 'closed'}{note}")
    if not cal.is_verified(day):
        print("note: holiday list for this year is not yet verified against the NSE circular")
    for ev in cal.events_on(day):
        print(f"event: {ev.name} [{ev.impact}]")
    return 0


def cmd_tg_whoami(_: argparse.Namespace) -> int:
    """List chat IDs that recently messaged the bot (send it any message first)."""
    secrets = _secrets()
    if secrets.telegram_bot_token is None:
        print("TELEGRAM_BOT_TOKEN is not set in .env")
        return 1
    try:
        chats = discover_chat_ids(secrets.telegram_bot_token.get_secret_value())
    except TelegramError as exc:
        print(f"FAIL: {exc}")
        return 1
    if not chats:
        print("No messages found. Send any message to your bot in Telegram, then retry.")
    for chat_id, user in chats:
        print(f"chat_id={chat_id} user=@{user}")
    return 0


def cmd_tg_test(_: argparse.Namespace) -> int:
    """Send a test message to every allow-listed chat."""
    settings = load_settings()
    secrets = _secrets()
    _init_logging(secrets)
    token = secrets.telegram_bot_token.get_secret_value() if secrets.telegram_bot_token else ""
    try:
        bot = TelegramBot(
            token,
            secrets.telegram_allowed_chat_ids,
            timeout=settings.telegram.request_timeout_seconds,
            max_per_minute=settings.telegram.max_messages_per_minute,
        )
        sent = bot.broadcast(
            f"nifbot test message ({now_ist():%Y-%m-%d %H:%M} IST).\n"
            "Setup works. No trade calls are sent yet.\n\n" + DISCLAIMER
        )
        bot.close()
    except TelegramError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"sent to {len(sent)} chat(s)")
    return 0


def _summary(snap: Snapshot) -> str:
    parts = [f"{snap.received_at:%H:%M:%S}"]
    for name, q in (("spot", snap.spot), ("fut", snap.futures), ("vix", snap.vix)):
        parts.append(f"{name}={q.ltp:.2f}" if q else f"{name}=n/a")
    if snap.chain:
        parts.append(f"chain {snap.chain.expiry} rows={len(snap.chain.rows)}")
    if snap.errors:
        parts.append(f"errors={len(snap.errors)}")
    return " ".join(parts)


def _recorder_setup() -> tuple[Recorder, Path]:
    settings = load_settings()
    secrets = _secrets()
    _init_logging(secrets)
    data_dir = PROJECT_ROOT / settings.recorder.data_dir
    adapter, fut_desc = dhan_adapter(settings, secrets, data_dir, on_raw=RawWriter(data_dir))
    print(f"futures contract: {fut_desc}")
    return Recorder(adapter, data_dir), data_dir


def cmd_record_once(_args: argparse.Namespace) -> int:
    """Take one snapshot now (works outside market hours too) and print a summary."""
    try:
        recorder, _data_dir = _recorder_setup()
    except DataError as exc:
        print(f"FAIL: {exc}")
        return 1
    snap = recorder.snapshot()
    path = recorder.write(snap)
    print(_summary(snap))
    for err in snap.errors:
        print(f"  error: {err}")
    print(f"written: {path}")
    return 0 if not snap.errors else 2


def cmd_record(_: argparse.Namespace) -> int:
    """Record a full session (09:00-15:35 IST), then compact to Parquet."""
    try:
        recorder, data_dir = _recorder_setup()
    except DataError as exc:
        print(f"FAIL: {exc}")
        return 1
    s = load_settings()
    try:
        count = recorder.run(
            TradingCalendar.load(),
            s.session.monitor_start,
            s.session.monitor_end,
            s.recorder.interval_seconds,
            threading.Event(),
            on_snapshot=lambda snap: print(_summary(snap), flush=True),
        )
    except CalendarError as exc:
        print(f"FAIL: {exc}")
        return 1
    except KeyboardInterrupt:
        count = -1
    print(f"snapshots written: {count}")
    today_dir = day_dir(data_dir, now_ist().date())
    if (today_dir / "snapshots.jsonl").exists():
        for out in compact(today_dir):
            print(f"compacted: {out}")
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    """Convert a recorded day to Parquet."""
    settings = load_settings()
    day = date.fromisoformat(args.date)
    try:
        outs = compact(day_dir(PROJECT_ROOT / settings.recorder.data_dir, day))
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}")
        return 1
    for out in outs:
        print(f"written: {out}")
    return 0


def cmd_fetch_history(args: argparse.Namespace) -> int:
    """Download Nifty spot/futures and expired-options candles from Dhan."""
    settings = load_settings()
    secrets = _secrets()
    _init_logging(secrets)
    end = date.fromisoformat(args.end) if args.end else now_ist().date() - timedelta(days=1)
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=365 * args.years)
    out_dir = PROJECT_ROOT / "data" / "history" / "dhan"
    try:
        hist = DhanHistory(dhan_client(settings, secrets), out_dir)
    except DataError as exc:
        print(f"FAIL: {exc}")
        return 1
    nifty = settings.broker.dhan.nifty_security_id
    try:
        spot = hist.intraday(nifty, "IDX_I", "INDEX", start, end, oi=False)
        print(f"spot rows: {len(spot)} -> {hist.save(spot, f'spot_{start}_{end}')}")
        vix_id = settings.broker.dhan.vix_security_id
        vix = hist.intraday(vix_id, "IDX_I", "INDEX", start, end, oi=False)
        print(f"vix rows: {len(vix)} -> {hist.save(vix, f'vix_{start}_{end}')}")
    except DataError as exc:
        print(f"FAIL: {exc}")
        return 1
    if args.options:
        for strike in relative_strikes(args.width):
            sides: tuple[Literal["CALL", "PUT"], ...] = ("CALL", "PUT")
            for side in sides:
                spec = RollingSpec("WEEK", 1, strike, side)
                frame = hist.rolling_option(nifty, spec, start, end)
                path = hist.save(frame, f"options_week1_{strike}_{side}_{start}_{end}")
                print(f"options {strike} {side}: {len(frame)} rows -> {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nifbot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="validate config, calendar and secrets").set_defaults(
        func=cmd_check
    )
    cal = sub.add_parser("calendar", help="is a date an NSE trading day?")
    cal.add_argument("date", nargs="?", help="YYYY-MM-DD (default: today IST)")
    cal.set_defaults(func=cmd_calendar)
    sub.add_parser("tg-whoami", help="find your Telegram chat ID").set_defaults(func=cmd_tg_whoami)
    sub.add_parser("tg-test", help="send a Telegram test message").set_defaults(func=cmd_tg_test)
    sub.add_parser("record-once", help="take one Dhan snapshot now").set_defaults(
        func=cmd_record_once
    )
    sub.add_parser("record", help="record today's session").set_defaults(func=cmd_record)
    comp = sub.add_parser("compact", help="convert a recorded day to Parquet")
    comp.add_argument("date", help="YYYY-MM-DD")
    comp.set_defaults(func=cmd_compact)
    fh = sub.add_parser("fetch-history", help="download Dhan historical candles")
    fh.add_argument("--start", help="YYYY-MM-DD")
    fh.add_argument("--end", help="YYYY-MM-DD (default: yesterday)")
    fh.add_argument("--years", type=int, default=3)
    fh.add_argument("--options", action="store_true", help="also expired weekly options")
    fh.add_argument("--width", type=int, default=10, help="strikes each side of ATM")
    fh.set_defaults(func=cmd_fetch_history)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
