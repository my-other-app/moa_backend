"""What the bot reads from the box: Docker (the CLI), the journal, /proc.

Every command is an argument list (no shell), with a timeout, and its
output is capped. Nothing here writes anything except `start_unit`, which
asks systemd to start the backup unit. Tests replace `Runner.run`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

OUTPUT_CAP = 4 * 1024 * 1024

#: A compose service name, as /logs accepts it.
SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


class CommandError(Exception):
    pass


class Runner:
    def run(self, argv: list[str], timeout: float = 30) -> str:
        """stdout+stderr of `argv`; CommandError when it fails or times out."""
        try:
            done = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise CommandError(f"{argv[0]} is not installed") from None
        except subprocess.TimeoutExpired:
            raise CommandError(f"{argv[0]} {argv[1] if len(argv) > 1 else ''} timed out after {timeout:.0f}s") from None
        out = done.stdout[-OUTPUT_CAP:].decode("utf-8", errors="replace")
        if done.returncode != 0:
            raise CommandError(f"{' '.join(argv[:2])} exited {done.returncode}: {out.strip()[-300:]}")
        return out


@dataclass
class Container:
    id: str
    name: str
    service: str
    project: str
    status: str  # running, exited, restarting, paused, dead, created
    health: str  # healthy, unhealthy, starting, "" (no healthcheck)
    restarts: int
    started_at: datetime | None
    exit_code: int
    oom_killed: bool
    mem_limit: int  # bytes, 0 = none

    @property
    def label(self) -> str:
        return self.service or self.name


def _parse_time(raw: str) -> datetime | None:
    if not raw or raw.startswith("0001-"):
        return None
    match = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)?", raw)
    if not match:
        return None
    stamp = datetime.fromisoformat(match.group(1))
    zone = match.group(2) or "Z"
    if zone == "Z":
        return stamp.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(match.group(1) + zone)


def parse_inspect(raw: str) -> list[Container]:
    containers = []
    for item in json.loads(raw or "[]"):
        state = item.get("State") or {}
        labels = (item.get("Config") or {}).get("Labels") or {}
        health = (state.get("Health") or {}).get("Status", "")
        containers.append(
            Container(
                id=item.get("Id", "")[:12],
                name=item.get("Name", "").lstrip("/"),
                service=labels.get("com.docker.compose.service", ""),
                project=labels.get("com.docker.compose.project", ""),
                status=state.get("Status", "unknown"),
                health=health,
                restarts=int(item.get("RestartCount") or 0),
                started_at=_parse_time(state.get("StartedAt", "")),
                exit_code=int(state.get("ExitCode") or 0),
                oom_killed=bool(state.get("OOMKilled")),
                mem_limit=int((item.get("HostConfig") or {}).get("Memory") or 0),
            )
        )
    containers.sort(key=lambda c: c.label)
    return containers


class Host:
    """The box, read through `runner`."""

    def __init__(self, runner: Runner | None = None, *, project: str = "moa"):
        self.runner = runner or Runner()
        self.project = project

    # -- Docker ------------------------------------------------------------

    def containers(self) -> list[Container]:
        """The stack's long-running containers (not `run --rm` one-offs)."""
        argv = ["docker", "ps", "-aq", "--no-trunc", "--filter", "label=com.docker.compose.oneoff=False"]
        if self.project:
            argv += ["--filter", f"label=com.docker.compose.project={self.project}"]
        ids = self.runner.run(argv, timeout=20).split()
        if not ids:
            return []
        return parse_inspect(self.runner.run(["docker", "inspect", *ids], timeout=20))

    def stats(self) -> dict[str, dict[str, str]]:
        """Container name -> {cpu, mem} from one `docker stats` sample."""
        out = self.runner.run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=30)
        stats = {}
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            stats[item.get("Name", "")] = {"cpu": item.get("CPUPerc", "?"), "mem": item.get("MemUsage", "?")}
        return stats

    def logs(self, container: str, *, tail: int | None = None, since: float | None = None, until: float | None = None) -> str:
        argv = ["docker", "logs"]
        if tail is not None:
            argv += ["--tail", str(int(tail))]
        if since is not None:
            argv += ["--since", f"{since:.3f}"]
        if until is not None:
            argv += ["--until", f"{until:.3f}"]
        argv.append(container)
        return self.runner.run(argv, timeout=30)

    def docker_df(self) -> list[dict[str, str]]:
        out = self.runner.run(["docker", "system", "df", "--format", "{{json .}}"], timeout=60)
        rows = []
        for line in out.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows

    # -- journal -----------------------------------------------------------

    def journal(self, matches: list[str], *, cursor: str | None, since: str | None = None, limit: int = 300) -> list[dict]:
        """Journal entries after `cursor` (or since `since`), oldest first."""
        argv = ["journalctl", "--no-pager", "-q", "-o", "json", "-n", str(limit), *matches]
        if cursor:
            argv += ["--after-cursor", cursor]
        elif since:
            argv += ["--since", since]
        out = self.runner.run(argv, timeout=30)
        entries = []
        for line in out.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("MESSAGE", "")
            if isinstance(message, list):  # not UTF-8: journald gives bytes
                message = bytes(b & 0xFF for b in message).decode("utf-8", errors="replace")
            entry["MESSAGE"] = str(message)
            entries.append(entry)
        return entries

    # -- systemd -----------------------------------------------------------

    def unit_state(self, unit: str) -> str:
        try:
            return self.runner.run(["systemctl", "is-active", unit], timeout=15).strip()
        except CommandError as error:
            # is-active exits non-zero for inactive/failed; the word is the output.
            text = str(error).rsplit(":", 1)[-1].strip()
            return text.split()[-1] if text else "unknown"

    def start_unit(self, unit: str) -> None:
        self.runner.run(["systemctl", "start", "--no-block", unit], timeout=30)

    # -- /proc and the filesystem -----------------------------------------

    @staticmethod
    def meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
        """Bytes, by /proc/meminfo's names."""
        values = {}
        with open(path) as handle:
            for line in handle:
                name, _, rest = line.partition(":")
                parts = rest.split()
                if parts and parts[0].isdigit():
                    values[name] = int(parts[0]) * (1024 if len(parts) > 1 else 1)
        return values

    @staticmethod
    def memory_pressure(path: str = "/proc/pressure/memory") -> float | None:
        """PSI 'full avg60' (% of the last minute all tasks stalled on memory)."""
        try:
            with open(path) as handle:
                for line in handle:
                    if line.startswith("full"):
                        match = re.search(r"avg60=([\d.]+)", line)
                        return float(match.group(1)) if match else None
        except OSError:
            return None
        return None

    @staticmethod
    def loadavg() -> tuple[float, float, float]:
        return os.getloadavg()

    @staticmethod
    def disks(paths: tuple[str, ...]) -> list[tuple[str, int, int]]:
        """(path, used, size) for each existing path, one per filesystem; used
        over size is df's Use%."""
        seen = set()
        rows = []
        for path in paths:
            try:
                device = os.stat(path).st_dev
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            if device in seen:
                continue
            seen.add(device)
            rows.append((path, usage.used, usage.used + usage.free))
        return rows
