"""The money ledger at scale: a world with money in it, the ledger backfilled
from it, and the finance and revenue reads timed both ways.

    createdb moa_ledger_bench
    BENCH_DB=moa_ledger_bench python -m scripts.bench.seed --clubs 2000 --upcoming 15000 --past 30000 \\
        --users 150000 --registrations 1000000 --follows 10000 --threads 1000 --likes 1000 --comments 1000
    BENCH_DB=moa_ledger_bench python -m scripts.bench.ledger money [--charges 1000000]
    BENCH_DB=moa_ledger_bench python -m scripts.bench.ledger backfill
    BENCH_DB=moa_ledger_bench python -m scripts.bench.ledger measure --out results.json

`money` turns the seeded paid registrations into money as the app makes it
(set-based SQL, triggers off while loading, the counters put right after):
of a paid event's registrations, 85% paid online (an order, a captured
payment, a tenth of them with a store order riding on it), 10% paid at the
desk, 3% paid and refunded, 2% never paid; and `--charges` subscription
charges across 100,000 subscriptions, ten months each (2% refunded, 1% in
part, 1% failed).

`megaclub` adds one club's history at the scale that hurts its finance
page: --events paid events of --per registrations each (400 x 2,500 = 10
lakh registrations for one club), with the same money as above; then
`backfill --since <the time it printed>` posts only what is new.

`backfill` runs the real backfill (app.api.ledger.backfill), timed.

`measure` checks the ledger against the rows for the biggest clubs (the
compare), then times each read both ways -- the club finance page over the
biggest club's whole history and over the default period, the biggest
event's page, the admin revenue page -- best of REPEAT, and EXPLAIN
ANALYZEs the queries behind them. On the seeded world the compare lists one
kind of difference only: the seed soft-deletes some paid registrations, and
the ledger keeps the money they brought in where the rows forget it (a known
difference, app/api/ledger/compare.py).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from datetime import datetime, timezone

from scripts.bench import _env  # noqa: F401  (settings before app; no admin mail)

from sqlalchemy import create_engine, text

from app.api.admin_alerts import service as _alerts
from app.config import settings

# Belt and braces: whatever the environment says, nothing here mails or posts.
_alerts.deliver = lambda alert: None
assert not settings.ADMIN_ALERT_EMAILS

REPEAT = 5

MONEY_SQL = [
    "SET session_replication_role = replica",
    """
    CREATE TEMP TABLE money AS
    SELECT r.id, r.event_id, r.ticket_id, r.actual_amount, r.created_at, abs(hashtext(r.id::text)) % 100 AS bucket
    FROM event_registrations_link r JOIN events e ON e.id = r.event_id
    WHERE e.has_fee AND NOT r.is_deleted AND r.actual_amount > 0
    """,
    "CREATE INDEX ON money (id)",
    "ANALYZE money",
    # Never paid; paid and refunded; paid at the desk (no receipt); paid online.
    """UPDATE event_registrations_link r SET is_paid = false, paid_amount = 0, payment_receipt = NULL
       FROM money m WHERE r.id = m.id AND m.bucket < 2""",
    """UPDATE event_registrations_link r SET is_paid = false, paid_amount = 0, payment_receipt = 'er_' || m.ticket_id
       FROM money m WHERE r.id = m.id AND m.bucket BETWEEN 2 AND 4""",
    """UPDATE event_registrations_link r SET is_paid = true, paid_amount = m.actual_amount, payment_receipt = NULL
       FROM money m WHERE r.id = m.id AND m.bucket BETWEEN 5 AND 14""",
    """UPDATE event_registrations_link r SET is_paid = true, paid_amount = m.actual_amount, payment_receipt = 'er_' || m.ticket_id
       FROM money m WHERE r.id = m.id AND m.bucket >= 15""",
    # A tenth of the online ones bought a ₹350 tee with the ticket (and the refunded ones gave theirs back).
    """INSERT INTO event_store_items (id, club_id, event_id, name, price, stock, sold_count, reserved_count, free_roles, on_sale, sort_order,
                                       created_at, updated_at, is_deleted)
       SELECT gen_random_uuid(), e.club_id, e.id, 'Tee', 350, 100000, 0, 0, '[]', true, 0, now(), now(), false
       FROM events e WHERE e.club_id IS NOT NULL AND e.id IN (SELECT DISTINCT event_id FROM money WHERE bucket >= 90 OR bucket = 2)""",
    # Each event offers its tee (items are the club's; an event sells those it offers).
    """INSERT INTO event_store_offers (event_id, item_id, sort_order)
       SELECT i.event_id, i.id, 0 FROM event_store_items i WHERE i.name = 'Tee' AND i.event_id IS NOT NULL
       ON CONFLICT DO NOTHING""",
    """INSERT INTO event_store_orders (id, event_id, registration_id, status, total, paid_at, created_at, updated_at)
       SELECT gen_random_uuid(), m.event_id, m.id, CASE WHEN m.bucket = 2 THEN 'refunded' ELSE 'paid' END, 350,
              m.created_at + interval '1 minute', m.created_at, m.created_at + interval '1 minute'
       FROM money m WHERE m.bucket >= 90 OR m.bucket = 2""",
    """INSERT INTO event_store_order_lines (id, order_id, item_id, item_name, qty, unit_price, line_total, is_free)
       SELECT gen_random_uuid(), o.id, i.id, 'Tee', 1, 350, 350, false
       FROM event_store_orders o JOIN money m ON m.id = o.registration_id
       JOIN event_store_offers so ON so.event_id = o.event_id JOIN event_store_items i ON i.id = so.item_id""",
    # An order and a captured (or refunded) payment for each paid online.
    """INSERT INTO payment_orders (id, receipt, razorpay_receipt, razorpay_order_id, amount, currency, status, source, payload,
                                   created_at, updated_at, is_deleted)
       SELECT gen_random_uuid(), 'er_' || m.ticket_id, 'er_' || m.ticket_id || '#1', 'order_b_' || replace(m.id::text, '-', ''),
              m.actual_amount + CASE WHEN m.bucket >= 90 OR m.bucket = 2 THEN 350 ELSE 0 END, 'INR',
              CASE WHEN m.bucket BETWEEN 2 AND 4 THEN 'refunded'::orderstatus ELSE 'paid'::orderstatus END, 'event_registration',
              json_build_object('event_id', m.event_id, 'event_registration_id', m.id::text),
              m.created_at, m.created_at + interval '1 minute', false
       FROM money m WHERE m.bucket >= 15 OR m.bucket BETWEEN 2 AND 4""",
    """INSERT INTO payment_logs (id, order_id, razorpay_payment_id, status, amount, payment_method, created_at, updated_at, is_deleted)
       SELECT gen_random_uuid(), o.id, 'pay_b_' || substr(o.razorpay_order_id, 9),
              CASE WHEN o.status = 'refunded' THEN 'refunded'::paymentstatus ELSE 'captured'::paymentstatus END, o.amount, 'upi',
              o.created_at + interval '1 minute',
              CASE WHEN o.status = 'refunded' THEN o.created_at + interval '2 days' ELSE o.created_at + interval '1 minute' END, false
       FROM payment_orders o JOIN money m ON o.receipt = 'er_' || m.ticket_id""",
    "SET session_replication_role = origin",
    # What the triggers would have kept.
    """UPDATE events e SET paid_count = c.n FROM (
         SELECT event_id, count(*) AS n FROM event_registrations_link WHERE NOT is_deleted AND is_paid GROUP BY event_id
       ) c WHERE c.event_id = e.id AND e.paid_count <> c.n""",
    """UPDATE events e SET paid_count = 0 WHERE paid_count <> 0
       AND NOT EXISTS (SELECT 1 FROM event_registrations_link r WHERE r.event_id = e.id AND NOT r.is_deleted AND r.is_paid)""",
]

def _money_for(where: str) -> list[str]:
    """MONEY_SQL for the registrations of the events `where` picks."""
    out = []
    for sql in MONEY_SQL:
        if "CREATE TEMP TABLE money" in sql:
            sql = sql.replace("WHERE e.has_fee AND NOT r.is_deleted", f"WHERE e.has_fee AND NOT r.is_deleted AND ({where})")
        out.append(sql)
    return out


def megaclub(events: int, per: int) -> dict:
    """One existing club gets `events` paid events, held over the last two
    years, of `per` registrations each, and the money on them."""
    engine = _engine()
    out = {}
    with engine.connect() as conn:
        started = conn.execute(text("SELECT now()")).scalar()
        club = conn.execute(text("SELECT min(id) FROM clubs")).scalar()
        nusers = conn.execute(text("SELECT max(id) FROM users")).scalar()
        cols = [c for c in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'events' ORDER BY ordinal_position"
        )).scalars() if c != "id"]
        over = {
            "slug": "'mega-' || g", "name": "'Mega Fest ' || g", "has_fee": "true", "reg_fee": "499", "club_id": str(club),
            "status": "'closed'", "is_deleted": "false", "max_participants": "NULL", "going_count": str(per), "paid_count": "0",
            "event_datetime": "now() - make_interval(days => (g * 7) % 700 + 1)",
            "created_at": "now() - make_interval(days => (g * 7) % 700 + 40)", "updated_at": "now()",
        }
        t = time.monotonic()
        conn.execute(text("SET session_replication_role = replica"))
        conn.execute(text(
            f"INSERT INTO events ({', '.join(cols)}) SELECT {', '.join(over.get(c, 't.' + c) for c in cols)} "
            f"FROM generate_series(1, :n) g, (SELECT * FROM events WHERE has_fee AND NOT is_deleted LIMIT 1) t"
        ), {"n": events})
        conn.execute(text("""
            INSERT INTO event_registrations_link (event_id, user_id, full_name, email, ticket_id, role, is_attended, is_won, is_paid,
                                                  paid_amount, actual_amount, certificate_design_version, created_at, updated_at, is_deleted)
            SELECT e.id, 2 + ((e.id::bigint * 7919 + g) % (:nusers - 2)), 'Mega ' || g, 'mega' || g || '@example.com',
                   'MOA-M' || e.id || '-' || g, 'attendee', false, false, false, 0, 499, 0,
                   e.event_datetime - interval '20 days' + make_interval(secs => g * 60), now(), false
            FROM events e, generate_series(0, :per - 1) g WHERE e.slug LIKE 'mega-%'
        """), {"nusers": nusers, "per": per})
        conn.commit()
        out["registrations_seconds"] = round(time.monotonic() - t, 1)
        t = time.monotonic()
        for sql in _money_for("e.slug LIKE 'mega-%'"):
            conn.execute(text(sql))
        conn.commit()
        out["money_seconds"] = round(time.monotonic() - t, 1)
        out.update(club_id=club, since=started.isoformat(), events=events, registrations=events * per)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM ANALYZE"))
    return out


CHARGES_SQL = """
INSERT INTO subscription_charges (club_id, cycle, amount_paise, currency, razorpay_payment_id, razorpay_subscription_id, status,
                                  refunded_paise, charged_at, created_at, updated_at)
SELECT c.id, 'monthly', 69900, 'INR', 'pay_sb_' || s || '_' || k, 'sub_b_' || s,
       CASE WHEN (s + k) % 100 = 0 THEN 'failed' WHEN (s + k) % 100 IN (1, 2) THEN 'refunded'
            WHEN (s + k) % 100 = 3 THEN 'partially_refunded' ELSE 'captured' END,
       CASE WHEN (s + k) % 100 IN (1, 2) THEN 69900 WHEN (s + k) % 100 = 3 THEN 20000 ELSE 0 END,
       now() - make_interval(days => k * 30 + (s % 30)), now(), now()
FROM generate_series(1, :subs) s
JOIN LATERAL (SELECT id FROM clubs ORDER BY id OFFSET (s % :nclubs) LIMIT 1) c ON true,
     generate_series(0, :per - 1) k
"""


def _engine():
    return create_engine(settings.DATABASE_URL_SYNC)


def money(charges: int) -> dict:
    engine = _engine()
    out = {}
    with engine.connect() as conn:
        for sql in MONEY_SQL:
            t = time.monotonic()
            conn.execute(text(sql))
            out[sql.split()[0] + " " + " ".join(sql.split()[1:4])] = round(time.monotonic() - t, 1)
        conn.commit()
        nclubs = conn.execute(text("SELECT count(*) FROM clubs")).scalar()
        t = time.monotonic()
        conn.execute(text(CHARGES_SQL), {"subs": charges // 10, "per": 10, "nclubs": nclubs})
        conn.commit()
        out["charges"] = round(time.monotonic() - t, 1)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM ANALYZE"))
        out["counts"] = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in (
            "event_registrations_link", "payment_orders", "payment_logs", "event_store_orders", "subscription_charges")}
        out["paid_registrations"] = conn.execute(text("SELECT count(*) FROM event_registrations_link WHERE is_paid")).scalar()
    return out


async def backfill(batch: int, since: datetime | None = None) -> dict:
    from app.api.ledger import backfill as ledger_backfill
    from app.db.core import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        done = await ledger_backfill.backfill(session, batch=batch, since=since)
    engine = _engine()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("VACUUM ANALYZE ledger_entries"))
        conn.execute(text("VACUUM ANALYZE ledger_monthly"))
        conn.execute(text("VACUUM ANALYZE ledger_platform_monthly"))
        done["rows"] = {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar()
                        for t in ("ledger_entries", "ledger_monthly", "ledger_platform_monthly")}
        done["bytes"] = {t: conn.execute(text(f"SELECT pg_total_relation_size('{t}')")).scalar()
                         for t in ("ledger_entries", "ledger_monthly", "ledger_platform_monthly")}
    return done


# ─── Measuring ───────────────────────────────────────────────────────────


async def _timed(fn, repeat=REPEAT) -> dict:
    runs = []
    for _ in range(repeat):
        t = time.perf_counter()
        await fn()
        runs.append((time.perf_counter() - t) * 1000)
    return {"best_ms": round(min(runs), 1), "median_ms": round(statistics.median(runs), 1)}


class Capture:
    """Every SQL statement a block runs, with its parameters, for EXPLAIN."""

    def __init__(self, engine):
        self.engine, self.statements = engine, []

    def __enter__(self):
        from sqlalchemy import event

        event.listen(self.engine.sync_engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event

        event.remove(self.engine.sync_engine, "before_cursor_execute", self._on)

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append((statement, parameters))


def _explain(sync_engine, statement: str, parameters) -> dict:
    """EXPLAIN (ANALYZE, BUFFERS) of one captured asyncpg statement ($1.. parameters)."""
    import re

    params = list(parameters or ())
    sql = statement

    def lit(v):
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, (list, tuple)):
            return "ARRAY[" + ",".join(lit(x) for x in v) + "]" if v else "'{}'"
        if isinstance(v, datetime):
            return f"'{v.isoformat()}'::timestamptz"
        return "'" + str(v).replace("'", "''") + "'"

    # Each $n becomes its value; the cast after it, if any, stays as written.
    sql = re.sub(r"\$(\d+)", lambda m: lit(params[int(m.group(1)) - 1]), sql)
    with sync_engine.connect() as conn:
        plan = conn.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)).scalar()[0]
    top = plan["Plan"]

    def nodes(n):
        yield n
        for c in n.get("Plans", []):
            yield from nodes(c)

    scans = [f"{n['Node Type']} on {n.get('Relation Name', '')} {('using ' + n['Index Name']) if n.get('Index Name') else ''}".strip()
             for n in nodes(top) if "Scan" in n["Node Type"]]
    return {"sql": " ".join(statement.split())[:240], "execution_ms": round(plan["Execution Time"], 2),
            "rows": top.get("Actual Rows"), "shared_hit": top.get("Shared Hit Blocks"), "shared_read": top.get("Shared Read Blocks"),
            "scans": scans}


async def measure(out_path: str | None, club_id: int | None = None) -> dict:
    from app.api.clubs.models import Clubs
    from app.api.events.models import Events
    from app.api.finance import service as finance
    from app.api.ledger import compare, reads
    from app.api.subscriptions import revenue
    from app.db.core import AsyncSessionLocal, engine
    from sqlalchemy import func, select

    from app.api.events.models import EventRegistrationsLink as Reg

    sync_engine = _engine()
    result: dict = {"at": datetime.now(timezone.utc).isoformat()}
    async with AsyncSessionLocal() as session:
        # The biggest clubs by paid registrations, and the biggest event.
        top = (await session.execute(
            select(Events.club_id, func.count()).join(Reg, Reg.event_id == Events.id)
            .where(Events.club_id.isnot(None), Reg.is_paid == True)  # noqa: E712
            .group_by(Events.club_id).order_by(func.count().desc()).limit(3)
        )).all()
        big_club = await session.get(Clubs, club_id or top[0][0])
        big_event = await session.scalar(
            select(Events).join(Reg, Reg.event_id == Events.id).group_by(Events.id).order_by(func.count().desc()).limit(1)
        )
        span = await compare._span(session, big_club.id)
        regs_in_club = await session.scalar(select(func.count()).select_from(Reg).join(Events, Events.id == Reg.event_id).where(Events.club_id == big_club.id))
        regs_in_event = await session.scalar(select(func.count()).where(Reg.event_id == big_event.id))
        result["subjects"] = {"club_id": big_club.id, "club_registrations": regs_in_club, "club_paid": top[0][1],
                              "club_span": [str(span[0]), str(span[1])], "event_id": big_event.id, "event_registrations": regs_in_event}
        await session.commit()

        t = time.monotonic()
        checked = await compare.compare(session, club_ids=sorted({big_club.id, *(c for c, _ in top)}))
        result["compare"] = {"clubs": checked["clubs"], "club_months": checked["club_months"],
                             "differences": len(checked["differences"]), "first": checked["differences"][:5],
                             "seconds": round(time.monotonic() - t, 1)}

        frm, to = finance.resolve_period(None, None)
        now = datetime.now(timezone.utc)
        reads_to_time = {
            "club_summary_whole_history": lambda: finance.club_summary(session, big_club, span[0], span[1]),
            "club_summary_default_period": lambda: finance.club_summary(session, big_club, frm, to),
            "event_summary_biggest_event": lambda: finance.event_summary(session, big_event),
            "revenue_page": lambda: revenue.revenue(session, now=now),
            "revenue_collected": lambda: revenue.collected(session, now),
            "revenue_series_12": lambda: revenue.series(session, 12, now),
            "revenue_ticket_sales": lambda: revenue.ticket_sales(session, now),
        }
        timings = {}
        plans = {}
        for name, fn in reads_to_time.items():
            for flag in (False, True):
                label = "ledger" if flag else "rows"
                with reads.reading(flag):
                    await fn()  # warm
                    timings.setdefault(name, {})[label] = await _timed(fn)
                    with Capture(engine) as cap:
                        await fn()
                plans.setdefault(name, {})[label] = cap.statements
            await session.commit()
        result["timings"] = timings

    # EXPLAIN ANALYZE of the statements that dominate, both ways.
    explained = {}
    for name in ("club_summary_whole_history", "event_summary_biggest_event", "revenue_collected", "revenue_series_12", "revenue_ticket_sales"):
        for label, statements in plans[name].items():
            rows = []
            for statement, parameters in statements:
                if not statement.lstrip().upper().startswith("SELECT"):
                    continue
                try:
                    rows.append(_explain(sync_engine, statement, parameters))
                except Exception as exc:  # noqa: BLE001
                    rows.append({"sql": " ".join(statement.split())[:240], "error": str(exc)[:200]})
            rows.sort(key=lambda r: -(r.get("execution_ms") or 0))
            explained.setdefault(name, {})[label] = {"statements": len(rows), "total_ms": round(sum(r.get("execution_ms") or 0 for r in rows), 1),
                                                     "slowest": rows[:4]}
    result["explain"] = explained
    if out_path:
        with open(out_path, "w") as f:
            json.dump(result, f, indent=1, default=str)
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("money")
    m.add_argument("--charges", type=int, default=1_000_000)
    g = sub.add_parser("megaclub")
    g.add_argument("--events", type=int, default=400)
    g.add_argument("--per", type=int, default=2500)
    b = sub.add_parser("backfill")
    b.add_argument("--batch", type=int, default=500)
    b.add_argument("--since", type=datetime.fromisoformat, default=None)
    s = sub.add_parser("measure")
    s.add_argument("--out")
    s.add_argument("--club", type=int, default=None, help="the club whose finance page is timed (default: most paid registrations)")
    args = p.parse_args()
    if args.cmd == "money":
        print(json.dumps(money(args.charges), indent=1))
    elif args.cmd == "megaclub":
        print(json.dumps(megaclub(args.events, args.per), indent=1))
    elif args.cmd == "backfill":
        print(json.dumps(asyncio.run(backfill(args.batch, args.since)), indent=1))
    else:
        out = asyncio.run(measure(args.out, args.club))
        print(json.dumps({k: out[k] for k in ("subjects", "compare", "timings")}, indent=1, default=str))


if __name__ == "__main__":
    main()
