# nifbot

A semi-automatic assistant for Nifty 50 options. It watches market data and news, sends a
pre-market brief, news alerts and trade calls to **your** Telegram, and tracks the calls.
**It never places orders.** You decide and place every trade yourself.

> Every call is a bot signal, not investment advice. No strategy goes live unless it passes
> an out-of-sample backtest after full costs, and passing a backtest does not guarantee profit.

## Status

| # | Milestone | Status |
|---|---|---|
| 1 | Skeleton, config, secrets, holiday calendar, Telegram with allow-list | done (test message pending on your machine) |
| 2 | Dhan adapter, recorder, replay adapter, Dhan history download | done (live recording pending) |
| 3 | News (free RSS + official sources) and flows connectors, pre-market brief | next |
| 4 | Shared feature module + look-ahead tests | |
| 5 | Backtest engine, Indian charges, first 3 strategies, HTML report | |
| 6 | Remaining strategies, regime classifier, go-live gate | |
| 7 | Model training, walk-forward validation, registry | |
| 8 | Live engine, risk manager, call tracker, kill switch | |
| 9 | Paper mode (10 expiry days), live-vs-backtest report | |
| 10 | Hardening: Docker, scheduling, security scans | |

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
make setup        # installs pinned deps, git hooks (gitleaks), creates .env (chmod 600)
make test lint audit
```

### Telegram bot token and chat ID

1. In Telegram, open **@BotFather**, send `/newbot`, and follow the prompts. Copy the token.
2. Put it in `.env` as `TELEGRAM_BOT_TOKEN=...`. Never paste it anywhere else.
3. Send any message to your new bot, then run `make tg-whoami`. It prints your `chat_id`.
4. Put it in `.env` as `TELEGRAM_ALLOWED_CHAT_IDS=<chat_id>` (comma-separate several).
5. Run `make tg-test`. You should receive a test message.

The bot ignores messages and button presses from every chat that is not allow-listed and
writes each attempt to `logs/audit.log`.

### Dhan

1. In the Dhan web app, open **DhanHQ Trading APIs** and generate an access token.
   Market data APIs (quotes, option chain, historical) must be active on your account.
2. Put `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` in `.env`. Dhan tokens expire; renew as needed.
3. `make record-once` takes one snapshot and prints it. Check the numbers against your
   Dhan terminal (spot, futures, VIX, a few strikes). Also verify `vix_security_id` in
   `config/settings.yaml`.
4. On a trading day, `make record` records 09:00-15:35 IST every minute into
   `data/recorded/<date>/` and converts it to Parquet at the end.

Only market-data endpoints are called. Order placement is disabled in config
(`order_placement.enabled: false`) and no order code exists.

Raw API responses are stored next to the parsed data (`raw.jsonl`), so if Dhan changes a
field name nothing is lost and the day can be re-parsed.

### Historical data from Dhan

```bash
make fetch-history                            # last 3 years: Nifty spot + India VIX, 1-minute
make fetch-history ARGS="--options --width 10" # + expired weekly options ATM-10..ATM+10
```

Files go to `data/history/dhan/` (Parquet, plus gzipped raw responses). Every candle has
`start` and `available_at = start + interval`; models and backtests only use a candle after
`available_at`, which prevents look-ahead. How far back Dhan's data goes depends on Dhan.

### Checks

```bash
make check                    # config valid, secrets present (values never printed)
make calendar DATE=2026-10-20 # is it an NSE trading day?
```

## Configuration

- `config/settings.yaml`: capital, risk limits, schedule (IST). **Set `capital_inr` to your
  real capital.** The shipped value is a placeholder.
- `config/holidays.yaml`: NSE holidays per year. A missing year stops all calls (kill switch).
  The 2026 list comes from secondary sources and is marked `verified: false` until checked
  against the official NSE circular.
- `config/events.yaml`: RBI/Fed/CPI/budget/results days (no new calls or reduced size).

## Security

- Secrets only in `.env` (git-ignored, `chmod 600`) or environment variables. Logs redact them.
- HTTPS with certificate verification, timeouts and retries on every request.
- All fetched data is validated with pydantic and treated as untrusted.
- Pre-commit runs gitleaks; CI runs ruff, mypy, pytest, bandit, pip-audit and gitleaks.
