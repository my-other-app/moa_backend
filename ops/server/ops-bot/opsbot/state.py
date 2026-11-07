"""A small JSON file in the bot's state directory: journal cursors, which
conditions are currently bad, when a stranger was last reported. Nothing in
it is secret; it is root's alone anyway."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("opsbot.state")


class State:
    def __init__(self, path: Path | None):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict = {}
        if path is not None:
            try:
                self.data = json.loads(path.read_text())
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as error:
                log.warning("state file unreadable (%s); starting afresh", type(error).__name__)
        self.data.setdefault("conditions", {})

    def get(self, key: str, default=None):
        with self.lock:
            return self.data.get(key, default)

    def set(self, key: str, value) -> None:
        with self.lock:
            self.data[key] = value

    def save(self) -> None:
        if self.path is None:
            return
        with self.lock:
            text = json.dumps(self.data, sort_keys=True)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
            os.replace(tmp, self.path)
        except OSError as error:
            log.warning("could not save the state file: %s", type(error).__name__)
