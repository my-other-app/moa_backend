"""The guards `alembic upgrade` runs under (migrations/env.py).

A migration shares the database with the live API: production migrates in
the container that is still serving. Three things keep it from stalling
that traffic.

- **lock_timeout.** An ALTER TABLE asks for an ACCESS EXCLUSIVE lock. If a
  slow query or an open transaction holds the table, the ALTER waits -- and
  every query on that table that arrives after it queues behind the ALTER,
  even plain SELECTs. With no limit that queue lasts as long as the slowest
  query in front, and requests time out behind it. With lock_timeout the
  ALTER gives up after a few seconds instead, its transaction rolls back
  (releasing every lock it held, so the queue drains at once), and the
  migration is tried again after a pause: `run`, below.
- **statement_timeout.** A statement that holds a strong lock for long -- a
  column type change that rewrites the table, a CHECK or foreign key added
  without NOT VALID, an index built without CONCURRENTLY -- blocks the table
  for all of its run. The limit bounds that harm: it fails the migration,
  loudly, rather than taking the table away for minutes.
- **idle_in_transaction_session_timeout.** If the process running the
  migration stops talking mid-transaction (a hung client, a dropped SSH
  session whose process lingers), the server ends the session after this
  long and its locks go with it.

The "online" statements are the exception: CREATE/DROP INDEX CONCURRENTLY,
REINDEX CONCURRENTLY, VALIDATE CONSTRAINT, VACUUM and ANALYZE, when run in
an autocommit block. They take SHARE UPDATE EXCLUSIVE at most, which blocks
neither reads nor writes, so a long build or a long wait for old
transactions costs the deploy time, not the site's. They run with their own
limits (MIGRATION_ONLINE_STATEMENT_TIMEOUT, none by default): a big index
built concurrently must not be killed half way by the guard meant for a
blocking ALTER, which would leave an INVALID index behind.

Settings (app/config.py): MIGRATION_LOCK_TIMEOUT, MIGRATION_STATEMENT_TIMEOUT,
MIGRATION_ONLINE_STATEMENT_TIMEOUT, MIGRATION_IDLE_TIMEOUT,
MIGRATION_LOCK_ATTEMPTS, MIGRATION_RETRY_WAIT. A timeout of "0" turns that
guard off, as in Postgres.
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from sqlalchemy import event

logger = logging.getLogger("alembic.env")

#: Postgres durations as the server reads them: a bare number is milliseconds.
DURATION = re.compile(r"^\d+(us|ms|s|min|h|d)?$")

#: SQLSTATEs worth trying again: the lock was not granted in time
#: (lock_not_available), or the server broke a deadlock by cancelling us.
RETRYABLE = {"55P03": "lock timeout", "40P01": "deadlock"}

#: What Postgres sees as the client: what the runbook's pg_stat_activity
#: query looks for.
APPLICATION_NAME = "alembic-migration"

_COMMENTS = re.compile(r"(--[^\n]*\n)|(/\*.*?\*/)", re.S)
_ONLINE = re.compile(
    r"^(CREATE\s+(UNIQUE\s+)?INDEX\s+CONCURRENTLY"
    r"|DROP\s+INDEX\s+CONCURRENTLY"
    r"|REINDEX\b.*\bCONCURRENTLY"
    r"|ALTER\s+TABLE\b.*\bVALIDATE\s+CONSTRAINT"
    r"|VACUUM\b|ANALYZE\b)",
    re.I | re.S,
)


def is_online(statement: str) -> bool:
    """Whether `statement` is one that blocks neither reads nor writes while
    it runs (see the module docstring), so may take as long as it needs."""
    text = _COMMENTS.sub(" ", statement or "").strip()
    return bool(_ONLINE.match(text))


def _duration(name: str, value: str) -> str:
    value = str(value).strip()
    if not DURATION.match(value):
        raise ValueError(f"{name}={value!r} is not a Postgres duration such as 5s, 500ms or 2min")
    return value


class MigrationLockTimeout(RuntimeError):
    """Every attempt at a migration waited out lock_timeout (or deadlocked)."""


@dataclass
class Guard:
    lock_timeout: str = "5s"
    statement_timeout: str = "30s"
    online_statement_timeout: str = "0"
    idle_timeout: str = "60s"
    attempts: int = 4
    retry_wait: float = 5.0
    #: Swapped in tests, so a retry does not really wait.
    sleep: Callable[[float], None] = time.sleep
    # Per attempt: whether the migration being run has committed part of its
    # work (an autocommit block) -- see on_commit.
    _step_done: bool = field(default=False, repr=False)
    partial: bool = field(default=False, repr=False)
    applied: list = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        # Validated here because they are written into SET statements.
        for name in ("lock_timeout", "statement_timeout", "online_statement_timeout", "idle_timeout"):
            setattr(self, name, _duration(name, getattr(self, name)))

    @classmethod
    def from_settings(cls, settings) -> "Guard":
        return cls(
            lock_timeout=_duration("APP_MIGRATION_LOCK_TIMEOUT", settings.MIGRATION_LOCK_TIMEOUT),
            statement_timeout=_duration("APP_MIGRATION_STATEMENT_TIMEOUT", settings.MIGRATION_STATEMENT_TIMEOUT),
            online_statement_timeout=_duration(
                "APP_MIGRATION_ONLINE_STATEMENT_TIMEOUT", settings.MIGRATION_ONLINE_STATEMENT_TIMEOUT
            ),
            idle_timeout=_duration("APP_MIGRATION_IDLE_TIMEOUT", settings.MIGRATION_IDLE_TIMEOUT),
            attempts=max(1, int(settings.MIGRATION_LOCK_ATTEMPTS)),
            retry_wait=max(0.0, float(settings.MIGRATION_RETRY_WAIT)),
        )

    def describe(self) -> str:
        return (
            f"lock_timeout={self.lock_timeout} statement_timeout={self.statement_timeout} "
            f"(online statements: {self.online_statement_timeout}) "
            f"idle_in_transaction_session_timeout={self.idle_timeout} attempts={self.attempts}"
        )

    # ------------------------------------------------------------------
    # The session
    # ------------------------------------------------------------------

    def connect_args(self) -> dict:
        """Session settings as libpq startup options: in force from the first
        statement, and not undone by a rolled-back transaction the way a SET
        issued inside one would be."""
        options = " ".join(
            f"-c {name}={value}"
            for name, value in (
                ("lock_timeout", self.lock_timeout),
                ("statement_timeout", self.statement_timeout),
                ("idle_in_transaction_session_timeout", self.idle_timeout),
            )
        )
        return {"options": options, "application_name": APPLICATION_NAME}

    def install(self, engine) -> None:
        """Hook the engine: online statements get their own limits, and every
        commit is noted for `partial`."""
        event.listen(engine, "before_cursor_execute", self._before)
        event.listen(engine, "after_cursor_execute", self._after)
        event.listen(engine, "handle_error", self._on_error)
        event.listen(engine, "commit", self._on_commit)

    def _set(self, dbapi_connection, statement_timeout: str, lock_timeout: str) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute(f"SET statement_timeout = '{statement_timeout}'; SET lock_timeout = '{lock_timeout}'")
        finally:
            cursor.close()

    def _before(self, conn, cursor, statement, parameters, context, executemany) -> None:
        dbapi_connection = cursor.connection
        # Only outside a transaction. In autocommit mode each statement is its
        # own transaction, so the SET below takes effect at once and lasts
        # until _after puts the guard back; inside a transaction an online
        # statement (VALIDATE after an ALTER in the same transaction) would
        # run while that transaction holds its ACCESS EXCLUSIVE lock, which
        # is exactly what the guard is for.
        if not getattr(dbapi_connection, "autocommit", False) or not is_online(statement):
            return
        self._set(dbapi_connection, self.online_statement_timeout, "0")
        conn.info["moa_online"] = True

    def _restore(self, conn, dbapi_connection) -> None:
        if conn.info.pop("moa_online", False):
            self._set(dbapi_connection, self.statement_timeout, self.lock_timeout)

    def _after(self, conn, cursor, statement, parameters, context, executemany) -> None:
        self._restore(conn, cursor.connection)

    def _on_error(self, exception_context) -> None:
        conn = exception_context.connection
        if conn is None or exception_context.is_disconnect or not conn.info.get("moa_online"):
            return
        try:
            self._restore(conn, conn.connection.dbapi_connection)
        except Exception:  # noqa: BLE001 -- the original error is the one to raise
            logger.warning("Could not restore the migration guard after an error", exc_info=True)

    # ------------------------------------------------------------------
    # Partial work: may this migration simply be run again?
    # ------------------------------------------------------------------
    #
    # With transaction_per_migration, alembic runs each migration in its own
    # transaction and stamps its revision inside it, just before the commit
    # (on_version_apply is called there). So the first commit after
    # on_version_apply is that migration's own, and any other commit is an
    # autocommit block committing part of the next one -- which a retry would
    # run a second time.

    def on_version_apply(self, *, ctx, step, heads, run_args) -> None:
        self._step_done = True
        self.applied.append(getattr(step, "up_revision_id", None) or str(step))

    def _on_commit(self, conn) -> None:
        if self._step_done:
            self._step_done = False
            self.partial = False
        else:
            self.partial = True

    def reset(self) -> None:
        self._step_done = False
        self.partial = False

    # ------------------------------------------------------------------
    # Retrying
    # ------------------------------------------------------------------

    def run(self, attempt: Callable[[], None]) -> None:
        """Call `attempt` (one `alembic upgrade` on a fresh connection) until
        it succeeds, trying again after a lock timeout or a deadlock as long
        as the failed migration committed none of its work.

        Each attempt starts from the database's current revision, so the
        migrations applied before the failure are not run again."""
        for number in range(1, self.attempts + 1):
            self.reset()
            try:
                attempt()
                return
            except Exception as error:  # noqa: BLE001 -- re-raised unless retryable
                reason = retryable(error)
                if reason is None:
                    raise
                where = f"after {self.applied[-1]}" if self.applied else "the first pending one"
                if self.partial:
                    raise MigrationLockTimeout(
                        f"A migration ({where}) hit a {reason} after it had committed part of its work "
                        "in an autocommit block, so it was not retried: running it again repeats that "
                        "part. Check the migration is idempotent (IF NOT EXISTS, a dropped INVALID index) "
                        "before running `alembic upgrade head` again. What held the lock: "
                        "docs/OPERATIONS.md, 'A stalled migration'."
                    ) from error
                if number == self.attempts:
                    raise MigrationLockTimeout(
                        f"Gave up after {self.attempts} attempts: a migration ({where}) could not get its "
                        f"locks within lock_timeout={self.lock_timeout} ({reason}) each time. Its transaction "
                        "was rolled back, so nothing of it is applied; the migrations before it are. "
                        "Something holds the table -- a long query or an open transaction. Find it: "
                        "docs/OPERATIONS.md, 'A stalled migration'."
                    ) from error
                wait = self.retry_wait * (2 ** (number - 1)) * random.uniform(1.0, 1.5)
                logger.warning(
                    "%s on a migration (%s), rolled back; trying again in %.1fs (attempt %d of %d)",
                    reason, where, wait, number + 1, self.attempts,
                )
                self.sleep(wait)


def retryable(error: BaseException) -> Optional[str]:
    """The reason to try `error` again (lock timeout, deadlock), else None.
    Looks through SQLAlchemy's wrapper and the exception's causes for the
    driver error's SQLSTATE."""
    seen = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for candidate in (current, getattr(current, "orig", None)):
            code = getattr(candidate, "pgcode", None) or getattr(candidate, "sqlstate", None)
            if code in RETRYABLE:
                return RETRYABLE[code]
        current = current.__cause__ or current.__context__
    return None
