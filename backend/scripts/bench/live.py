"""The live room under a hall: a room of V phones polling its feed, and a quiz
of P players polling their screen, against a running server (serve.py).

    BENCH_DB=moa_s1 python -m scripts.bench.live setup --voters 5000 --players 2000 --out room.json
    BENCH_DB=moa_s1 python -m scripts.bench.live room --url http://127.0.0.1:8093 --room room.json \\
        --concurrency 25,100 --duration 20 --procs 3 --stats-dir /tmp/stats --server-pid 123 --out live.json
    BENCH_DB=moa_s1 python -m scripts.bench.live quiz --url ... (the same)

`setup` takes a published event of a listed club from the seeded world and
turns it into a room in full swing: it starts now; 120 questions on the board
with V voters' votes spread over them (and a twentieth of the voters asked
one), a live multiple-choice poll V/2 of them answered, ten updates, and a
running quiz of P players with scores and the open question half answered.
Only the live tables and that one event are written: the same rows on the
schema before and after.

`room` and `quiz` load it closed-loop: C clients (split over --procs
processes, so the generator is not what saturates) each sending their next
request when the last is answered, each request as one of the V phones
(X-Live-Key) or P players (X-Live-Player), for D seconds after a warm-up.
Reported per concurrency: requests a second, latency percentiles, errors,
the server's CPU and its event loop's lateness (serve.py's stats), and what
that sustains in people: a phone polls the room every 5 s (the event site's
live.jsx) and a player every 2 s (play.jsx), so a rate of R serves 5R phones
or 2R players.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import random
import secrets
import statistics
import time
import uuid

from scripts.bench import _env  # noqa: F401  (settings before app)

import asyncpg
import httpx

from scripts.bench.load import cpu_seconds, pct, reset_server, server_stats

API = "/api/v1"
ROOM_POLL_S = 5.0
QUIZ_POLL_S = 2.0
QUESTIONS = 120


def voter(i: int) -> str:
    return f"bench-voter-{i:08d}"


async def setup(db: str, voters: int, players: int) -> dict:
    conn = await asyncpg.connect(database=db, host="localhost")
    rng = random.Random(7)
    try:
        event_id = await conn.fetchval(
            "SELECT e.id FROM events e JOIN clubs c ON c.id = e.club_id WHERE NOT e.is_deleted AND e.status = 'published' "
            "AND c.is_verified AND NOT c.is_deleted AND e.event_datetime > now() ORDER BY e.id LIMIT 1")
        async with conn.transaction():
            await conn.execute("UPDATE events SET event_datetime = now() - interval '30 minutes', duration = 4, qa_approve_first = false WHERE id = $1", event_id)
            # A room run before starts clean (the children go with these by cascade).
            for t in ("live_quizzes", "live_questions", "live_polls", "live_updates"):
                await conn.execute(f"DELETE FROM {t} WHERE event_id = $1", event_id)
            now = await conn.fetchval("SELECT now()")
            # The board: 120 questions; every voter voted for a few, some asked one.
            qids = [uuid.uuid4() for _ in range(QUESTIONS)]
            await conn.copy_records_to_table(
                "live_questions", columns=["id", "event_id", "text", "author_name", "author_key", "status", "pinned", "votes", "created_at", "updated_at"],
                records=[(q, event_id, f"Question {i} from the floor about the talk?", f"Guest {i}", voter(i * (voters // QUESTIONS)), "approved",
                          i == 0, 0, now, now) for i, q in enumerate(qids)])
            votes = set()
            for v in range(voters):
                for q in rng.sample(qids, rng.randint(0, 4)):
                    votes.add((q, voter(v)))
            await conn.copy_records_to_table("live_question_votes", columns=["id", "question_id", "voter_key", "created_at"],
                                             records=[(uuid.uuid4(), q, k, now) for q, k in votes])
            await conn.execute("UPDATE live_questions q SET votes = (SELECT count(*) FROM live_question_votes v WHERE v.question_id = q.id) WHERE q.event_id = $1", event_id)
            # The poll that is up, half the room has answered.
            poll = uuid.uuid4()
            options = ["Never touched it", "A little", "Comfortable", "I dream in pointers"]
            await conn.execute(
                "INSERT INTO live_polls (id, event_id, kind, prompt, options, status, launched_at, sort_order, created_at, updated_at) "
                "VALUES ($1, $2, 'choice', 'How comfortable are you with embedded C?', $3::json, 'live', $4, 0, $4, $4)",
                poll, event_id, json.dumps(options), now)
            await conn.copy_records_to_table("live_poll_responses", columns=["id", "poll_id", "voter_key", "value", "created_at"],
                                             records=[(uuid.uuid4(), poll, voter(v), rng.choice(options), now) for v in range(0, voters, 2)])
            for i in range(10):
                await conn.execute(
                    "INSERT INTO live_updates (id, event_id, kind, text, audience, recipients, pushed, sent_at) "
                    "VALUES ($1, $2, 'general', $3, 'checked_in', 0, 0, $4::timestamptz - $5::int * interval '1 minute')",
                    uuid.uuid4(), event_id, f"Update {i}: the next talk starts in hall B", now, 10 - i)
            # The quiz: running, the fourth question open, half the players answered it.
            quiz = uuid.uuid4()
            await conn.execute(
                "INSERT INTO live_quizzes (id, event_id, title, status, join_code, current_index, question_opened_at, created_at, updated_at) "
                "VALUES ($1, $2, 'Hall quiz', 'live', $3, 3, $4, $4, $4)", quiz, event_id, f"{rng.randint(100000, 999999)}", now)
            qq = [uuid.uuid4() for _ in range(10)]
            await conn.copy_records_to_table(
                "live_quiz_questions", columns=["id", "quiz_id", "prompt", "options", "correct_key", "seconds", "sort_order", "created_at", "updated_at"],
                records=[(q, quiz, f"Quiz question {i}?", json.dumps([{"key": k, "label": k.upper()} for k in "abcd"]), "a", 3600, i, now, now) for i, q in enumerate(qq)])
            pids = [uuid.uuid4() for _ in range(players)]
            keys = [secrets.token_urlsafe(24) for _ in range(players)]
            await conn.copy_records_to_table(
                "live_quiz_players", columns=["id", "quiz_id", "name", "player_key", "score", "created_at", "updated_at"],
                records=[(p, quiz, f"Player {i}", keys[i], rng.randint(0, 3) * 900 + rng.randint(0, 99), now, now) for i, p in enumerate(pids)])
            await conn.copy_records_to_table(
                "live_quiz_answers", columns=["id", "question_id", "player_id", "option_key", "correct", "points", "answered_at", "created_at", "updated_at"],
                records=[(uuid.uuid4(), qq[3], p, "a", True, 800, now, now, now) for p in pids[::2]])
        await conn.execute("ANALYZE live_questions; ANALYZE live_question_votes; ANALYZE live_poll_responses; ANALYZE live_quiz_players; ANALYZE live_quiz_answers")
        return dict(event_id=event_id, voters=voters, players=players, player_keys=keys, questions=QUESTIONS, votes=len(votes))
    finally:
        await conn.close()


def _client_proc(args: tuple) -> dict:
    url, kind, room, concurrency, duration, warmup, seed = args
    return asyncio.run(_clients(url, kind, room, concurrency, duration, warmup, seed))


async def _clients(url: str, kind: str, room: dict, concurrency: int, duration: float, warmup: float, seed: int) -> dict:
    lat: list[float] = []
    codes: dict[str, int] = {}
    event_id = room["event_id"]
    path = f"/events/{event_id}/live" if kind == "room" else f"/events/{event_id}/live/quiz/play"
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=url + API, limits=limits, timeout=60.0) as client:
        start = time.perf_counter()

        async def one(i: int) -> None:
            r = random.Random(seed * 10_000 + i)
            while True:
                t0 = time.perf_counter()
                if t0 - start > warmup + duration:
                    return
                if kind == "room":
                    headers = {"X-Live-Key": voter(r.randrange(room["voters"]))}
                else:
                    headers = {"X-Live-Player": r.choice(room["player_keys"])}
                try:
                    resp = await client.get(path, headers=headers)
                    code = str(resp.status_code)
                except httpx.HTTPError as exc:
                    code = type(exc).__name__
                if t0 - start >= warmup:
                    lat.append(time.perf_counter() - t0)
                    codes[code] = codes.get(code, 0) + 1

        await asyncio.gather(*[one(i) for i in range(concurrency)])
    return dict(lat=lat, codes=codes)


def run_level(url: str, kind: str, room: dict, concurrency: int, duration: float, warmup: float, procs: int, server_pids: set) -> dict:
    per = [concurrency // procs + (1 if i < concurrency % procs else 0) for i in range(procs)]
    before = cpu_seconds(server_pids)
    t0 = time.perf_counter()
    with multiprocessing.get_context("spawn").Pool(procs) as pool:
        parts = pool.map(_client_proc, [(url, kind, room, n, duration, warmup, 31 + i) for i, n in enumerate(per) if n])
    wall = time.perf_counter() - t0
    after = cpu_seconds(server_pids)
    lat = [x for p in parts for x in p["lat"]]
    codes: dict[str, int] = {}
    for p in parts:
        for k, v in p["codes"].items():
            codes[k] = codes.get(k, 0) + v
    n = len(lat)
    rps = n / duration
    poll = ROOM_POLL_S if kind == "room" else QUIZ_POLL_S
    ok = codes.get("200", 0)
    return dict(
        kind=kind, concurrency=concurrency, duration_s=duration, requests=n, rps=round(rps, 1),
        people_served=int(rps * poll), errors=n - ok, codes=codes,
        latency_ms=dict(p50=round(pct(lat, 0.5) * 1000, 1), p95=round(pct(lat, 0.95) * 1000, 1), p99=round(pct(lat, 0.99) * 1000, 1),
                        mean=round(statistics.mean(lat) * 1000, 1) if lat else None),
        server_cpu_s=round(after.get("server", 0) - before.get("server", 0), 1),
        postgres_cpu_s=round(after.get("postgres", 0) - before.get("postgres", 0), 1),
        wall_s=round(wall, 1),
    )


async def _reset(url: str) -> None:
    async with httpx.AsyncClient() as client:
        await reset_server(client, url, 1)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("what", choices=["setup", "room", "quiz"])
    p.add_argument("--voters", type=int, default=5000)
    p.add_argument("--players", type=int, default=2000)
    p.add_argument("--room", help="what setup wrote")
    p.add_argument("--url", default="http://127.0.0.1:8093")
    p.add_argument("--concurrency", default="25,100")
    p.add_argument("--duration", type=float, default=20.0)
    p.add_argument("--warmup", type=float, default=3.0)
    p.add_argument("--procs", type=int, default=3)
    p.add_argument("--stats-dir", default="")
    p.add_argument("--server-pid", type=int, action="append", default=[])
    p.add_argument("--label", default="")
    p.add_argument("--out")
    args = p.parse_args()
    if args.what == "setup":
        room = asyncio.run(setup(_env.DB, args.voters, args.players))
        print(json.dumps({k: v for k, v in room.items() if k != "player_keys"}))
        if args.out:
            json.dump(room, open(args.out, "w"))
        return
    room = json.load(open(args.room))
    results = []
    for c in [int(x) for x in args.concurrency.split(",")]:
        asyncio.run(_reset(args.url))
        level = run_level(args.url, args.what, room, c, args.duration, args.warmup, args.procs, set(args.server_pid))
        if args.stats_dir:
            level["server"] = server_stats(args.stats_dir)
        print(json.dumps({k: v for k, v in level.items() if k != "server"}), flush=True)
        if args.stats_dir:
            print("  loop lag ms", level["server"]["loop_lag_ms"], flush=True)
        results.append(level)
    if args.out:
        existing = json.load(open(args.out)) if os.path.exists(args.out) else []
        existing.append(dict(label=args.label, what=args.what, voters=room["voters"], players=room["players"], levels=results))
        json.dump(existing, open(args.out, "w"), indent=1)


if __name__ == "__main__":
    main()
