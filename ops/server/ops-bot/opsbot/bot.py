"""serve: the long-running bot. Two threads:

- the poller: getUpdates long polling (no webhook, no open port). Only the
  configured chat is answered; anyone else is ignored without a reply,
  logged (at most once a minute) and reported to the owner once a day;
- the watcher: every OPS_WATCH_INTERVAL seconds, one Watcher.tick().

Errors from Telegram or the network never end the process: the poller backs
off (2 s doubling to 5 min; 10 min when the token is refused) and the
watcher just tries again next round.
"""

from __future__ import annotations

import logging
import random
import threading
import time

from . import fmt
from .commands import Commands
from .config import Config
from .redact import redact
from .state import State
from .telegram import Telegram, TelegramError
from .watcher import Watcher

log = logging.getLogger("opsbot")

#: A command older than this when it arrives (the bot was down) is not run.
STALE_AFTER = 120
START_NOTICE_EVERY = 6 * 3600


class Bot:
    def __init__(self, cfg: Config, telegram: Telegram, commands: Commands, watcher: Watcher, state: State, *, clock=time.time, sleep=None):
        self.cfg = cfg
        self.telegram = telegram
        self.commands = commands
        self.watcher = watcher
        self.state = state
        self.clock = clock
        self.stop = threading.Event()
        #: Waits are cut short by stop (SIGTERM) unless a test passes its own.
        self.sleep = sleep or self.stop.wait
        self.offset: int | None = state.get("offset")
        self.failures = 0
        self.last_stranger_log = 0.0

    # -- updates -------------------------------------------------------------

    def handle_update(self, update: dict) -> None:
        self.offset = int(update.get("update_id", 0)) + 1
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = (message.get("chat") or {}).get("id")
        if chat != self.cfg.chat_id:
            self.stranger(message)
            return
        text = message.get("text")
        sent = message.get("date") or 0
        if sent and self.clock() - sent > STALE_AFTER:
            if text and text.startswith("/"):
                self.telegram.send_message(
                    f"Not run: {fmt.esc(text[:40])} was sent at {fmt.stamp_time(sent)}, while I was offline. Send it again."
                )
            return
        reply = self.commands.handle(text)
        if reply:
            self.telegram.send_message(reply)

    def stranger(self, message: dict) -> None:
        chat = (message.get("chat") or {}).get("id")
        user = (message.get("from") or {}).get("username") or ""
        now = self.clock()
        if now - self.last_stranger_log >= 60:
            self.last_stranger_log = now
            log.warning("ignored a message from chat %s (not the owner's)", chat)
        today = fmt.now_ist().strftime("%Y-%m-%d")
        if self.state.get("stranger_day") == today:
            return
        self.state.set("stranger_day", today)
        self.state.save()
        who = f"chat id {chat}" + (f", @{user[:32]}" if user else "")
        self.telegram.send_message(
            f"<b>{fmt.esc(self.cfg.host_label)}</b>: someone else wrote to this bot ({fmt.esc(who)}). "
            "Ignored, not answered. (Reported at most once a day.)"
        )

    def poll_once(self) -> None:
        try:
            updates = self.telegram.get_updates(self.offset)
        except TelegramError as error:
            self.failures += 1
            if error.status in (401, 404):
                delay = 600.0
                log.error("Telegram refused the token (%s); check TELEGRAM_BOT_TOKEN", error.status)
            else:
                delay = min(2.0**self.failures, 300.0) * random.uniform(0.8, 1.2)
                log.warning("getUpdates failed (%s); next try in %.0fs", redact(str(error), 200), delay)
            self.sleep(delay)
            return
        self.failures = 0
        for update in updates:
            try:
                self.handle_update(update)
            except Exception as error:  # noqa: BLE001 -- one bad update must not stop the bot
                log.error("update failed: %s", redact(f"{type(error).__name__}: {error}", 300))
        if updates:
            self.state.set("offset", self.offset)
            self.state.save()

    # -- loops ---------------------------------------------------------------

    def poll_loop(self) -> None:
        while not self.stop.is_set():
            self.poll_once()

    def watch_loop(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                message = self.watcher.tick()
                if message:
                    self.telegram.send_message(message)
            except Exception as error:  # noqa: BLE001
                log.error("watcher round failed: %s", redact(f"{type(error).__name__}: {error}", 300))
            self.stop.wait(max(5.0, self.cfg.watch_interval - (time.monotonic() - started)))

    def start_notice(self) -> None:
        now = self.clock()
        if now - float(self.state.get("start_notice") or 0) < START_NOTICE_EVERY:
            return
        self.state.set("start_notice", now)
        self.state.save()
        self.telegram.send_message(f"<b>{fmt.esc(self.cfg.host_label)}</b>: ops bot started. /help for commands.")

    def run(self) -> None:
        self.start_notice()
        threads = [
            threading.Thread(target=self.poll_loop, name="poll", daemon=True),
            threading.Thread(target=self.watch_loop, name="watch", daemon=True),
        ]
        for thread in threads:
            thread.start()
        while not self.stop.is_set():
            for thread in threads:
                if not thread.is_alive():
                    log.error("thread %s ended; exiting so systemd restarts the bot", thread.name)
                    self.stop.set()
                    raise SystemExit(1)
            self.stop.wait(5)
