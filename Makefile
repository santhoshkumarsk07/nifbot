.PHONY: setup test lint check calendar tg-whoami tg-test audit record record-once compact fetch-history backtest train report paper live

setup:            ## install pinned deps + git hooks
	uv sync
	uv run pre-commit install
	@test -f .env || (cp .env.example .env && chmod 600 .env && echo "created .env - fill it in")

test:
	uv run pytest

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy

audit:
	uv run bandit -q -c pyproject.toml -r src
	uv run pip-audit --skip-editable

check:            ## validate config, calendar, secrets present
	uv run nifbot check

calendar:
	uv run nifbot calendar $(DATE)

tg-whoami:        ## print your Telegram chat id (message the bot first)
	uv run nifbot tg-whoami

tg-test:          ## send a test message to allow-listed chats
	uv run nifbot tg-test

record-once:      ## one Dhan snapshot now (spot, futures, VIX, option chain)
	uv run nifbot record-once

record:           ## record today's session 09:00-15:35 IST, then compact to Parquet
	uv run nifbot record

compact:          ## make compact DATE=2026-10-05
	uv run nifbot compact $(DATE)

fetch-history:    ## Dhan history: spot+VIX (add ARGS="--options" for expired weekly options)
	uv run nifbot fetch-history $(ARGS)

backtest train report paper live:
	@echo "'$@' is not implemented yet (see milestone plan in README)"; exit 1
