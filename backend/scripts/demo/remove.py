"""Remove the demo: the seeded clubs, their events and the invented people,
and everything that hangs off them, leaving every real club, person and
event exactly as it is.

    python -m scripts.demo.remove                         # a dry run: what would go
    APP_DEMO_RESET_ALLOWED=1 python -m scripts.demo.remove --execute --confirm <database name>

The same three guards as the reset: `--execute`, `--confirm` with the
exact database name, and APP_DEMO_RESET_ALLOWED=1. A dry run, the default,
reads in a read-only transaction and changes nothing.

What is the demo, decided from the rows themselves, not from the data
files:

- a demo club is one whose owner signs in as `<its slug>@demo.myotherapp.in`
  (scripts/demo/seed.py makes every club that way, and refuses a slug
  already taken);
- a demo person is any account, admins excepted, whose address is at
  demo.myotherapp.in or a subdomain of it -- the attendees, the club logins,
  the club team members -- and the main demo user when `--main-email`
  names an address elsewhere;
- a demo event is a demo club's event.

Everything else that goes is found by following the database's own
foreign keys from those three: a row goes when it points, through a key
that would stop the delete (or cascade it), at a row that goes. A key that
empties itself on delete (ON DELETE SET NULL) is left to the database: a
real row pointing at a demo row loses the pointer, not itself. The money
ledger has no foreign keys by design, so its lines are found by their club,
event and registration, its append-only trigger lifted for this one
transaction, and the platform's monthly sums worked out again for the
months they touched. The demo's own organisations (marked with a demo
address) go when nothing else points at them.

All in one transaction at REPEATABLE READ; before the commit every
predicted row is checked gone and nothing outside the prediction moved in
the clubs, events and users tables. The bucket is left alone.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field

sys.path.insert(0, ".")

from sqlalchemy import text  # noqa: E402

from scripts.demo.reset import LOCK_KEY, Refused, database_tables, foreign_keys, guard_problems  # noqa: E402
from scripts.demo.schema import DEMO_DOMAIN  # noqa: E402

#: The ledger's tables and the columns that say whose a line is.
LEDGER = ("ledger_entries", "ledger_monthly", "ledger_platform_monthly")
GUARD_TRIGGERS = ("trg_ledger_entries_no_truncate", "trg_ledger_entries_append_only")

#: Keys whose delete is stopped or cascaded: those decide what goes with a doomed row.
FOLLOWED_ACTIONS = ("a", "r", "c")  # NO ACTION, RESTRICT, CASCADE


@dataclass
class Plan:
    predicates: dict[str, str] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    nulls: list[tuple[str, str]] = field(default_factory=list)
    clubs: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    ledger: dict[str, int] = field(default_factory=dict)
    months: list = field(default_factory=list)
    orgs: int = 0
    owned: str = "FALSE"


def domain_sql(column: str) -> str:
    d = DEMO_DOMAIN.lower()
    return f"(lower(split_part(btrim({column}), '@', 2)) = '{d}' OR lower(split_part(btrim({column}), '@', 2)) LIKE '%.{d}')"


def roots(main_email: str | None) -> dict[str, str]:
    """The demo's people, clubs and events, as WHERE clauses over their own
    tables (subqueries, not temporary tables: a dry run's transaction is
    read-only, and may create nothing)."""
    extra = ""
    if main_email:
        extra = " OR lower(btrim(email)) = lower(btrim('" + main_email.replace("'", "''") + "'))"
    users = f"SELECT id FROM users WHERE user_type <> 'admin' AND ({domain_sql('email')}{extra})"
    clubs = (
        "SELECT c.id FROM clubs c JOIN users u ON u.id = c.user_id "
        f"WHERE lower(btrim(u.email)) = lower(c.slug) || '@{DEMO_DOMAIN}'"
    )
    events = f"SELECT id FROM events WHERE club_id IN ({clubs})"
    return {"users": f"id IN ({users})", "clubs": f"id IN ({clubs})", "events": f"id IN ({events})"}


async def make_plan(conn, main_email: str | None) -> Plan:
    ROOTS = roots(main_email)
    tables = set(await database_tables(conn))
    fks = [fk for fk in await foreign_keys(conn) if fk.child in tables and fk.parent in tables]
    actions = {r[0]: r[1] for r in (await conn.execute(text(
        "SELECT conname, CAST(confdeltype AS text) FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
        "WHERE c.contype = 'f' AND n.nspname = 'public'"
    ))).all()}
    by_child: dict[str, list] = defaultdict(list)
    for fk in fks:
        if actions.get(fk.name) in FOLLOWED_ACTIONS and fk.child != fk.parent:
            by_child[fk.child].append(fk)

    plan = Plan()
    resolving: set[str] = set()

    def predicate(table: str) -> str | None:
        if table in ROOTS:
            return ROOTS[table]
        if table in plan.predicates:
            return plan.predicates[table]
        if table in resolving:
            return None
        resolving.add(table)
        parts = []
        for fk in by_child.get(table, []):
            parent = predicate(fk.parent)
            if parent:
                parts.append(f'"{fk.column}" IN (SELECT "{fk.parent_column}" FROM "{fk.parent}" WHERE {parent})')
        resolving.discard(table)
        if parts:
            plan.predicates[table] = "(" + " OR ".join(parts) + ")"
            return plan.predicates[table]
        return None

    for t in sorted(tables):
        predicate(t)
    for t, where in ROOTS.items():
        plan.predicates[t] = where
    for t, where in plan.predicates.items():
        plan.counts[t] = int(await conn.scalar(text(f'SELECT count(*) FROM "{t}" WHERE {where}')))
    doomed = {t for t, n in plan.counts.items() if n}

    # Children first; a loop between doomed tables (an event and the design
    # forked for it) is broken by emptying the nullable key on the doomed rows.
    edges = [(fk.child, fk.parent, fk) for fk in fks if fk.child in doomed and fk.parent in doomed and fk.child != fk.parent]
    while True:
        order, done, pending = [], set(), set(doomed)
        children = defaultdict(set)
        for child, parent, _fk in edges:
            children[parent].add(child)
        while pending:
            ready = sorted(t for t in pending if children[t] <= done)
            if not ready:
                break
            for t in ready:
                order.append(t)
                done.add(t)
                pending.discard(t)
        if not pending:
            plan.order = order
            break
        # Only a key on a loop is emptied, and only one the row's own
        # reason for going does not rest on (an event goes for its club, not
        # for the design it points at).
        reach = {t: set() for t in pending}
        for t in pending:
            stack = [t]
            while stack:
                for c2, p2, _ in edges:
                    if c2 == stack[-1] and p2 in pending and p2 not in reach[t]:
                        reach[t].add(p2)
                        stack.append(p2)
                        break
                else:
                    stack.pop()
        on_loop = [(c, p, fk) for c, p, fk in edges
                   if c in pending and p in pending and c in reach.get(p, set())
                   and not fk.not_null and f'"{fk.column}"' not in plan.predicates[c] and fk.column not in ROOTS.get(c, "")]
        if not on_loop:
            raise Refused(f"the tables {sorted(pending)} point at each other through keys that cannot be emptied safely")
        c, p, fk = sorted(on_loop, key=lambda e: (e[0] not in ROOTS, e[0], e[2].column))[0]
        plan.nulls.append((c, fk.column))
        edges = [e for e in edges if e[2] is not fk]

    plan.clubs = [r[0] for r in (await conn.execute(text(f"SELECT slug FROM clubs WHERE {ROOTS['clubs']} ORDER BY slug"))).all()]
    plan.events = [str(x) for r in (await conn.execute(text(f"SELECT id, slug FROM events WHERE {ROOTS['events']}"))).all() for x in r]
    plan.owned = (f"club_id IN (SELECT id FROM clubs WHERE {ROOTS['clubs']}) OR "
                  f"event_id IN (SELECT id FROM events WHERE {ROOTS['events']})")
    owned = plan.owned
    if "ledger_entries" in tables:
        plan.ledger["ledger_entries"] = int(await conn.scalar(text(
            f"SELECT count(*) FROM ledger_entries WHERE {owned} OR registration_id IN "
            f"(SELECT id FROM event_registrations_link WHERE {plan.predicates['event_registrations_link']})")))
        plan.ledger["ledger_monthly"] = int(await conn.scalar(text(f"SELECT count(*) FROM ledger_monthly WHERE {owned}")))
        plan.months = [r[0] for r in (await conn.execute(text(f"SELECT DISTINCT month FROM ledger_monthly WHERE {owned}"))).all()]
    plan.orgs = int(await conn.scalar(text(f"SELECT count(*) FROM organizations WHERE {domain_sql('email')}")))
    return plan


async def execute_plan(conn, plan: Plan) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '15s'"))
    await conn.execute(text("SET LOCAL statement_timeout = '15min'"))
    before = {t: int(await conn.scalar(text(f'SELECT count(*) FROM "{t}"'))) for t in ("users", "clubs", "events")}

    # The ledger first: its lines are found through registrations that are about to go.
    if plan.ledger:
        owned = plan.owned
        for trigger in GUARD_TRIGGERS:
            await conn.execute(text(f'ALTER TABLE ledger_entries DISABLE TRIGGER "{trigger}"'))
        await conn.execute(text(
            f"DELETE FROM ledger_entries WHERE {owned} OR registration_id IN "
            f"(SELECT id FROM event_registrations_link WHERE {plan.predicates['event_registrations_link']})"))
        for trigger in GUARD_TRIGGERS:
            await conn.execute(text(f'ALTER TABLE ledger_entries ENABLE TRIGGER "{trigger}"'))
        await conn.execute(text(f"DELETE FROM ledger_monthly WHERE {owned}"))
        if plan.months:
            # The platform's sums for those months, from the clubs' sums again
            # (as the daily reconcile puts them right: the shards summed on shard 0).
            await conn.execute(text("DELETE FROM ledger_platform_monthly WHERE month = ANY(:m)"), {"m": plan.months})
            await conn.execute(text(
                "INSERT INTO ledger_platform_monthly (month, origin_month, club_gateway, account, kind, shard, amount_paise, units, entries, updated_at) "
                "SELECT month, origin_month, gateway_id <> 0, account, kind, 0, sum(amount_paise), sum(units), sum(entries), now() "
                "FROM ledger_monthly WHERE month = ANY(:m) GROUP BY month, origin_month, gateway_id <> 0, account, kind"
            ), {"m": plan.months})

    for table, column in plan.nulls:
        await conn.execute(text(f'UPDATE "{table}" SET "{column}" = NULL WHERE {plan.predicates[table]}'))
    for table, _column in plan.nulls:
        still = int(await conn.scalar(text(f'SELECT count(*) FROM "{table}" WHERE {plan.predicates[table]}')))
        if still != plan.counts[table]:
            raise Refused(f"emptying a key on {table} changed which of its rows are the demo's ({plan.counts[table]} -> {still})")
    for t in plan.order:
        await conn.execute(text(f'DELETE FROM "{t}" WHERE {plan.predicates[t]}'))
    await conn.execute(text(
        f"DELETE FROM organizations o WHERE {domain_sql('o.email')} "
        "AND NOT EXISTS (SELECT 1 FROM clubs c WHERE c.org_id = o.id) AND NOT EXISTS (SELECT 1 FROM user_profiles p WHERE p.org_id = o.id)"
    ))

    problems = []
    for t in plan.order:
        left = int(await conn.scalar(text(f'SELECT count(*) FROM "{t}" WHERE {plan.predicates[t]}')))
        if left:
            problems.append(f"{t}: {left} demo row(s) still there")
    for t, n in before.items():
        now = int(await conn.scalar(text(f'SELECT count(*) FROM "{t}"')))
        if now != n - plan.counts.get(t, 0):
            problems.append(f"{t}: {n} rows before, {plan.counts.get(t, 0)} to go, {now} now -- something else moved")
    enabled = {r[0]: r[1] for r in (await conn.execute(text(
        "SELECT tgname, CAST(tgenabled AS text) FROM pg_trigger WHERE tgrelid = CAST('ledger_entries' AS regclass)"))).all()} if plan.ledger else {}
    for trigger in GUARD_TRIGGERS:
        if enabled and enabled.get(trigger) != "O":
            problems.append(f"ledger trigger {trigger} is not back on")
    if problems:
        raise Refused("the checks after the removal failed, so nothing was committed:\n  " + "\n  ".join(problems))


def render(plan: Plan, *, execute: bool) -> str:
    verb = "removed" if execute else "would go"
    lines = [f"Demo clubs: {', '.join(plan.clubs) or 'none'}", "", f"{'table'.ljust(34)} {verb:>9}", "-" * 44]
    for t in plan.order:
        lines.append(f"{t.ljust(34)} {plan.counts[t]:>9}")
    for t, n in plan.ledger.items():
        lines.append(f"{t.ljust(34)} {n:>9}")
    if plan.months:
        lines.append(f"{'ledger_platform_monthly'.ljust(34)} {'rebuilt':>9}  ({len(plan.months)} month(s))")
    lines.append(f"{'organizations (demo-marked, unused)'.ljust(34)} {('up to ' + str(plan.orgs)):>9}")
    if plan.nulls:
        lines.append("  emptied first to break a loop: " + ", ".join(f"{t}.{c}" for t, c in plan.nulls))
    return "\n".join(lines)


async def run(*, execute: bool, confirm: str | None, main_email: str | None, env: dict | None = None, out=print) -> int:
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.config import settings

    env = os.environ if env is None else env
    engine = create_async_engine(settings.DATABASE_URL, pool_size=1, max_overflow=0, isolation_level="REPEATABLE READ")
    try:
        async with engine.connect() as conn:
            database = await conn.scalar(text("SELECT current_database()"))
            await conn.rollback()
            problems = guard_problems(execute=execute, confirm=confirm, database=database, env=env)
            if problems:
                out("Refused: " + "; ".join(problems) + ".")
                return 2
            async with conn.begin() as trans:
                if not execute:
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": LOCK_KEY})
                try:
                    plan = await make_plan(conn, main_email)
                    out(f"Database {database!r}{' -- DRY RUN, nothing will change' if not execute else ''}\n")
                    out(render(plan, execute=execute))
                    if not execute:
                        await trans.rollback()
                        out("\nDry run: nothing changed. To execute: APP_DEMO_RESET_ALLOWED=1 python -m scripts.demo.remove "
                            f"--execute --confirm {database}" + (f" --main-email {main_email}" if main_email else ""))
                        return 0
                    await execute_plan(conn, plan)
                except Refused as e:
                    await trans.rollback()
                    out(f"Refused: {e}")
                    return 2
            out(f"\nDone: the demo ({len(plan.clubs)} club(s)) removed in one transaction, checked and committed.")
        _revalidate(plan)
        return 0
    finally:
        await engine.dispose()


def _revalidate(plan: Plan) -> None:
    """Tell the public site to drop the removed pages (when it is set up to be told)."""
    from scripts.demo.reset import revalidate

    if plan.clubs or plan.events:
        revalidate(plan.events, plan.clubs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.demo.remove", description=__doc__.split("\n\n")[0])
    parser.add_argument("--execute", action="store_true", help="really do it (the default is a dry run)")
    parser.add_argument("--confirm", metavar="DATABASE", help="the exact name of the database, to execute")
    parser.add_argument("--main-email", default=None, help="the main demo user's address, when it is not at the demo domain")
    args = parser.parse_args(argv)
    return asyncio.run(run(execute=args.execute, confirm=args.confirm, main_email=args.main_email))


if __name__ == "__main__":
    sys.exit(main())
