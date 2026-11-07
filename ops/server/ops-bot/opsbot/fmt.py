"""Formatting for a phone: sizes, durations, India time, monospace tables."""

from __future__ import annotations

import html
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), "IST")


def esc(text: object) -> str:
    return html.escape(str(text), quote=False)


def pre(text: str) -> str:
    return f"<pre>{esc(text)}</pre>"


def size(n: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(n) < 1024 or unit == "T":
            if unit == "B":
                return f"{int(n)}B"
            return f"{n:.1f}{unit}" if abs(n) < 10 else f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.0f}T"


def mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(max(seconds, 0))
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def now_ist(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(IST)


def stamp(now: datetime | None = None) -> str:
    return now_ist(now).strftime("%d %b %H:%M IST")


def table(rows: list[list[str]]) -> str:
    """Left-aligned columns, two spaces apart."""
    if not rows:
        return ""
    widths = [max(len(str(row[i])) for row in rows if i < len(row)) for i in range(max(len(r) for r in rows))]
    lines = []
    for row in rows:
        cells = [str(cell).ljust(widths[i]) for i, cell in enumerate(row)]
        lines.append(" ".join(cells).rstrip())
    return "\n".join(lines)


def compact_mem(usage: str) -> str:
    """docker stats' '512.3MiB / 2GiB' -> '512M/2G'."""
    parts = [p.strip() for p in usage.split("/")]

    def one(part: str) -> str:
        part = part.replace("iB", "").replace("B", "")
        number = "".join(ch for ch in part if ch.isdigit() or ch == ".")
        unit = part[len(number) :].strip()[:1]
        try:
            value = float(number)
        except ValueError:
            return part
        text = f"{value:.1f}" if value < 10 and value != int(value) else f"{value:.0f}"
        return text + unit

    return "/".join(one(p) for p in parts if p)


def stamp_time(epoch: float) -> str:
    """'03:02 IST' for a Unix time."""
    return datetime.fromtimestamp(epoch, IST).strftime("%H:%M IST")
