"""The answers to /status, /disk and /logs, as Telegram HTML."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from . import fmt
from .redact import redact
from .system import CommandError, Container, Host


def container_state(c: Container) -> str:
    if c.status == "running":
        if c.health == "unhealthy":
            return "UNHLTH"
        if c.health == "starting":
            return "start"
        return "ok"
    if c.status == "exited":
        return f"EXIT{c.exit_code}"
    return {"restarting": "RESTRT", "paused": "PAUSED", "dead": "DEAD", "created": "CREATED"}.get(c.status, c.status[:7].upper())


def host_line(host: Host, disk_paths: tuple[str, ...]) -> str:
    parts = []
    try:
        one, five, _ = host.loadavg()
        parts.append(f"load {one:.2f} {five:.2f}/{os.cpu_count() or '?'}cpu")
    except OSError:
        pass
    try:
        mem = host.meminfo()
        total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        if total:
            parts.append(f"mem {fmt.size(total - avail)}/{fmt.size(total)} ({100 * (total - avail) // total}%)")
        swap_total = mem.get("SwapTotal", 0)
        if swap_total:
            parts.append(f"swap {fmt.size(swap_total - mem.get('SwapFree', 0))}")
    except OSError:
        pass
    for path, used, total in host.disks(disk_paths)[:2]:
        parts.append(f"{path} {100 * used // max(total, 1)}%")
    return " · ".join(parts)


def status(host: Host, *, label: str, disk_paths: tuple[str, ...], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    lines = [f"<b>{fmt.esc(label)}</b> · {fmt.stamp(now)}", fmt.esc(host_line(host, disk_paths))]
    try:
        containers = host.containers()
    except CommandError as error:
        lines.append(f"docker: {fmt.esc(redact(str(error), 300))}")
        return "\n".join(lines)
    try:
        stats = host.stats()
    except CommandError:
        stats = {}
    rows = [["svc", "state", "up", "rs", "cpu", "mem"]]
    for c in containers:
        sample = stats.get(c.name, {})
        up = fmt.duration((now - c.started_at).total_seconds()) if c.started_at and c.status == "running" else "-"
        cpu = sample.get("cpu", "-").replace("%", "")
        try:
            cpu = f"{float(cpu):.0f}%" if float(cpu) >= 10 else f"{float(cpu):.1f}%"
        except ValueError:
            cpu = "-"
        mem = fmt.compact_mem(sample["mem"]) if sample.get("mem") else (f"-/{fmt.size(c.mem_limit)}" if c.mem_limit else "-")
        rows.append([c.label[:8], container_state(c), up, str(c.restarts), cpu, mem])
    if len(rows) == 1:
        lines.append("No containers of the stack found.")
        return "\n".join(lines)
    lines.append(fmt.pre(fmt.table(rows)))
    bad = sorted(c.label for c in containers if container_state(c) not in ("ok", "start"))
    lines.append("All running." if not bad else "Needs a look: " + fmt.esc(", ".join(bad)))
    return "\n".join(lines)


def _dir_size(path: Path) -> tuple[int, int, str | None, float | None]:
    """(bytes, dumps, newest dump name, its mtime) of a backup folder."""
    total = count = 0
    newest, newest_time = None, None
    try:
        entries = list(os.scandir(path))
    except OSError:
        return 0, 0, None, None
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        total += info.st_size
        if entry.name.endswith(".dump") and not entry.name.startswith("."):
            count += 1
            if newest_time is None or info.st_mtime > newest_time:
                newest, newest_time = entry.name, info.st_mtime
    return total, count, newest, newest_time


def disk(host: Host, *, label: str, disk_paths: tuple[str, ...], backup_dir: Path, now: float | None = None) -> str:
    import time

    now = now or time.time()
    rows = [["mount", "used", "size", "use"]]
    for path, used, total in host.disks(disk_paths):
        rows.append([path, fmt.size(used), fmt.size(total), f"{100 * used // max(total, 1)}%"])
    lines = [f"<b>{fmt.esc(label)}</b> · disk", fmt.pre(fmt.table(rows))]
    try:
        df = host.docker_df()
    except CommandError as error:
        lines.append(f"docker: {fmt.esc(redact(str(error), 300))}")
        df = []
    if df:
        drows = [["docker", "count", "size", "free-able"]]
        for row in df:
            kind = {"Local Volumes": "volumes", "Build Cache": "build"}.get(row.get("Type", ""), row.get("Type", "").lower())
            reclaim = str(row.get("Reclaimable", "")).split(" ")[0]
            drows.append([kind, f"{row.get('Active', '?')}/{row.get('TotalCount', '?')}", row.get("Size", "?"), reclaim])
        lines.append(fmt.pre(fmt.table(drows)))
    total, count, newest, newest_time = _dir_size(backup_dir)
    backups = f"backups: {fmt.size(total)} in {count} dump(s) ({fmt.esc(backup_dir)})"
    if newest:
        backups += f"\nnewest: {fmt.esc(newest)}, {fmt.duration(now - newest_time)} ago"
    lines.append(backups)
    return "\n".join(lines)


def logs(host: Host, container: Container, lines: int) -> str:
    try:
        out = host.logs(container.name, tail=lines)
    except CommandError as error:
        return f"Could not read the logs: {fmt.esc(redact(str(error), 300))}"
    text = "\n".join(line[:300] for line in out.splitlines()[-lines:])
    text = redact(text, 3500) or "(no output)"
    return f"<b>{fmt.esc(container.label)}</b> · last {lines} lines\n{fmt.pre(text)}"
