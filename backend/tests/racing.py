"""The other half of a double tap.

Two taps of one button arrive as two requests; each checks that the row is
not there yet, and the second inserts after the first committed. The suite
runs every request on one connection, so the race cannot be run for real;
`beaten_to` stages it: just before the app's own INSERT INTO `table`, the
row the other tap made is written on the same connection, as if it had
committed in between. Code that checked and then inserted fails on the
unique key (a 500); code that inserts ON CONFLICT answers.
"""
from __future__ import annotations

import contextlib

from sqlalchemy import event


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
