"""Telegram allow-list, callback validation, retries and token secrecy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nifbot.notify.telegram import (
    ButtonPress,
    Command,
    TelegramBot,
    TelegramError,
    call_buttons,
    callback_data,
    discover_chat_ids,
    parse_callback_data,
)
from tests.conftest import ALLOWED, TOKEN, FakeTelegram

STRANGER = 999


def _callback(chat_id: int, data: str) -> dict[str, Any]:
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cq1",
            "data": data,
            "message": {"message_id": 5, "chat": {"id": chat_id}},
        },
    }


def _message(chat_id: int, text: str) -> dict[str, Any]:
    return {
        "update_id": 2,
        "message": {"message_id": 6, "chat": {"id": chat_id}, "from": {"id": 1}, "text": text},
    }


def test_allow_listed_button_press_is_accepted(make_bot: Callable[..., TelegramBot]) -> None:
    bot = make_bot()
    result = bot.handle_update(_callback(ALLOWED, "v1:taken:C20261005A"))
    assert result == ButtonPress(ALLOWED, "C20261005A", "taken", "cq1")


def test_stranger_button_press_is_ignored_and_audited(
    make_bot: Callable[..., TelegramBot],
    fake_tg: FakeTelegram,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = make_bot()
    with caplog.at_level(logging.INFO, logger="nifbot.audit"):
        assert bot.handle_update(_callback(STRANGER, "v1:taken:C1")) is None
    assert any(r.getMessage() == "telegram_rejected_callback" for r in caplog.records)
    assert fake_tg.calls == []  # nothing is sent back to strangers


def test_stranger_message_is_ignored(make_bot: Callable[..., TelegramBot]) -> None:
    assert make_bot().handle_update(_message(STRANGER, "/status")) is None


def test_callback_without_message_is_rejected(make_bot: Callable[..., TelegramBot]) -> None:
    update = {"update_id": 3, "callback_query": {"id": "x", "data": "v1:skip:C1"}}
    assert make_bot().handle_update(update) is None


@pytest.mark.parametrize(
    "data",
    [
        "v1:buy:C1",
        "v1:taken:",
        "v1:taken:C1;rm -rf",
        "v2:taken:C1",
        "__import__('os')",
        "v1:taken:" + "A" * 60,
    ],
)
def test_malformed_callback_data_is_rejected(
    make_bot: Callable[..., TelegramBot], fake_tg: FakeTelegram, data: str
) -> None:
    assert make_bot().handle_update(_callback(ALLOWED, data)) is None
    assert fake_tg.calls[-1][0] == "answerCallbackQuery"


def test_invalid_update_schema_is_ignored(make_bot: Callable[..., TelegramBot]) -> None:
    assert make_bot().handle_update({"update_id": "not-int", "message": 5}) is None


def test_commands(make_bot: Callable[..., TelegramBot]) -> None:
    bot = make_bot()
    assert bot.handle_update(_message(ALLOWED, "/status")) == Command(ALLOWED, "status")
    assert bot.handle_update(_message(ALLOWED, "/status@nifbot extra")) == Command(
        ALLOWED, "status"
    )
    assert bot.handle_update(_message(ALLOWED, "/placeorder")) is None
    assert bot.handle_update(_message(ALLOWED, "hello")) is None


def test_send_refuses_non_allow_listed_chat(
    make_bot: Callable[..., TelegramBot], fake_tg: FakeTelegram
) -> None:
    with pytest.raises(TelegramError):
        make_bot().send_message(STRANGER, "hi")
    assert fake_tg.calls == []


def test_send_with_buttons_and_truncation(
    make_bot: Callable[..., TelegramBot], fake_tg: FakeTelegram
) -> None:
    bot = make_bot()
    bot.send_message(ALLOWED, "x" * 5000, call_buttons("C1"))
    method, body = fake_tg.calls[0]
    assert method == "sendMessage"
    assert len(body["text"]) == 4096
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "v1:taken:C1"


def test_broadcast_sends_to_all_allowed(
    make_bot: Callable[..., TelegramBot], fake_tg: FakeTelegram
) -> None:
    sent = make_bot(allowed=[1, 2]).broadcast("hello")
    assert set(sent) == {1, 2}
    assert len(fake_tg.calls) == 2


def test_retries_on_429_then_succeeds(
    make_bot: Callable[..., TelegramBot], fake_tg: FakeTelegram
) -> None:
    fake_tg.responses = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}}),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 9}}),
    ]
    assert make_bot().send_message(ALLOWED, "hi") == 9


def test_network_error_does_not_leak_token(
    make_bot: Callable[..., TelegramBot],
    fake_tg: FakeTelegram,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_tg.responses = [httpx.ConnectError(f"failed for /bot{TOKEN}/x")] * 4
    with pytest.raises(TelegramError) as info, caplog.at_level(logging.DEBUG):
        make_bot().send_message(ALLOWED, "hi")
    assert TOKEN not in str(info.value)
    assert all(TOKEN not in r.getMessage() for r in caplog.records)


def test_http_error_raises(make_bot: Callable[..., TelegramBot], fake_tg: FakeTelegram) -> None:
    fake_tg.responses = [httpx.Response(401, json={"ok": False, "description": "Unauthorized"})]
    with pytest.raises(TelegramError, match="401"):
        make_bot().send_message(ALLOWED, "hi")


def test_get_updates_and_answer(
    make_bot: Callable[..., TelegramBot], fake_tg: FakeTelegram
) -> None:
    fake_tg.responses = [httpx.Response(200, json={"ok": True, "result": [{"update_id": 1}, 7]})]
    bot = make_bot()
    assert bot.get_updates(offset=5) == [{"update_id": 1}]
    assert fake_tg.calls[0][1]["offset"] == 5
    bot.answer_callback("cq", "ok")
    bot.close()


def test_constructor_requires_token_and_allow_list() -> None:
    with pytest.raises(TelegramError):
        TelegramBot("", [1])
    with pytest.raises(TelegramError):
        TelegramBot("replace-me", [1])
    with pytest.raises(TelegramError):
        TelegramBot(TOKEN, [])


def test_callback_data_helpers() -> None:
    assert parse_callback_data(callback_data("skip", "abc")) == ("skip", "abc")
    assert parse_callback_data(None) is None
    with pytest.raises(ValueError):
        callback_data("taken", "bad id!")


def test_discover_chat_ids() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "message_id": 1,
                            "chat": {"id": 42},
                            "from": {"id": 42, "username": "me"},
                        },
                    },
                    {"bogus": True},
                ],
            },
        )

    assert discover_chat_ids(TOKEN, transport=httpx.MockTransport(handler)) == [(42, "me")]
