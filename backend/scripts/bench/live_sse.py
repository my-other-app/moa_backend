"""The live room pushed against the live room polled: N phones on one room,
a host acting on it, a running server (serve.py with APP_LIVE_SSE=true).

    createdb moa_bench_sse
    BENCH_DB=moa_bench_sse python -m scripts.bench.live_sse setup --out room.json
    APP_LIVE_SSE=true [APP_REDIS_URL=redis://localhost:6379/12] BENCH_DB=moa_bench_sse \\
        python -m scripts.bench.serve --workers 1 --port 8095 &
    BENCH_DB=moa_bench_sse python -m scripts.bench.live_sse run --url http://127.0.0.1:8095 --room room.json \\
        --mode sse --clients 1000 --procs 4 --actions 10 --server-pid <uvicorn's pid> --label "sse, 1 worker" --out live_sse.json
    ... --mode poll (the same phones polling the feed every 5 s, as live.jsx does)
    dropdb moa_bench_sse

`setup` makes a schema (create_all) and one room in full swing: a listed club,
an event under way with the floor open, 120 questions on the board with
votes from 2,000 browsers, a live poll half of them answered, ten updates
(a 30 KB snapshot, like the room measured in RESULTS.md).

`run` opens N clients, split over --procs processes (one address each, as
phones behind a few NATs), each a browser of its own (X-Live-Key):

- `sse`: one stream each (`view=room`), every snapshot timed on arrival;
- `poll`: GET the feed every 5 s each, at a random phase.

The clients connect in two waves, a fifth and then the rest, and the
server's memory (uvicorn and its workers: macOS's physical footprint, RSS
elsewhere) is read after each: the difference over the second wave is the
memory a connection costs. Then its CPU over a quiet window (SSE: pings
only), then the host acts --actions times, --gap
seconds apart: each action pins another question (a moderation, told at
once). For each action and each client, the time from just before the
host's request to the first body the client got that shows that question
pinned; reported as p50 / p99 over every client and action, and the time to
the *last* client per action (p50 / p99 over actions). Server CPU per
second, per 1,000 clients, quiet and while acting.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import random
import statistics
import subprocess
import time
import uuid

from scripts.bench import _env  # noqa: F401  (settings before app)

import httpx

from scripts.bench.load import cpu_seconds, pct

API = "/api/v1"
POLL_S = 5.0
QUESTIONS = 120
VOTERS = 2000


# ─── The room ────────────────────────────────────────────────────────────


async def setup(db: str) -> dict:
    import asyncpg
    from sqlalchemy import create_engine

    from app.db import registry  # noqa: F401  (every model on the metadata)
    from app.db.base import AbstractSQLModel

    sync = create_engine(f"postgresql://localhost:5432/{db}")
    AbstractSQLModel.metadata.create_all(sync)
    sync.dispose()
    # The club, its owner and the event through the models (their defaults).
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.clubs.models import Clubs
    from app.api.events.models import EventCategories, Events
    from app.api.users.models import Users, UserTypes

    engine = create_async_engine(f"postgresql+asyncpg://localhost:5432/{db}")
    tag = uuid.uuid4().hex[:8]
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        owner = Users(full_name="Bench Club", username=f"bench-{tag}", email=f"bench-{tag}@example.com", password="x", user_type=UserTypes.club)
        s.add(owner)
        await s.flush()
        club = Clubs(slug=f"bench-society-{tag}", name="Bench Society", user_id=owner.id, is_verified=True)
        s.add(club)
        await s.flush()
        category = EventCategories(name="General", created_by_id=owner.id)
        s.add(category)
        await s.flush()
        ev = Events(slug=f"bench-night-{tag}", name="Bench Night", images=[], event_datetime=datetime.now(timezone.utc) - timedelta(minutes=30),
                    duration=4.0, club_id=club.id, category_id=category.id, qa_approve_first=False)
        s.add(ev)
        await s.commit()
        owner_id, event = owner.id, ev.id
    await engine.dispose()
    conn = await asyncpg.connect(database=db, host="localhost")
    rng = random.Random(7)
    owner = owner_id
    try:
        async with conn.transaction():
            now = await conn.fetchval("SELECT now()")
            qids = [uuid.uuid4() for _ in range(QUESTIONS)]
            await conn.copy_records_to_table(
                "live_questions", columns=["id", "event_id", "text", "author_name", "author_key", "status", "pinned", "votes", "created_at", "updated_at"],
                records=[(q, event, f"Question {i} from the floor about the talk?", f"Guest {i}", f"bench-asker-{i:06d}", "approved", False, 0, now, now)
                         for i, q in enumerate(qids)])
            votes = {(q, f"bench-voter-{v:08d}") for v in range(VOTERS) for q in rng.sample(qids, rng.randint(0, 4))}
            await conn.copy_records_to_table("live_question_votes", columns=["id", "question_id", "voter_key", "created_at"],
                                             records=[(uuid.uuid4(), q, k, now) for q, k in votes])
            await conn.execute("UPDATE live_questions q SET votes = (SELECT count(*) FROM live_question_votes v WHERE v.question_id = q.id) WHERE q.event_id = $1", event)
            poll = uuid.uuid4()
            options = ["Never touched it", "A little", "Comfortable", "I dream in pointers"]
            await conn.execute(
                "INSERT INTO live_polls (id, event_id, kind, prompt, options, status, launched_at, sort_order, created_at, updated_at) "
                "VALUES ($1, $2, 'choice', 'How comfortable are you with embedded C?', $3::json, 'live', $4, 0, $4, $4)", poll, event, json.dumps(options), now)
            await conn.copy_records_to_table("live_poll_responses", columns=["id", "poll_id", "voter_key", "value", "created_at"],
                                             records=[(uuid.uuid4(), poll, f"bench-voter-{v:08d}", rng.choice(options), now) for v in range(0, VOTERS, 2)])
            for i in range(10):
                await conn.execute(
                    "INSERT INTO live_updates (id, event_id, kind, text, audience, recipients, pushed, sent_at) "
                    "VALUES ($1, $2, 'general', $3, 'checked_in', 0, 0, $4::timestamptz - $5::int * interval '1 minute')",
                    uuid.uuid4(), event, f"Update {i}: the next talk starts in hall B", now, 10 - i)
        await conn.execute("ANALYZE")
        return {"event_id": event, "owner_id": owner, "questions": [str(q) for q in qids]}
    finally:
        await conn.close()


def token_for(owner_id: int) -> str:
    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token

    return create_access_token(data=AuthTokenData(user_id=owner_id, token_type="access_token", token_version=0).model_dump())


# ─── The phones ──────────────────────────────────────────────────────────


_FIRST = b'"questions":[{"id":"'


def _first_question(body: bytes) -> str | None:
    """The id of the feed's first question: the pinned one once the host has
    pinned it (the feed orders pinned first). Found by its place in the
    bytes, not by parsing 30 KB of JSON per phone per change: the load
    generator must not be what is measured."""
    i = body.find(_FIRST)
    return body[i + len(_FIRST):i + len(_FIRST) + 36].decode() if i >= 0 else None


def _phones(url: str, event_id: int, mode: str, n: int, first: int, proc: int, connected, go, stop, results) -> None:
    results.put(asyncio.run(_phones_async(url, event_id, mode, n, first, proc, connected, go, stop)))


async def _phones_async(url: str, event_id: int, mode: str, n: int, first_wave: int, proc: int, connected, go, stop) -> dict:
    """n phones in this process, the first `first_wave` of them before `go`;
    each records (arrival ns, pinned question) per body."""
    address = f"49.36.{100 + proc}.{proc + 1}"
    seen: list[list] = [[] for _ in range(n)]
    errors: dict[str, int] = {}
    limits = httpx.Limits(max_connections=n + 10, max_keepalive_connections=n + 10)
    timeout = httpx.Timeout(60.0, read=None if mode == "sse" else 60.0)
    async with httpx.AsyncClient(base_url=url + API, limits=limits, timeout=timeout) as client:

        host, port = url.split("://", 1)[1].split(":")

        async def stream(i: int) -> None:
            # A bare HTTP/1.1 client: the request written once, the response
            # read as it comes. Each frame is one chunk of the chunked body,
            # so frames are found by their blank line, chunk sizes and all.
            request = (
                f"GET {API}/events/{event_id}/live/stream?view=room HTTP/1.1\r\nHost: {host}\r\nAccept: text/event-stream\r\n"
                f"X-Live-Key: bench-phone-{proc:02d}-{i:06d}\r\nX-Forwarded-For: {address}\r\n\r\n"
            ).encode()
            first = True
            while not stop.is_set():
                writer = None
                try:
                    reader, writer = await asyncio.open_connection(host, int(port))
                    writer.write(request)
                    head = await reader.readuntil(b"\r\n\r\n")
                    if not head.startswith(b"HTTP/1.1 200"):
                        code = head[9:12].decode()
                        errors[code] = errors.get(code, 0) + 1
                        writer.close()
                        await asyncio.sleep(1)
                        continue
                    buf = b""
                    while not stop.is_set():
                        chunk = await reader.read(262144)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n\n" in buf:
                            raw, buf = buf.split(b"\n\n", 1)
                            if b"event: snapshot" in raw:
                                seen[i].append((time.time_ns(), _first_question(raw)))
                                if first:
                                    first = False
                                    with connected.get_lock():
                                        connected.value += 1
                except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
                    errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
                    await asyncio.sleep(1)
                finally:
                    if writer is not None:
                        writer.close()

        async def poll(i: int) -> None:
            headers = {"X-Live-Key": f"bench-phone-{proc:02d}-{i:06d}", "X-Forwarded-For": address}
            await asyncio.sleep(random.uniform(0, POLL_S))
            first = True
            while not stop.is_set():
                t0 = time.monotonic()
                try:
                    r = await client.get(f"/events/{event_id}/live", headers=headers)
                    if r.status_code == 200:
                        seen[i].append((time.time_ns(), _first_question(r.content)))
                        if first:
                            first = False
                            with connected.get_lock():
                                connected.value += 1
                    else:
                        errors[str(r.status_code)] = errors.get(str(r.status_code), 0) + 1
                except (httpx.HTTPError, OSError) as exc:
                    errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
                await asyncio.sleep(max(0.0, POLL_S - (time.monotonic() - t0)))

        async def watch_stop(tasks) -> None:
            parent = os.getppid()
            # Gone with the run that started it, even one killed outright.
            while not stop.is_set() and os.getppid() == parent:
                await asyncio.sleep(0.2)
            for t in tasks:
                t.cancel()

        tasks = []
        for i in range(n):
            if i == first_wave:
                while not go.is_set():
                    await asyncio.sleep(0.1)
            tasks.append(asyncio.create_task((stream if mode == "sse" else poll)(i)))
            if mode == "sse" and i % 50 == 49:
                await asyncio.sleep(0.05)  # a hall arriving, not one instant
        await asyncio.gather(watch_stop(tasks), *tasks, return_exceptions=True)
    return {"seen": seen, "errors": errors}


# ─── The server ──────────────────────────────────────────────────────────


def _family(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    rows = [tuple(int(x) for x in line.split()) for line in out.splitlines() if line.strip()]
    return [pid] + [p for p, pp in rows if pp == pid]


def memory_mb(pid: int) -> float:
    """What the server's processes hold. On macOS its physical footprint
    (dirty pages, compressed ones included: RSS leaves out what the
    compressor took, which on a busy laptop is most of an idle process);
    elsewhere RSS."""
    import re
    import shutil

    total = 0.0
    for p in _family(pid):
        if shutil.which("footprint"):
            out = subprocess.run(["footprint", str(p)], capture_output=True, text=True).stdout
            m = re.search(r"Footprint:\s*([\d.]+)\s*(KB|MB|GB)", out)
            if m:
                total += float(m.group(1)) * {"KB": 1 / 1024, "MB": 1, "GB": 1024}[m.group(2)]
                continue
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(p)], capture_output=True, text=True).stdout
        total += sum(int(x) for x in out.split()) / 1024
    return total


def server_cpu(pid: int) -> float:
    return cpu_seconds({pid}).get("server", 0.0)


async def act(url: str, event_id: int, token: str, question: str) -> int:
    async with httpx.AsyncClient(base_url=url + API, timeout=30) as client:
        t = time.time_ns()
        r = await client.post(f"/events/{event_id}/live/questions/{question}", json={"action": "pin"}, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return t


def run(args) -> dict:
    room = json.load(open(args.room))
    ctx = multiprocessing.get_context("spawn")
    connected = ctx.Value("i", 0)
    go = ctx.Event()
    stop = ctx.Event()
    per = [args.clients // args.procs + (1 if i < args.clients % args.procs else 0) for i in range(args.procs)]
    # Memory per connection is read as a slope: after a first fifth, and after the rest.
    firsts = [max(1, n // 5) for n in per]
    token = token_for(room["owner_id"])
    # The first question pinned before anyone connects, so every action changes the room.
    asyncio.run(act(args.url, room["event_id"], token, room["questions"][-1]))
    time.sleep(1.5)
    cpu0 = server_cpu(args.server_pid)
    t_open = time.monotonic()
    results = ctx.Queue()
    procs = [ctx.Process(target=_phones, args=(args.url, room["event_id"], args.mode, n, f, p, connected, go, stop, results))
             for p, (n, f) in enumerate(zip(per, firsts))]
    for proc in procs:
        proc.start()
    ends = time.monotonic() + args.connect_timeout
    while connected.value < sum(firsts) and time.monotonic() < ends:
        time.sleep(0.25)
    time.sleep(3.0)
    n_a, rss0 = connected.value, memory_mb(args.server_pid)
    go.set()
    while connected.value < args.clients and time.monotonic() < ends:
        time.sleep(0.25)
    opened_in = time.monotonic() - t_open
    connected_n = connected.value
    time.sleep(3.0)
    rss1 = memory_mb(args.server_pid)
    # Quiet: SSE sends pings only; polls keep polling.
    q0, tq0 = server_cpu(args.server_pid), time.monotonic()
    time.sleep(args.quiet)
    q1, tq1 = server_cpu(args.server_pid), time.monotonic()
    actions = []
    for k in range(args.actions):
        q = room["questions"][k % (len(room["questions"]) - 1)]
        actions.append((asyncio.run(act(args.url, room["event_id"], token, q)), q))
        time.sleep(args.gap)
    a1, ta1 = server_cpu(args.server_pid), time.monotonic()
    stop.set()
    parts = [results.get(timeout=120) for _ in procs]
    for proc in procs:
        proc.join(timeout=30)
    seen = [s for p in parts for s in p["seen"]]
    errors: dict[str, int] = {}
    for p in parts:
        for k, v in p["errors"].items():
            errors[k] = errors.get(k, 0) + v
    lat: list[float] = []
    last: list[float] = []
    each: list[list[float]] = []
    missed = 0
    by_phone: list[list] = []
    for t_k, q in actions:
        per_action = []
        row = []
        for records in seen:
            got = next((at for at, pinned in records if at >= t_k and pinned == q), None)
            if got is None:
                missed += 1
                row.append(None)
            else:
                per_action.append((got - t_k) / 1e6)
                row.append(round((got - t_k) / 1e6, 1))
        by_phone.append(row)
        lat += per_action
        if per_action:
            last.append(max(per_action))
            each.append([round(min(per_action), 1), round(pct(per_action, 0.5), 1), round(max(per_action), 1)])
    quiet_rate = (q1 - q0) / (tq1 - tq0)
    acting_rate = (a1 - q1) / (ta1 - tq1)
    k = 1000 / max(1, connected_n)
    out = dict(
        label=args.label, mode=args.mode, clients=args.clients, connected=connected_n, opened_in_s=round(opened_in, 1),
        server_memory_mb=dict(at_first_fifth=round(rss0, 1), at_all=round(rss1, 1)),
        kb_per_connection=round((rss1 - rss0) * 1024 / max(1, connected_n - n_a), 1),
        server_cpu_per_s_per_1000=dict(quiet=round(quiet_rate * k, 3), acting=round(acting_rate * k, 3)),
        server_cpu_s=dict(open=round(q0 - cpu0, 2), quiet=round(q1 - q0, 2), acting=round(a1 - q1, 2)),
        latency_ms=dict(p50=round(pct(lat, 0.5), 1), p99=round(pct(lat, 0.99), 1), max=round(max(lat), 1) if lat else None),
        to_last_client_ms=dict(p50=round(pct(last, 0.5), 1), p99=round(pct(last, 0.99), 1), mean=round(statistics.mean(last), 1) if last else None),
        actions=len(actions), missed=missed, errors=errors, per_action_min_p50_max_ms=each,
    )
    if args.dump:
        json.dump({"per": per, "latency_ms_by_action_and_phone": by_phone}, open(args.dump, "w"))
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("what", choices=["setup", "run"])
    p.add_argument("--room")
    p.add_argument("--out")
    p.add_argument("--url", default="http://127.0.0.1:8095")
    p.add_argument("--mode", choices=["sse", "poll"], default="sse")
    p.add_argument("--clients", type=int, default=1000)
    p.add_argument("--procs", type=int, default=4)
    p.add_argument("--actions", type=int, default=10)
    p.add_argument("--gap", type=float, default=2.0)
    p.add_argument("--quiet", type=float, default=20.0)
    p.add_argument("--connect-timeout", type=float, default=120.0)
    p.add_argument("--server-pid", type=int)
    p.add_argument("--label", default="")
    p.add_argument("--dump", help="every phone's latency per action, as JSON")
    args = p.parse_args()
    if args.what == "setup":
        room = asyncio.run(setup(_env.DB))
        print(json.dumps({k: v for k, v in room.items() if k != "questions"}))
        json.dump(room, open(args.out, "w"))
        return
    result = run(args)
    print(json.dumps(result), flush=True)
    if args.out:
        existing = json.load(open(args.out)) if os.path.exists(args.out) else []
        existing.append(result)
        json.dump(existing, open(args.out, "w"), indent=1)


if __name__ == "__main__":
    main()
