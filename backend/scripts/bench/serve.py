"""The app under uvicorn, against the benchmark database, the way production
runs it (`python main.py`: uvicorn.run("doth:application", workers=WORKERS),
access log on), for the load and write tests (load.py, hot.py).

    BENCH_DB=moa_s1 python -m scripts.bench.serve --workers 4 --port 8077 --stats-dir /tmp/stats > server.log 2>&1

What differs from production, and why:

- Nothing leaves the machine. Mail sends (`send_email`,
  `send_email_with_attachment`) are replaced by a sleep in the worker thread
  they already run in (--mail-ms, 80 ms: about what an SES call takes), and
  the ticket PDF, which production renders with wkhtmltopdf in a subprocess
  *on the event loop* (events/tickets.py: render_ticket_pdf is synchronous
  and ticket_document calls it directly), is replaced by a blocking sleep of
  --ticket-pdf-ms (0 by default; wkhtmltopdf is not installed here). Set it to
  see what a registration spike does to everyone else.
- Each worker writes its own numbers to --stats-dir every second: how late
  the event loop wakes (a 50 ms sleep, measured), the connection pool's
  checked-out and overflow counts, and how long each `SELECT … FOR UPDATE`
  took (the wait for the row lock is inside it).

The app itself is imported unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

from scripts.bench import _env  # noqa: F401  (settings before app)

STATS_DIR = os.environ.get("BENCH_STATS_DIR", "")
TICKET_PDF_MS = float(os.environ.get("BENCH_TICKET_PDF_MS", "0"))
MAIL_MS = float(os.environ.get("BENCH_MAIL_MS", "80"))


def _patch() -> None:
    from app.core.email import email as email_mod

    def fake_send(*_a, **_k):
        time.sleep(MAIL_MS / 1000)  # in the thread asyncio.to_thread gave it, as the boto3 call would be
        return {"MessageId": "bench"}

    email_mod.send_email = fake_send
    email_mod.send_email_with_attachment = fake_send
    email_mod.send_email_background = lambda *a, **k: None

    from app.api.events import tickets

    def fake_pdf(registration, style) -> bytes:
        if TICKET_PDF_MS:
            time.sleep(TICKET_PDF_MS / 1000)  # wkhtmltopdf, synchronous, on the loop
        return b"%PDF-1.4 bench"

    tickets.render_ticket_pdf = fake_pdf


_patch()

from doth import application as _app  # noqa: E402
from app.db.core import engine  # noqa: E402

_lags: list[float] = []
_locks: list[float] = []
_state = dict(requests=0, inflight=0, max_inflight=0, started=False)


def _listen() -> None:
    from sqlalchemy import event

    # Per copy of this module: run as __main__ with one worker, uvicorn imports it a second time.
    key = f"_bench_lock_t0_{id(_locks)}"

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement:
            conn.info[key] = time.perf_counter()

    @event.listens_for(engine.sync_engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        t0 = conn.info.pop(key, None)
        if t0 is not None:
            _locks.append(time.perf_counter() - t0)


_listen()


async def _monitor() -> None:
    """How late a 50 ms sleep wakes: the time the loop was busy with something else."""
    last_dump = time.monotonic()
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(0.05)
        _lags.append(time.monotonic() - t0 - 0.05)
        if STATS_DIR and time.monotonic() - last_dump >= 1.0:
            last_dump = time.monotonic()
            _dump()


def _dump() -> None:
    pool = engine.sync_engine.pool
    data = dict(
        pid=os.getpid(), at=time.time(), requests=_state["requests"], inflight=_state["inflight"], max_inflight=_state["max_inflight"],
        pool_checked_out=pool.checkedout(), pool_overflow=pool.overflow(), pool_size=pool.size(),
        lags=_lags[-2000:], locks=_locks[-5000:],
    )
    tmp = os.path.join(STATS_DIR, f".{os.getpid()}.json")
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, os.path.join(STATS_DIR, f"{os.getpid()}.json"))


async def application(scope, receive, send):
    if scope["type"] == "http":
        if not _state["started"]:
            _state["started"] = True
            asyncio.get_running_loop().create_task(_monitor())
        if scope["path"] == "/__bench/reset":
            _lags.clear()
            _locks.clear()
            _state["requests"] = 0
            _state["max_inflight"] = 0
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        _state["requests"] += 1
        _state["inflight"] += 1
        _state["max_inflight"] = max(_state["max_inflight"], _state["inflight"])
        try:
            await _app(scope, receive, send)
        finally:
            _state["inflight"] -= 1
        return
    await _app(scope, receive, send)


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--port", type=int, default=8077)
    p.add_argument("--stats-dir", default="")
    p.add_argument("--ticket-pdf-ms", type=float, default=0.0)
    p.add_argument("--mail-ms", type=float, default=80.0)
    p.add_argument("--no-access-log", action="store_true")
    args = p.parse_args()
    if args.stats_dir:
        os.makedirs(args.stats_dir, exist_ok=True)
    # Read by every worker when it imports this module.
    os.environ["BENCH_STATS_DIR"] = args.stats_dir
    os.environ["BENCH_TICKET_PDF_MS"] = str(args.ticket_pdf_ms)
    os.environ["BENCH_MAIL_MS"] = str(args.mail_ms)
    uvicorn.run("scripts.bench.serve:application", host="127.0.0.1", port=args.port, workers=args.workers,
                access_log=not args.no_access_log, log_level="info")


if __name__ == "__main__":
    main()
