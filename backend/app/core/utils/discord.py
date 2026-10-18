import asyncio
import time
from datetime import datetime, timedelta, timezone
import logging
from typing import Optional

import httpx
from fastapi import Request

from app.config import settings

logger = logging.getLogger(__name__)

#: Error reports posted at most this often, platform-wide...
MAX_REPORTS_PER_MINUTE = 10
#: ...and the same kind of error on the same path at most once in this long;
#: the next report for it says how many were left out meanwhile.
SAME_ERROR_EVERY = 300.0

_client: Optional[httpx.AsyncClient] = None
_tasks: set[asyncio.Task] = set()
_last_seen: dict[tuple[str, str], float] = {}
_suppressed: dict[tuple[str, str], int] = {}
_recent: list[float] = []


def _http() -> httpx.AsyncClient:
    """One client for every report, kept for the life of the process."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


def reset_for_tests() -> None:
    _last_seen.clear()
    _suppressed.clear()
    _recent.clear()


def report_error(request: Request, exc: Exception, track_id: str) -> bool:
    """Post the error to Discord in the background, never holding up the 500
    that is about to be answered. It used to be awaited in the handler: a new
    client each time, two tries, five-second timeouts, so a 500 took up to
    eleven seconds. At most MAX_REPORTS_PER_MINUTE a minute, and one per
    (error type, path) every SAME_ERROR_EVERY seconds, so a broken endpoint
    under load does not become a request to Discord per request. Returns
    whether a report was scheduled."""
    if not settings.DISCORD_ERROR_WEBHOOK:
        return False
    from app.core import redact

    now = time.monotonic()
    key = (type(exc).__name__, redact.path(request.url.path))
    last = _last_seen.get(key)
    while _recent and now - _recent[0] > 60:
        _recent.pop(0)
    if (last is not None and now - last < SAME_ERROR_EVERY) or len(_recent) >= MAX_REPORTS_PER_MINUTE:
        _suppressed[key] = _suppressed.get(key, 0) + 1
        return False
    _last_seen[key] = now
    _recent.append(now)
    if len(_last_seen) > 1000:
        for stale in [k for k, t in _last_seen.items() if now - t > SAME_ERROR_EVERY]:
            _last_seen.pop(stale, None)
    left_out = _suppressed.pop(key, 0)
    try:
        task = asyncio.get_running_loop().create_task(notify_error(request, exc, track_id, left_out=left_out))
    except RuntimeError:
        return False
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def notify_error(request: Request, exc: Exception, track_id: str, left_out: int = 0):
    """Post one error report. Never raises."""
    if not settings.DISCORD_ERROR_WEBHOOK:
        return
    from app.core import redact

    # Never a club webhook's whole token, and never the message of an error
    # on a route whose body holds a Razorpay key secret (app/core/redact.py).
    error_details = {
        "track_id": track_id,
        "path": redact.path(request.url.path),
        "method": request.method,
        "traceback": redact.error_traceback(request.url.path, exc),
    }
    if left_out:
        error_details["left_out"] = left_out

    try:
        await send_discord_notification(error_details)
    except Exception:  # noqa: BLE001
        logger.warning("Discord error report failed", exc_info=True)


async def send_discord_notification(
    error_details: dict, max_retries: int = 2, retry_delay: float = 1.0
):
    """Post the report as a traceback.txt attachment, with one retry, on the
    shared client."""
    if not settings.DISCORD_ERROR_WEBHOOK:
        return
    timestamp = (
        datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()
    )
    error_content = f"""
============================ ERROR DETAILS ============================

Timestamp: {timestamp}
Track ID: {error_details.get("track_id", "N/A")}
Path: {error_details.get("path", "N/A")}
Method: {error_details.get("method", "N/A")}
Same error left out since the last report: {error_details.get("left_out", 0)}

============================== TRACEBACK ==============================

{error_details.get("traceback", "")}

=======================================================================
"""
    files = {"file": ("traceback.txt", error_content.encode("utf-8"), "text/plain")}
    client = _http()
    for attempt in range(max_retries):
        try:
            response = await client.post(settings.DISCORD_ERROR_WEBHOOK, files=files)
            response.raise_for_status()
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Discord notification attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(retry_delay * (2**attempt))

    logger.warning("Failed to send Discord error notification after all retries")
