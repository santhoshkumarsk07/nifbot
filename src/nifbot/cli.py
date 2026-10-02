"""Command-line entry point: ``nifbot <command>``."""

from __future__ import annotations

import argparse
import sys
from datetime import date

from nifbot import DISCLAIMER
from nifbot.config import PROJECT_ROOT, Secrets, insecure_permissions, load_settings
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
