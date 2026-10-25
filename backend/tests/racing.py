"""The other half of a double tap.

Two taps of one button arrive as two requests; each checks that the row is
not there yet, and the second inserts after the first committed. The suite
runs every request on one connection, so the race cannot be run for real;
`beaten_to` stages it: just before the app's own INSERT INTO `table`, the
row the other tap made is written on the same connection, as if it had
committed in between. Code that checked and then inserted fails on the
unique key (a 500); code that inserts ON CONFLICT answers.

Tests that do run a race for real (tests/test_ticketing_races.py) commit on
connections of their own; `committed_keys` and `sweep` put the database back
as they found it.
"""
from __future__ import annotations

import contextlib

from sqlalchemy import delete, event, func, literal, select, text, tuple_, union_all

#: Triggers that refuse a DELETE the sweep must make. Its own transaction
#: turns them off and on again; nothing else sees the gap.
_REFUSE_DELETE = {"ledger_entries": "trg_ledger_entries_append_only"}


def _tables():
    from app.db.base import AbstractSQLModel

    return AbstractSQLModel.metadata.sorted_tables


async def _keys(conn) -> dict[str, set[tuple]]:
    """Every table's primary keys: one statement counts the rows of all of
    them, and only the tables with rows are read."""
    tables = _tables()
    counts = union_all(*(select(literal(t.name), select(func.count()).select_from(t).scalar_subquery()) for t in tables))
    full = {name for name, n in (await conn.execute(counts)).all() if n}
    return {
        t.name: set(map(tuple, (await conn.execute(select(*t.primary_key.columns))).all())) if t.name in full else set()
        for t in tables
    }


async def committed_keys(engine) -> dict[str, set[tuple]]:
    """Every table's primary keys, as committed now."""
    async with engine.connect() as conn:
        return await _keys(conn)


async def sweep(engine, before: dict[str, set[tuple]]) -> dict[str, int]:
    """Delete every committed row that was not there `before`, in every
    table, and say how many went from each.

    Whatever the code under test wrote -- ledger lines, outbox rows, jobs,
    guests -- goes, named or not, so the next test file starts from the
    database this one found. Children go before their parents; rows a DELETE
    trigger writes (the search outbox) go in the next pass."""
    by_name = {t.name: t for t in _tables()}
    order = [t.name for t in reversed(_tables())]
    removed: dict[str, int] = {}
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '10s'"))
        for table, trigger in _REFUSE_DELETE.items():
            await conn.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}"))
        for _ in range(5):
            now = await _keys(conn)
            new = {name: now[name] - before[name] for name in order if now[name] - before[name]}
            if not new:
                break
            for name in (n for n in order if n in new):
                t = by_name[name]
                await conn.execute(delete(t).where(tuple_(*t.primary_key.columns).in_(list(new[name]))))
                removed[name] = removed.get(name, 0) + len(new[name])
        for table, trigger in _REFUSE_DELETE.items():
            await conn.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}"))
    return removed


@contextlib.contextmanager
def beaten_to(session, table: str, sql: str, *params):
    """Run `sql` ($1-style parameters) right before the first INSERT INTO `table`."""
    conn = session.bind.sync_connection
    fired: list[bool] = []

    def before(_conn, cursor, statement, _parameters, _context, _many):
        if not fired and statement.lstrip().upper().startswith(f"INSERT INTO {table.upper()} "):
            fired.append(True)
            cursor.execute(sql, params)

    event.listen(conn, "before_cursor_execute", before)
    try:
        yield fired
    finally:
        event.remove(conn, "before_cursor_execute", before)


@contextlib.contextmanager
def statements(session):
    """Every statement the session's connection runs meanwhile."""
    conn = session.bind.sync_connection
    seen: list[str] = []

    def before(_conn, _cursor, statement, *_args):
        seen.append(statement)

    event.listen(conn, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(conn, "before_cursor_execute", before)
