"""Telegram Bot API client with chat allow-list, callback validation and rate limiting.

Only the allow-listed chat IDs can receive messages or press buttons. Everything
else is ignored and written to the audit log. The bot token is never logged:
errors carry the API method name, not the request URL.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nifbot.logging_setup import audit
from nifbot.notify.ratelimit import TokenBucket

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MAX_TEXT = 4096
ENV_PLACEHOLDER = "replace-me"  # .env.example placeholder, not a secret
_CALLBACK_RE = re.compile(r"^v1:(taken|skip):([A-Za-z0-9_-]{1,40})$")
_COMMAND_RE = re.compile(r"^/([a-z_]{1,32})(?:@\w+)?(?:\s.*)?$", re.DOTALL)
ALLOWED_COMMANDS = frozenset({"start", "help", "status", "whoami"})

Action = Literal["taken", "skip"]


class TelegramError(RuntimeError):
    """A Bot API call failed. Never contains the bot token."""


# ---- inbound update schema (untrusted input) ---------------------------------


class _Chat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int


class _User(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int
    username: str | None = None


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message_id: int
    chat: _Chat
    from_: _User | None = Field(default=None, alias="from")
    text: str | None = None


class _CallbackQuery(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    message: _Message | None = None
    data: str | None = None


class _Update(BaseModel):
    model_config = ConfigDict(extra="ignore")
    update_id: int
    message: _Message | None = None
    callback_query: _CallbackQuery | None = None


# ---- parsed results ------------------------------------------------------------


@dataclass(frozen=True)
class ButtonPress:
    """A validated Taken/Skip press from an allow-listed chat."""

    chat_id: int
    call_id: str
    action: Action
    callback_query_id: str


@dataclass(frozen=True)
class Command:
    """A validated slash command from an allow-listed chat."""

    chat_id: int
    name: str


def callback_data(action: Action, call_id: str) -> str:
    """Build callback data for a call button; validated on the way out too."""
    data = f"v1:{action}:{call_id}"
    if not _CALLBACK_RE.match(data):
        raise ValueError("invalid call_id for callback data")
    return data


def parse_callback_data(data: str | None) -> tuple[Action, str] | None:
    """Return (action, call_id) for well-formed callback data, else ``None``."""
    if data is None or len(data.encode()) > 64:
        return None
    match = _CALLBACK_RE.match(data)
    if not match:
        return None
    action: Action = "taken" if match.group(1) == "taken" else "skip"
    return action, match.group(2)


def call_buttons(call_id: str) -> list[list[dict[str, str]]]:
    """Inline keyboard with Taken / Skip for a trade call."""
    return [
        [
            {"text": "✅ Taken", "callback_data": callback_data("taken", call_id)},
            {"text": "⏭ Skip", "callback_data": callback_data("skip", call_id)},
        ]
    ]


class TelegramBot:
    """Minimal Bot API client over HTTPS (certificate verification on)."""

    def __init__(
        self,
        token: str,
        allowed_chat_ids: Iterable[int],
        *,
        timeout: float = 10.0,
        max_per_minute: int = 20,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        limiter: TokenBucket | None = None,
    ) -> None:
        if not token or token == ENV_PLACEHOLDER:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        self._allowed = frozenset(allowed_chat_ids)
        if not self._allowed:
            raise TelegramError("TELEGRAM_ALLOWED_CHAT_IDS is empty")
        self._token = token
        self._max_retries = max_retries
        self._sleep = sleep
        self._limiter = limiter or TokenBucket(max_per_minute, 60.0, sleep=sleep)
        self._client = httpx.Client(
            base_url=API_BASE, timeout=timeout, transport=transport, verify=True
        )

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        return self._allowed

    def close(self) -> None:
        self._client.close()

    # -- transport -------------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any], *, limited: bool = True) -> Any:
        """POST a Bot API method with retries and exponential backoff."""
        if limited:
            self._limiter.acquire()
        path = f"/bot{self._token}/{method}"
        delay = 1.0
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._client.post(path, json=payload)
            except httpx.HTTPError as exc:
                # str(exc) may contain the URL (with token): log only the type.
                log.warning("telegram %s network error: %s", method, type(exc).__name__)
                if attempt == self._max_retries:
                    raise TelegramError(f"{method}: network error ({type(exc).__name__})") from None
                self._sleep(delay)
                delay *= 2
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 200 and body.get("ok"):
                return body.get("result")
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if retryable and attempt < self._max_retries:
                retry_after = (body.get("parameters") or {}).get("retry_after")
                wait = float(retry_after) if isinstance(retry_after, int | float) else delay
                log.warning("telegram %s HTTP %s, retrying", method, resp.status_code)
                self._sleep(min(wait, 60.0))
                delay *= 2
                continue
            desc = str(body.get("description", ""))[:200]
            raise TelegramError(f"{method}: HTTP {resp.status_code} {desc}")
        raise TelegramError(f"{method}: retries exhausted")  # pragma: no cover

    # -- outbound ----------------------------------------------------------------

    def send_message(
        self,
        chat_id: int,
        text: str,
        buttons: Sequence[Sequence[dict[str, str]]] | None = None,
    ) -> int:
        """Send plain text to an allow-listed chat. Returns the message id."""
        if chat_id not in self._allowed:
            audit("telegram_send_blocked", chat_id=chat_id)
            raise TelegramError("refusing to send to a chat that is not allow-listed")
        if len(text) > MAX_TEXT:
            text = text[: MAX_TEXT - 1] + "…"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [list(row) for row in buttons]}
        result = self._call("sendMessage", payload)
        message_id = int(result["message_id"]) if isinstance(result, dict) else 0
        audit("telegram_sent", chat_id=chat_id, message_id=message_id, chars=len(text))
        return message_id

    def broadcast(
        self, text: str, buttons: Sequence[Sequence[dict[str, str]]] | None = None
    ) -> dict[int, int]:
        """Send to every allow-listed chat."""
        return {cid: self.send_message(cid, text, buttons) for cid in sorted(self._allowed)}

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge a button press so the client stops its spinner."""
        self._call(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:200]},
            limited=False,
        )

    # -- inbound -----------------------------------------------------------------

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict[str, Any]]:
        """Long-poll for updates. Results are untrusted and must go through ``handle_update``."""
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, limited=False)
        return [u for u in result if isinstance(u, dict)] if isinstance(result, list) else []

    def handle_update(self, raw: dict[str, Any]) -> ButtonPress | Command | None:
        """Validate one update. Returns a press/command, or ``None`` if ignored."""
        try:
            update = _Update.model_validate(raw)
        except ValidationError:
            audit("telegram_update_invalid")
            return None

        if update.callback_query is not None:
            cq = update.callback_query
            chat_id = cq.message.chat.id if cq.message else None
            if chat_id not in self._allowed:
                audit("telegram_rejected_callback", chat_id=chat_id, update_id=update.update_id)
                return None
            parsed = parse_callback_data(cq.data)
            if parsed is None:
                audit("telegram_bad_callback_data", chat_id=chat_id, update_id=update.update_id)
                self.answer_callback(cq.id, "Invalid button")
                return None
            if chat_id is None:  # pragma: no cover - excluded by the allow-list check
                return None
            action, call_id = parsed
            audit("button_press", chat_id=chat_id, call_id=call_id, action=action)
            return ButtonPress(chat_id, call_id, action, cq.id)

        if update.message is not None:
            chat_id = update.message.chat.id
            if chat_id not in self._allowed:
                audit("telegram_rejected_message", chat_id=chat_id, update_id=update.update_id)
                return None
            match = _COMMAND_RE.match(update.message.text or "")
            if match and match.group(1) in ALLOWED_COMMANDS:
                audit("telegram_command", chat_id=chat_id, command=match.group(1))
                return Command(chat_id, match.group(1))
        return None


def discover_chat_ids(
    token: str, *, timeout: float = 10.0, transport: httpx.BaseTransport | None = None
) -> list[tuple[int, str]]:
    """Setup helper: list (chat_id, username) of recent senders. Sends nothing back."""
    with httpx.Client(base_url=API_BASE, timeout=timeout, transport=transport) as client:
        try:
            resp = client.post(f"/bot{token}/getUpdates", json={"timeout": 0})
        except httpx.HTTPError as exc:
            raise TelegramError(f"getUpdates: network error ({type(exc).__name__})") from None
    body = (
        resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    )
    if resp.status_code != 200 or not body.get("ok"):
        raise TelegramError(f"getUpdates: HTTP {resp.status_code}")
    found: dict[int, str] = {}
    for raw in body.get("result", []):
        try:
            update = _Update.model_validate(raw)
        except ValidationError:
            continue
        msg = update.message or (update.callback_query.message if update.callback_query else None)
        if msg is not None:
            user = msg.from_.username if msg.from_ and msg.from_.username else ""
            found[msg.chat.id] = user
    return sorted(found.items())
