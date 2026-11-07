"""The poller: only the owner's chat is answered; strangers are ignored,
logged and reported once a day; network trouble backs off."""

import logging

import pytest
from conftest import OWNER, TOKEN, FakeTelegram

from opsbot.bot import Bot
from opsbot.commands import Commands
from opsbot.state import State
from opsbot.telegram import TelegramError
from opsbot.watcher import Watcher

NOW = 1_800_000_000.0


def make_bot(cfg, host, telegram, tmp_path, now=NOW):
    state = State(tmp_path / "state.json")
    sleeps = []
    bot = Bot(
        cfg, telegram, Commands(cfg, host), Watcher(cfg, host, state, clock=lambda: now), state, clock=lambda: now, sleep=sleeps.append
    )
    return bot, sleeps


def update(uid, chat, text, date=NOW, username="someone"):
    return {
        "update_id": uid,
        "message": {"message_id": uid, "date": date, "chat": {"id": chat}, "from": {"username": username}, "text": text},
    }


def test_the_owner_is_answered(cfg, host, telegram, tmp_path):
    bot, _ = make_bot(cfg, host, telegram, tmp_path)
    bot.handle_update(update(10, OWNER, "/help"))
    [(method, params)] = telegram.requests
    assert method == "sendMessage" and params["chat_id"] == OWNER and "/status" in params["text"]
    assert bot.offset == 11


def test_a_stranger_gets_no_reply_and_the_owner_hears_once_a_day(cfg, host, telegram, tmp_path, caplog):
    bot, _ = make_bot(cfg, host, telegram, tmp_path)
    with caplog.at_level(logging.WARNING):
        for uid in range(5):
            bot.handle_update(update(uid, 999, "/status", username="mallory"))
            bot.handle_update(update(100 + uid, 999, "/backup"))
    # Nothing ran, nothing went to the stranger, one note to the owner.
    assert host.started_units == []
    assert all(params["chat_id"] == OWNER for _, params in telegram.requests)
    [note] = telegram.texts
    assert "someone else wrote" in note and "999" in note and "mallory" in note
    assert "ignored a message from chat 999" in caplog.text
    # The next day: one more.
    bot.state.set("stranger_day", "2000-01-01")
    bot.handle_update(update(200, 999, "hi"))
    assert len(telegram.texts) == 2


def test_the_client_only_ever_writes_to_the_owner(cfg):
    telegram = FakeTelegram(chat_id=OWNER)
    telegram.send_message("x")
    assert telegram.requests[0][1]["chat_id"] == OWNER


def test_a_command_sent_while_offline_is_not_run(cfg, host, telegram, tmp_path):
    bot, _ = make_bot(cfg, host, telegram, tmp_path)
    bot.handle_update(update(1, OWNER, "/backup", date=NOW - 3600))
    assert host.started_units == []
    assert "Not run" in telegram.texts[0]


def test_non_message_updates_and_empty_text_are_skipped(cfg, host, telegram, tmp_path):
    bot, _ = make_bot(cfg, host, telegram, tmp_path)
    bot.handle_update({"update_id": 5, "edited_message": {"chat": {"id": OWNER}}})
    bot.handle_update({"update_id": 6, "message": {"chat": {"id": OWNER}, "date": NOW, "photo": []}})
    assert telegram.requests == [] and bot.offset == 7


def test_poll_errors_back_off_and_never_raise(cfg, host, tmp_path):
    telegram = FakeTelegram(answers=[OSError("network down")] * 3 + [{"ok": False, "error_code": 401, "description": "Unauthorized"}])
    bot, sleeps = make_bot(cfg, host, telegram, tmp_path)
    bot.poll_once()  # three retries? no: getUpdates is not retried, the loop backs off
    bot.poll_once()
    bot.poll_once()
    bot.poll_once()
    assert len(sleeps) == 4
    assert sleeps[0] < sleeps[1] < sleeps[2]  # doubling (with jitter)
    assert sleeps[3] == 600.0  # the token refused: slow down a lot


def test_updates_advance_the_offset_and_survive_a_bad_one(cfg, host, tmp_path, monkeypatch):
    telegram = FakeTelegram(answers=[{"ok": True, "result": [update(41, OWNER, "/help"), update(42, OWNER, "/status")]}])
    bot, _ = make_bot(cfg, host, telegram, tmp_path)
    calls = []

    def boom(text):
        calls.append(text)
        if text == "/help":
            raise RuntimeError("bug")
        return "ok"

    monkeypatch.setattr(bot.commands, "handle", boom)
    bot.poll_once()
    assert calls == ["/help", "/status"] and bot.offset == 43
    assert bot.state.get("offset") == 43
    assert telegram.requests[0][0] == "getUpdates" and telegram.requests[0][1]["allowed_updates"] == ["message"]


def test_errors_from_telegram_never_carry_the_token(cfg):
    telegram = FakeTelegram(answers=[{"ok": False, "error_code": 400, "description": f"bad request for bot{TOKEN}"}])
    with pytest.raises(TelegramError) as caught:
        telegram.call("sendMessage", {}, retries=0)
    assert TOKEN not in str(caught.value)
