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
| 3 | News (free RSS + official sources) and flows connectors, pre-market brief | done (live fetch pending on your machine) |
| 4 | Shared feature module + look-ahead tests | done |
| 5 | Backtest engine, Indian charges, first 3 strategies, HTML report | next |
| 6 | Remaining strategies, regime classifier, go-live gate | |
| 7 | Model training, walk-forward validation, registry | |
| 8 | Live engine, risk manager, call tracker, kill switch | |
| 9 | Paper mode (10 expiry days), live-vs-backtest report | |
| 10 | Hardening: Docker, scheduling, security scans | |

## What to run (simple version)

Every command works as `make <name>` or, on Windows without make, `uv run nifbot <name>`.

| When | Command | What it does |
|---|---|---|
| Once | `make setup` | installs everything |
| First time only | put a Dhan token in `.env` | after that the bot renews it itself |
| Each trading day 08:30 | `make morning` | renews the Dhan token (if < 8 h left), sends the brief |
| Only if the token expired | `make dhan-login` | PIN + 6-digit authenticator code, 15 seconds |
| Once | `make selftest` | tries Telegram, Dhan live + history, NSE, FRED and news once; PASS/FAIL each |
| Once (takes 30-60 min) | `make prepare-data` | downloads ~3 years of Dhan history incl. expired options, then builds the training feature table `data/features/history.parquet` |
| 08:45 | `make brief-send` | pre-market brief to Telegram |
| 09:00-15:35 | `make record` and `make news-watch` (two windows) | records the session, sends news alerts |
| After close | `make flows-fetch` | FII/DII/Pro/Client positions for the day |

If a command prints `FAIL` or an error, copy the whole output into the chat.

## What training needs (`make data-check` shows the current state)

| Needed | Source | Status check |
|---|---|---|
| 2+ years of Nifty 1-min history | Dhan `/charts/intraday` | `Nifty 1-min history` |
| India VIX 1-min history | Dhan | `India VIX 1-min history` |
| Expired weekly options, ATM-10..ATM+10, 1-min (price, OI, IV) | Dhan `/charts/rollingoption` | `Expired options history` |
| FII/DII/Pro/Client index positions per day | NSE archive files | `FII participant OI history` |
| NSE holiday lists for every year in the history | config/holidays.yaml | `NSE holiday lists` |
| Expiry weekday rules by date | config/contracts.yaml | `Expiry weekday rules` |
| Lot sizes and charges by date | config/charges.yaml (milestone 5) | `Lot sizes and charges` |
| Training feature table | `nifbot features-history` | `Training feature table` |

Known limits (shown as INFO, not errors): Dhan's free history has no 1-minute data for
expired futures, so futures features are live-only; free news sources have no back-history,
so the news score only exists from the day you start `news-watch`; FII/DII cash history needs
manual entry or the NSE connector.

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

### Dhan token: no daily copy-paste

Dhan access tokens last about 24 hours. The bot handles this:

1. **Automatic renewal.** Every Dhan call checks the token's expiry. With less than 8 hours
   left it calls Dhan's `RenewToken` and saves the new token in
   `data/secrets/dhan_token.json` (permissions 600, never committed). Run `make morning` (or
   `uv run nifbot dhan-token`) each trading day, e.g. from Windows Task Scheduler at 08:30, and
   the token keeps rolling forward.
2. **If it expired anyway** (PC off for a day, holiday): `make dhan-login` asks for your Dhan
   PIN (hidden) and the 6-digit code from your authenticator app. Nothing you type is stored.
   If renewal fails at 08:30 you also get a Telegram message.
3. **Optional, OFF by default: fully automatic login.** Put `DHAN_PIN` and `DHAN_TOTP_SECRET`
   (the secret shown when you set up TOTP in Dhan) in `.env` and set
   `broker.dhan.auto_login_totp: true`. Risk: anyone who can read `.env` can log in to your Dhan
   account. Only use it on a machine only you can access.

Check any time with `make dhan-token`: it prints when the token expires.

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

## News, flows and the pre-market brief

```bash
make news-once     # fetch every source once; prints new relevant headlines with +/-/0 and impact
make news-health   # which sources work on your network
make flows-fetch   # NSE participant-wise OI (FII/DII/Pro/Client) for the previous trading day
make brief         # print the brief;  make brief-send  sends it to Telegram
make news-watch    # run all session: high-impact news alerts to Telegram as they arrive
```

- **Sources** (`config/news.yaml`): RBI, SEBI, NSE announcements, PIB, US Fed, US BLS, Economic
  Times, Moneycontrol, Livemint, Business Standard, Hindu BusinessLine, CNBC-TV18 and Google
  News searches (which also carry Reuters headlines). All free RSS feeds. robots.txt is
  respected, each host is rate-limited, and any source can be switched off with `enabled: false`.
- **Pipeline:** dedupe (URL + fuzzy title), keyword relevance (Nifty, heavyweights, RBI, SEBI,
  budget, Fed, crude, war/tariffs, macro, FII), sentiment, impact (high/medium/low), stored in
  SQLite, and a time-decayed news score that only counts news *after* it was fetched.
- **Sentiment:** FinBERT runs locally if you install it (`uv sync --extra nlp`, a large
  PyTorch download). Without it, a small finance word-list scorer is used (less accurate).
- **Global cues:** FRED (US Federal Reserve data, official and free): S&P 500, Dow, Nasdaq,
  Nikkei, Brent, US 10Y, dollar index, USD/INR. Values are previous closes and show their date.
- **GIFT Nifty:** no free official feed, so the brief shows "not available".
- **FII/DII cash flows:** NSE publishes these only on its website, whose terms restrict
  automated access, so the connector is off (`flows.fii_dii_enabled: false`). Enter the daily
  figures with `uv run nifbot flows-add 2026-10-01 -- -2500 3000` (FII, DII in Rs crore), or
  turn the connector on at your own discretion.
- **Pre-market tilt:** a transparent rules-based summary of the overnight inputs. It is
  not a backtested forecast, and the brief says so.

## Features (milestone 4)

`src/nifbot/features/` is the ONE place features are computed; backtest, training and live
all call `build_features()`. Columns: time of day; returns and realised volatility; gap; day
range; 15/30-minute opening range; futures basis, VWAP distance and long/short build-up;
India VIX; PCR (all strikes and near ATM, OI and volume); max pain; call/put OI walls; change
in OI near ATM; ATM IV and put-call skew; ATM straddle price; news score; event flags; days
to expiry; FII futures positioning and cash flows (previous days only).

Look-ahead protection (`tests/unit/test_feature_leakage.py`): features at time t computed
from all data must equal features computed from only the data available at t; scrambling
everything after t must not change any earlier feature; a chain snapshot is never used
before it arrived; the opening range is unknown until its window has closed; flows for a
day are only used from the next day.

Expiry days come from `config/contracts.yaml` (Thursday weekly expiry until Aug 2025,
Tuesday from Sep 2025, moved earlier on holidays). Marked `verified: false`; check it.
Holiday lists for 2023-2025 were added (also unverified) so history can be processed.

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
