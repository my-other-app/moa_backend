"""The watcher: every OPS_WATCH_INTERVAL seconds (60), look at the box and
send at most one message with whatever changed.

  containers  unhealthy / exited / restarting / gone, and back; restarts
              (at most one alert per container per 10 minutes)
  disk        over 80%, again over 90%, and back under (78%)
  memory      MemAvailable under OPS_MEM_LOW_PCT (10%) or PSI full avg60
              over 20%, and back; OOM kills from the kernel's journal
  ssh         every accepted login: who, from where, how
  errors      Traceback / ERROR / HTTP 5xx lines in each service's logs:
              OPS_ERROR_MIN_LINES (3) in one interval starts a burst, at
              most one alert per service per 10 minutes, with the count and
              a redacted sample, and one message when it has been quiet for 10
"""

from __future__ import annotations

import logging
import re
import time

from . import fmt
from .alerts import Bursts, Conditions
from .config import Config
from .redact import redact
from .reports import container_state
from .state import State
from .system import CommandError, Host

log = logging.getLogger("opsbot.watcher")

BURST_WINDOW = 600.0
MISSING_TICKS = 3
DISK_LEVELS = (80, 90)
DISK_HYSTERESIS = 2
SSH_SHOWN = 10

ACCEPTED_RE = re.compile(r"\bAccepted (\S+) for (\S+) from (\S+) port (\d+)")
OOM_RE = re.compile(r"(?i)out of memory: Killed process \d+ \(([^)]{1,64})\)")
TRACEBACK = "Traceback (most recent call last)"
ERROR_RE = re.compile(r"\bERROR\b|\bCRITICAL\b|\"level\"\s*:\s*\"(?:error|fatal|panic)\"|\blevel=(?:error|fatal)\b")
HTTP_5XX_RE = re.compile(r"\"\s5\d\d\b|\"status\"\s*:\s*5\d\d\b|\bstatus[=:]\s?5\d\d\b")


def scan_errors(text: str) -> tuple[int, str]:
    """(error lines, a sample) in a chunk of container log. A traceback
    counts once and its sample is the exception line that ends it."""
    count, sample = 0, ""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if TRACEBACK in line:
            count += 1
            j = i + 1
            while j < len(lines) and (lines[j][:1].isspace() or not lines[j].strip()):
                j += 1
            if not sample:
                sample = "Traceback … " + (lines[j].strip() if j < len(lines) else "")
            i = j + 1
            continue
        if ERROR_RE.search(line) or HTTP_5XX_RE.search(line):
            count += 1
            if not sample:
                sample = line.strip()
        i += 1
    return count, redact(sample, 300)


def disk_level(pct: float, before: int) -> int:
    level = 0
    for i, threshold in enumerate(DISK_LEVELS, start=1):
        # Once over a threshold, stay over it until 2 points under.
        if pct >= threshold or (before >= i and pct >= threshold - DISK_HYSTERESIS):
            level = i
    return level


class Watcher:
    def __init__(self, cfg: Config, host: Host, state: State, *, clock=time.time):
        self.cfg = cfg
        self.host = host
        self.state = state
        self.clock = clock
        self.conditions = Conditions(state.data["conditions"])
        self.restart_bursts = Bursts(BURST_WINDOW, threshold=1)
        self.error_bursts = Bursts(BURST_WINDOW, threshold=cfg.error_min_lines)
        self.oom_bursts = Bursts(BURST_WINDOW, threshold=1)
        self.restarts: dict[str, tuple[str, int]] = {}
        self.seen: set[str] = set()
        self.missing: dict[str, int] = {}
        self.started = clock()
        self.last_logs: float | None = None

    # -- the checks ----------------------------------------------------------

    def check_containers(self, now: float, out: list[str]) -> list:
        try:
            containers = self.host.containers()
        except CommandError as error:
            msg = self.conditions.update("docker", True, f"[ALERT] Docker is not answering: {fmt.esc(redact(str(error), 200))}", None)
            if msg:
                out.append(msg)
            return []
        msg = self.conditions.update("docker", False, "", "[OK] Docker answers again")
        if msg:
            out.append(msg)

        present = {c.name for c in containers}
        for c in containers:
            self.seen.add(c.name)
            self.missing.pop(c.name, None)
            label = fmt.esc(c.label)
            state = container_state(c)
            bad = None if state == "start" else state != "ok"
            what = {"UNHLTH": "is unhealthy", "RESTRT": "keeps restarting", "PAUSED": "is paused", "DEAD": "is dead"}.get(
                state, f"is {fmt.esc(c.status)}"
            )
            if state.startswith("EXIT"):
                what = f"exited with status {c.exit_code}" + (" (killed: out of memory)" if c.oom_killed else "")
            back = "is running" + (" and healthy" if c.health == "healthy" else "")
            msg = self.conditions.update(f"c:{c.name}", bad, f"[ALERT] {label} {what}", f"[OK] {label} {back}")
            if msg:
                out.append(msg)

            before = self.restarts.get(c.name)
            self.restarts[c.name] = (c.id, c.restarts)
            if before and before[0] == c.id and c.restarts > before[1]:
                due = self.restart_bursts.feed(c.name, c.restarts - before[1], "", now)
                if due:
                    oom = " (last exit: out of memory)" if c.oom_killed else ""
                    out.append(f"[ALERT] {label} restarted {due[0]} time(s){oom}; {c.restarts} restarts in all")
        for name, _total, _began in self.restart_bursts.quiet(now):
            out.append(f"[OK] {fmt.esc(name)}: no restarts for 10 minutes")

        for name in sorted(self.seen - present):
            self.missing[name] = self.missing.get(name, 0) + 1
            if self.missing[name] == MISSING_TICKS:
                msg = self.conditions.update(
                    f"c:{name}", True, f"[ALERT] {fmt.esc(name)} is gone: no such container for {MISSING_TICKS} minutes", None
                )
                if msg:
                    out.append(msg)
        return containers

    def check_disks(self, out: list[str]) -> None:
        levels = self.state.get("disk_levels") or {}
        for path, used, total in self.host.disks(self.cfg.disk_paths):
            pct = 100.0 * used / max(total, 1)
            before = int(levels.get(path, 0))
            level = disk_level(pct, before)
            where = fmt.esc(path)
            if level > before:
                out.append(f"[ALERT] disk {where} is {pct:.0f}% full ({fmt.size(used)} of {fmt.size(total)})")
            elif level == 0 and before > 0:
                out.append(f"[OK] disk {where} is back to {pct:.0f}%")
            levels[path] = level
        self.state.set("disk_levels", levels)

    def check_memory(self, out: list[str]) -> None:
        mem = self.host.meminfo()
        total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        if not total:
            return
        pct = 100.0 * avail / total
        psi = self.host.memory_pressure()
        was_bad = self.conditions.state.get("mem") is True
        low = self.cfg.mem_low_pct + (5 if was_bad else 0)
        psi_limit = 5.0 if was_bad else 20.0
        bad = pct < low or (psi is not None and psi >= psi_limit)
        detail = f"{pct:.0f}% available ({fmt.size(avail)} of {fmt.size(total)})"
        if psi is not None:
            detail += f", stalled {psi:.0f}% of the last minute"
        msg = self.conditions.update("mem", bad, f"[ALERT] memory pressure: {detail}", f"[OK] memory: {detail}")
        if msg:
            out.append(msg)

    def _journal(self, key: str, matches: list[str], limit: int = 300) -> list[dict]:
        cursor = self.state.get(key)
        since = f"@{int(self.started)}"
        try:
            entries = self.host.journal(matches, cursor=cursor, since=None if cursor else since, limit=limit)
        except CommandError:
            if not cursor:
                raise
            # A cursor the journal no longer knows (rotated, another boot):
            # start again from when the bot started rather than fail forever.
            self.state.set(key, None)
            entries = self.host.journal(matches, cursor=None, since=since, limit=limit)
        if entries and entries[-1].get("__CURSOR"):
            self.state.set(key, entries[-1]["__CURSOR"])
        return entries

    def check_ssh(self, out: list[str]) -> None:
        logins = []
        for entry in self._journal("cursor_ssh", ["-t", "sshd", "-t", "sshd-session"], limit=2000):
            match = ACCEPTED_RE.search(entry["MESSAGE"])
            if match:
                method, user, ip, _ = match.groups()
                logins.append(f"{fmt.esc(user[:32])} from {fmt.esc(ip[:45])} ({fmt.esc(method[:20])})")
        if logins:
            shown = logins[:SSH_SHOWN]
            more = f" and {len(logins) - SSH_SHOWN} more" if len(logins) > SSH_SHOWN else ""
            out.append("[SSH] login accepted: " + "; ".join(shown) + more)

    def check_oom(self, now: float, out: list[str]) -> None:
        kills, sample = 0, ""
        for entry in self._journal("cursor_kernel", ["-k"]):
            match = OOM_RE.search(entry["MESSAGE"])
            if match:
                kills += 1
                sample = sample or match.group(1)
        due = self.oom_bursts.feed("oom", kills, sample, now)
        if due:
            out.append(f"[ALERT] out of memory: the kernel killed {fmt.esc(due[1] or 'a process')} ({due[0]} kill(s))")
        for _key, total, _ in self.oom_bursts.quiet(now):
            out.append(f"[OK] no out-of-memory kills for 10 minutes ({total} in all)")

    def check_errors(self, containers: list, now: float, out: list[str]) -> None:
        since = self.last_logs if self.last_logs is not None else now - self.cfg.watch_interval
        self.last_logs = now
        for c in containers:
            if c.status != "running" or c.label in self.cfg.error_ignore_services:
                continue
            try:
                text = self.host.logs(c.name, since=since, until=now)
            except CommandError as error:
                log.warning("logs of %s: %s", c.label, redact(str(error), 200))
                continue
            count, sample = scan_errors(text)
            due = self.error_bursts.feed(c.label, count, sample, now)
            if due:
                line = f"[ERRORS] {fmt.esc(c.label)}: {due[0]} error line(s) (Traceback / ERROR / 5xx)"
                if due[1]:
                    line += f"\n<code>{fmt.esc(due[1])}</code>"
                out.append(line)
        for key, total, began in self.error_bursts.quiet(now):
            out.append(f"[OK] {fmt.esc(key)}: no errors for 10 minutes ({total} since {fmt.stamp_time(began)})")

    # -- one round -----------------------------------------------------------

    def tick(self) -> str | None:
        """Run every check; the message to send, or None."""
        now = self.clock()
        out: list[str] = []
        containers: list = []
        for name, check in (
            ("containers", lambda: containers.extend(self.check_containers(now, out))),
            ("disk", lambda: self.check_disks(out)),
            ("memory", lambda: self.check_memory(out)),
            ("ssh", lambda: self.check_ssh(out)),
            ("oom", lambda: self.check_oom(now, out)),
            ("errors", lambda: self.check_errors(containers, now, out)),
        ):
            try:
                check()
            except Exception as error:  # noqa: BLE001 -- one broken check must not stop the others
                log.error("check %s failed: %s", name, redact(f"{type(error).__name__}: {error}", 300))
        self.state.save()
        if not out:
            return None
        return f"<b>{fmt.esc(self.cfg.host_label)}</b> · {fmt.stamp()}\n" + "\n".join(out)
