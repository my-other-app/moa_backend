"""Fakes for the ops bot's tests: no network, no Docker, no systemd.

python3 -m pytest ops/server/ops-bot/tests
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

BOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BOT_DIR))

from opsbot.config import Config  # noqa: E402
from opsbot.system import CommandError, Container, Host, Runner  # noqa: E402
from opsbot.telegram import RateLimiter, Telegram  # noqa: E402

TOKEN = "123456789:TEST_not_a_real_token_000000000000"
OWNER = 424242


class FakeRunner(Runner):
    """Answers commands from `responses` (first word -> text, or a callable
    taking argv, or an exception) and records every argv."""

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def run(self, argv, timeout=30):
        self.calls.append(list(argv))
        for key in (" ".join(argv[:2]), argv[0]):
            if key in self.responses:
                answer = self.responses[key]
                if isinstance(answer, Exception):
                    raise answer
                return answer(argv) if callable(answer) else answer
        raise CommandError(f"{argv[0]} is not installed")


class FakeTelegram(Telegram):
    """Telegram with the transport replaced: every request is recorded and
    answered from `answers` (a list, consumed in order; ok by default)."""

    def __init__(self, chat_id=OWNER, answers=None):
        super().__init__(TOKEN, chat_id, limiter=RateLimiter(min_interval=0, per_window=1000), sleep=lambda s: None)
        self.requests: list[tuple[str, dict]] = []
        self.documents: list[dict] = []
        self.answers = list(answers or [])
        self.events: list[str] = []

    def _answer(self):
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return {"ok": True, "result": {}}

    def _post_json(self, method, params, timeout):
        self.requests.append((method, params))
        self.events.append(method)
        return self._answer()

    def _post_multipart(self, method, fields, file_field, path, filename, timeout):
        data = Path(path).read_bytes()
        self.documents.append({"fields": fields, "path": Path(path), "filename": filename, "data": data})
        self.events.append(method)
        return self._answer()

    @property
    def texts(self) -> list[str]:
        return [p["text"] for m, p in self.requests if m == "sendMessage"]


def container(name="backend", *, status="running", health="healthy", restarts=0, cid="aaa", exit_code=0, oom=False, started=None):
    return Container(
        id=cid,
        name=f"moa-{name}-1",
        service=name,
        project="moa",
        status=status,
        health=health,
        restarts=restarts,
        started_at=started or datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc),
        exit_code=exit_code,
        oom_killed=oom,
        mem_limit=2 * 1024**3,
    )


class FakeHost(Host):
    """A box whose every reading is a plain attribute the test sets."""

    def __init__(self):
        super().__init__(FakeRunner())
        self.box: list[Container] = [container("backend"), container("postgres")]
        self.docker_error: Exception | None = None
        self.log_text: dict[str, str] = {}
        self.log_calls: list[tuple] = []
        self.disk_rows = [("/", 40, 100)]
        self.mem = {"MemTotal": 12 * 1024**3, "MemAvailable": 6 * 1024**3, "SwapTotal": 0}
        self.psi: float | None = 0.0
        self.journals: dict[str, list[dict]] = {}
        self.journal_calls: list[tuple] = []
        self.units: dict[str, str] = {}
        self.started_units: list[str] = []

    def containers(self):
        if self.docker_error:
            raise self.docker_error
        return list(self.box)

    def stats(self):
        return {c.name: {"cpu": "1.50%", "mem": "512.3MiB / 2GiB"} for c in self.box}

    def logs(self, name, *, tail=None, since=None, until=None):
        self.log_calls.append((name, tail, since, until))
        return self.log_text.get(name, "")

    def docker_df(self):
        return [{"Type": "Images", "TotalCount": "9", "Active": "8", "Size": "4.1GB", "Reclaimable": "1.2GB (29%)"}]

    def disks(self, paths):
        return list(self.disk_rows)

    def meminfo(self, path="/proc/meminfo"):
        return dict(self.mem)

    def memory_pressure(self, path="/proc/pressure/memory"):
        return self.psi

    def loadavg(self):
        return (0.5, 0.4, 0.3)

    def journal(self, matches, *, cursor, since=None, limit=300):
        key = "kernel" if "-k" in matches else "ssh"
        self.journal_calls.append((key, cursor, since))
        entries, self.journals[key] = self.journals.get(key, []), []
        return entries

    def unit_state(self, unit):
        return self.units.get(unit, "inactive")

    def start_unit(self, unit):
        self.started_units.append(unit)


@pytest.fixture
def cfg(tmp_path) -> Config:
    return replace(
        Config(token=TOKEN, chat_id=OWNER),
        host_label="moa-prod",
        backup_dir=tmp_path / "backups",
        work_dir=tmp_path / "work",
        dc="/srv/moa/dc",
    )


@pytest.fixture
def telegram() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def host() -> FakeHost:
    return FakeHost()
