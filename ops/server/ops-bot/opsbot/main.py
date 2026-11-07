"""python3 -I -B /srv/moa/ops-bot/run.py <command>

  serve             the bot (moa-ops-bot.service)
  deliver-backup    encrypt the newest dump and send it (myotherapp-backup.service's ExecStartPost)
  notify --title T  send an alert, its body read from stdin, redacted (ops/backup/alert.sh)
  check-config      say whether the config is complete (never prints the token)
  test-message      check-config, then send a test message to the owner's chat
  status            print /status here (HTML), sending nothing

Settings: /srv/moa/env/ops-bot.env, or OPS_BOT_CONFIG=<path>.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from . import commands as commands_mod
from . import config as config_mod
from . import fmt
from .backup import deliver
from .bot import Bot
from .redact import redact
from .state import State
from .system import Host, Runner
from .telegram import Telegram
from .watcher import Watcher

log = logging.getLogger("opsbot")

NOTIFY_INPUT_CAP = 20000


def _load(args) -> config_mod.Config:
    return config_mod.load(args.config)


def cmd_serve(args) -> int:
    cfg = _load(args)
    cfg.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = State(cfg.work_dir / "state.json")
    telegram = Telegram(cfg.token, cfg.chat_id)
    host = Host(Runner(), project=cfg.compose_project)
    bot = Bot(cfg, telegram, commands_mod.Commands(cfg, host), Watcher(cfg, host, state), state)

    def stop(signum, frame):
        log.info("stopping (signal %s)", signum)
        bot.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("serving chat %s on %s", cfg.chat_id, cfg.host_label)
    bot.run()
    state.save()
    return 0


def cmd_deliver_backup(args) -> int:
    try:
        cfg = _load(args)
    except config_mod.ConfigError as error:
        if error.unset:
            # Not set up yet: the backup itself is fine, so the unit is too.
            print(f"deliver-backup: Telegram is not set up ({error}); the dump was not sent")
            return 0
        print(f"deliver-backup FAILED: the bot's settings: {error}")
        return 1
    return deliver(cfg, Telegram(cfg.token, cfg.chat_id), Runner())


def cmd_notify(args) -> int:
    cfg = _load(args)
    body = sys.stdin.read(NOTIFY_INPUT_CAP) if not sys.stdin.isatty() else ""
    text = f"<b>[ALERT] {fmt.esc(redact(args.title, 200))}</b>"
    if body.strip():
        text += "\n" + fmt.pre(redact(body.strip(), 3000))
    return 0 if Telegram(cfg.token, cfg.chat_id).send_message(text) else 1


def cmd_check_config(args) -> int:
    cfg = _load(args)
    print(f"ok: chat id {cfg.chat_id}, host label {cfg.host_label!r}, token set (not shown), age recipient {cfg.age_recipient[:12]}...")
    return 0


def cmd_test_message(args) -> int:
    cfg = _load(args)
    ok = Telegram(cfg.token, cfg.chat_id).send_message(
        f"<b>{fmt.esc(cfg.host_label)}</b>: test message from the ops bot. /help for commands."
    )
    print("sent" if ok else "NOT sent (see the log line above)")
    return 0 if ok else 1


def cmd_status(args) -> int:
    cfg = _load(args)
    host = Host(Runner(), project=cfg.compose_project)
    print(commands_mod.Commands(cfg, host).handle("/status"))
    return 0


COMMANDS = {
    "serve": cmd_serve,
    "deliver-backup": cmd_deliver_backup,
    "notify": cmd_notify,
    "check-config": cmd_check_config,
    "test-message": cmd_test_message,
    "status": cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ops-bot", description="MyOtherApp's Telegram ops bot")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("--config", help="settings file (default /srv/moa/env/ops-bot.env)")
    parser.add_argument("--title", default="Alert", help="notify: the alert's title")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    try:
        return COMMANDS[args.command](args)
    except config_mod.ConfigError as error:
        print(f"ops-bot: {error}", file=sys.stderr)
        return config_mod.EX_CONFIG
