"""The guards `alembic upgrade` runs under (migrations/env.py,
app/db/migration_session.py), against a real Postgres.

Each test writes a tiny migration chain into a temporary versions folder and
runs it through the real env.py on a scratch database of its own (the
suite's database with `_mig` on the end), so what is proved is what
production runs: per-migration transactions, the session's timeouts, the
exemption for CONCURRENTLY builds, and the retry on a lock timeout."""
import logging
import textwrap
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

from app.config import settings
from app.db import migration_session
from app.db.migration_session import Guard, MigrationLockTimeout, is_online, retryable

BACKEND = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Without a database
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE INDEX CONCURRENTLY ix ON events (id)",
        "create unique index concurrently if not exists ix on users (lower(email))",
        "  -- why\nCREATE INDEX CONCURRENTLY ix ON t (a)",
        "/* a note */ DROP INDEX CONCURRENTLY IF EXISTS ix",
        "REINDEX INDEX CONCURRENTLY ix",
        "REINDEX (VERBOSE) TABLE CONCURRENTLY events",
        "ALTER TABLE events VALIDATE CONSTRAINT ck_x",
        "VACUUM ANALYZE events",
        "ANALYZE events",
    ],
)
def test_online_statements(statement):
    assert is_online(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE INDEX ix ON events (id)",
        "ALTER TABLE events ADD COLUMN x int",
        "ALTER TABLE events ADD CONSTRAINT ck CHECK (x > 0) NOT VALID",
        "UPDATE events SET x = 1",
        "SELECT 'CREATE INDEX CONCURRENTLY'",
        "DROP INDEX ix",
    ],
)
def test_blocking_statements_stay_guarded(statement):
    assert not is_online(statement)


def test_durations_are_validated_before_they_reach_a_set():
    with pytest.raises(ValueError):
        Guard(lock_timeout="5s; DROP TABLE users")
    with pytest.raises(ValueError):
        Guard(statement_timeout="ten seconds")
    assert Guard(lock_timeout="250ms", statement_timeout="0").connect_args()["options"].startswith(
        "-c lock_timeout=250ms -c statement_timeout=0"
    )


def test_settings_defaults():
    guard = Guard.from_settings(settings)
    assert (guard.lock_timeout, guard.statement_timeout, guard.online_statement_timeout, guard.idle_timeout) == (
        "5s", "30s", "0", "60s",
    )
    assert guard.attempts == 4
    assert guard.connect_args()["application_name"] == "alembic-migration"


class _PgError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.pgcode = code


class _Wrapped(Exception):
    def __init__(self, orig):
        super().__init__("wrapped")
        self.orig = orig


def test_retryable_reads_the_sqlstate_through_wrappers():
    assert retryable(_Wrapped(_PgError("55P03"))) == "lock timeout"
    assert retryable(_Wrapped(_PgError("40P01"))) == "deadlock"
    assert retryable(_Wrapped(_PgError("57014"))) is None  # statement timeout: not retried
    try:
        try:
            raise _Wrapped(_PgError("55P03"))
        except _Wrapped as inner:
            raise RuntimeError("outer") from inner
    except RuntimeError as outer:
        assert retryable(outer) == "lock timeout"
    assert retryable(ValueError("x")) is None


def test_run_retries_a_lock_timeout_then_succeeds():
    waits = []
    guard = Guard(attempts=4, retry_wait=1.0, sleep=waits.append)
    calls = []

    def attempt():
        calls.append(1)
        if len(calls) < 3:
            raise _Wrapped(_PgError("55P03"))

    guard.run(attempt)
    assert len(calls) == 3
    assert len(waits) == 2
    assert 1.0 <= waits[0] <= 1.5 and 2.0 <= waits[1] <= 3.0  # doubling, with jitter


def test_run_gives_up_loudly():
    guard = Guard(attempts=3, retry_wait=0, sleep=lambda s: None)
    calls = []

    def attempt():
        calls.append(1)
        raise _Wrapped(_PgError("55P03"))

    with pytest.raises(MigrationLockTimeout, match="Gave up after 3 attempts"):
        guard.run(attempt)
    assert len(calls) == 3


def test_run_never_retries_other_errors():
    guard = Guard(attempts=5, retry_wait=0, sleep=lambda s: None)
    calls = []

    def attempt():
        calls.append(1)
        raise _Wrapped(_PgError("57014"))

    with pytest.raises(_Wrapped):
        guard.run(attempt)
    assert len(calls) == 1


def test_run_does_not_retry_a_migration_that_committed_part_of_its_work():
    guard = Guard(attempts=5, retry_wait=0, sleep=lambda s: None)
    calls = []

    def attempt():
        calls.append(1)
        guard._on_commit(None)  # an autocommit block committed before the failure
        raise _Wrapped(_PgError("55P03"))

    with pytest.raises(MigrationLockTimeout, match="was not run again"):
        guard.run(attempt)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Through the real env.py, on a scratch database
# ---------------------------------------------------------------------------


def _scratch_url() -> str:
    parts = urlsplit(settings.DATABASE_URL_SYNC)
    return urlunsplit(parts._replace(path=parts.path + "_mig"))


def _admin_connection():
    parts = urlsplit(settings.DATABASE_URL_SYNC)
    conn = psycopg2.connect(urlunsplit(parts._replace(path="/postgres")))
    conn.autocommit = True
    return conn


@pytest.fixture(scope="module")
def scratch_db():
    url = _scratch_url()
    name = urlsplit(url).path.lstrip("/")
    try:
        admin = _admin_connection()
    except psycopg2.Error as error:  # pragma: no cover -- no maintenance db
        pytest.skip(f"cannot reach the maintenance database: {error}")
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{name}"')
    yield url
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin.close()


@pytest.fixture
def db(scratch_db, monkeypatch):
    """A clean scratch database, the guard's limits made small, and a
    connection for the test to look (and block) with."""
    conn = psycopg2.connect(scratch_db)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    monkeypatch.setattr(settings, "DATABASE_URL_SYNC", scratch_db)
    monkeypatch.setattr(settings, "MIGRATION_LOCK_TIMEOUT", "200ms")
    monkeypatch.setattr(settings, "MIGRATION_STATEMENT_TIMEOUT", "400ms")
    monkeypatch.setattr(settings, "MIGRATION_IDLE_TIMEOUT", "5s")
    monkeypatch.setattr(settings, "MIGRATION_LOCK_ATTEMPTS", 6)
    monkeypatch.setattr(settings, "MIGRATION_RETRY_WAIT", 0.1)
    yield conn
    conn.close()


def _chain(tmp_path: Path, *bodies: str) -> Config:
    """Migrations m1..mN, each `upgrade()` the given body, in a versions
    folder of their own, run through the real env.py."""
    versions = tmp_path / "versions"
    versions.mkdir()
    for number, body in enumerate(bodies, start=1):
        down = repr(f"m{number - 1}") if number > 1 else "None"
        (versions / f"m{number}.py").write_text(
            "import sqlalchemy as sa\nfrom alembic import op\n\n"
            f"revision = 'm{number}'\ndown_revision = {down}\nbranch_labels = None\ndepends_on = None\n\n\n"
            "def upgrade():\n" + textwrap.indent(textwrap.dedent(body).strip() + "\n", "    ")
            + "\n\ndef downgrade():\n    pass\n"
        )
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND / "migrations"))
    cfg.set_main_option("version_locations", str(versions))
    return cfg


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


def _version(conn):
    """The stamped revision; None when nothing is (the version table itself
    is made in the first migration's transaction, so it may not exist)."""
    if _one(conn, "SELECT to_regclass('alembic_version')") is None:
        return None
    return _one(conn, "SELECT string_agg(version_num, ',') FROM alembic_version")


def _blocker(dsn: str, sql: str):
    """A transaction holding a lock until released."""
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(sql)
    return conn


def test_the_session_runs_with_the_guard(db, tmp_path):
    cfg = _chain(tmp_path, """
        op.execute(
            "CREATE TABLE seen AS SELECT current_setting('lock_timeout') AS lt, "
            "current_setting('statement_timeout') AS st, "
            "current_setting('idle_in_transaction_session_timeout') AS it, "
            "current_setting('application_name') AS app"
        )
    """)
    command.upgrade(cfg, "head")
    with db.cursor() as cur:
        cur.execute("SELECT lt, st, it, app FROM seen")
        assert cur.fetchone() == ("200ms", "400ms", "5s", "alembic-migration")


def test_each_migration_commits_on_its_own(db, tmp_path):
    """m1 stays applied and stamped when m2 fails: it used to be one
    transaction, and m2's failure rolled m1 back with it."""
    cfg = _chain(
        tmp_path,
        "op.execute('CREATE TABLE one (id int)')",
        "op.execute('CREATE TABLE two (id int)')\nop.execute('SELECT 1/0')",
    )
    with pytest.raises(Exception, match="division by zero"):
        command.upgrade(cfg, "head")
    assert _version(db) == "m1"
    assert _one(db, "SELECT to_regclass('one')::text") == "one"
    assert _one(db, "SELECT to_regclass('two')::text") is None


def test_a_long_blocking_statement_hits_the_statement_timeout(db, tmp_path):
    cfg = _chain(tmp_path, "op.execute('SELECT pg_sleep(2)')")
    with pytest.raises(OperationalError, match="statement timeout"):
        command.upgrade(cfg, "head")
    assert _version(db) is None


def test_a_concurrent_index_build_is_not_cut_short(db, tmp_path):
    """The build takes well over the 400ms statement limit (a slow indexed
    expression over 60 rows) and waits on a transaction that outlives the
    200ms lock limit; it finishes anyway, the index is valid, and the guard
    is back in force for the statement after it."""
    with db.cursor() as cur:
        cur.execute(
            "CREATE FUNCTION slow_id(int) RETURNS int LANGUAGE sql IMMUTABLE "
            "AS 'SELECT $1 FROM pg_sleep(0.02)'"
        )
        cur.execute("CREATE TABLE s AS SELECT g AS id FROM generate_series(1, 60) g")
    # A writer's transaction, open when the build starts: CREATE INDEX
    # CONCURRENTLY waits for it to finish -- a lock wait.
    blocker = _blocker(settings.DATABASE_URL_SYNC, "UPDATE s SET id = id WHERE id = 1")
    release = threading.Timer(0.8, blocker.rollback)
    release.start()
    cfg = _chain(tmp_path, """
        with op.get_context().autocommit_block():
            op.execute("CREATE INDEX CONCURRENTLY ix_s_slow ON s (slow_id(id))")
            op.execute(
                "CREATE TABLE after_build AS SELECT current_setting('statement_timeout') AS st, "
                "current_setting('lock_timeout') AS lt"
            )
    """)
    try:
        command.upgrade(cfg, "head")
    finally:
        release.join()
        blocker.close()
    assert _version(db) == "m1"
    assert _one(db, "SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_s_slow'::regclass") is True
    with db.cursor() as cur:
        cur.execute("SELECT st, lt FROM after_build")
        assert cur.fetchone() == ("400ms", "200ms")


def test_a_lock_timeout_rolls_back_and_tries_again(db, tmp_path, caplog):
    """An ALTER behind a transaction holding the table gives up after 200ms
    rather than queueing everyone behind it, and goes through once the
    holder finishes."""
    with db.cursor() as cur:
        cur.execute("CREATE TABLE t (id int)")
    blocker = _blocker(settings.DATABASE_URL_SYNC, "LOCK TABLE t IN ACCESS SHARE MODE")
    release = threading.Timer(0.9, blocker.rollback)
    release.start()
    cfg = _chain(tmp_path, "op.execute('ALTER TABLE t ADD COLUMN x int')")
    try:
        with caplog.at_level(logging.WARNING, logger="alembic.env"):
            command.upgrade(cfg, "head")
    finally:
        release.join()
        blocker.close()
    retries = [r for r in caplog.records if "trying again" in r.getMessage()]
    assert retries, "the first attempt should have timed out on the lock"
    assert "lock timeout" in retries[0].getMessage()
    assert _version(db) == "m1"
    assert _one(db, "SELECT count(*) FROM information_schema.columns WHERE table_name = 't' AND column_name = 'x'") == 1


def test_gives_up_after_the_attempts_and_leaves_nothing_half_done(db, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(settings, "MIGRATION_LOCK_ATTEMPTS", 3)
    with db.cursor() as cur:
        cur.execute("CREATE TABLE t (id int)")
    blocker = _blocker(settings.DATABASE_URL_SYNC, "LOCK TABLE t IN ACCESS SHARE MODE")
    cfg = _chain(
        tmp_path,
        "op.execute('CREATE TABLE before_it (id int)')",
        "op.execute('CREATE TABLE beside_it (id int)')\nop.execute('ALTER TABLE t ADD COLUMN x int')",
    )
    try:
        with caplog.at_level(logging.WARNING, logger="alembic.env"):
            with pytest.raises(MigrationLockTimeout, match="Gave up after 3 attempts.*after m1"):
                command.upgrade(cfg, "head")
    finally:
        blocker.close()
    assert len([r for r in caplog.records if "trying again" in r.getMessage()]) == 2
    assert _version(db) == "m1"
    assert _one(db, "SELECT to_regclass('beside_it')::text") is None


def test_a_statement_in_an_autocommit_block_is_retried_in_place(db, tmp_path, caplog):
    """Each statement there is its own transaction: the one that timed out is
    tried again by itself, and the statements before it are not repeated
    (the INSERT would put a second row in)."""
    with db.cursor() as cur:
        cur.execute("CREATE TABLE t (id int); CREATE TABLE ran (n int)")
    blocker = _blocker(settings.DATABASE_URL_SYNC, "LOCK TABLE t IN ACCESS SHARE MODE")
    release = threading.Timer(0.9, blocker.rollback)
    release.start()
    cfg = _chain(tmp_path, """
        with op.get_context().autocommit_block():
            op.execute('INSERT INTO ran VALUES (1)')
            op.execute('ALTER TABLE t ADD COLUMN z int')
    """)
    try:
        with caplog.at_level(logging.WARNING, logger="alembic.env"):
            command.upgrade(cfg, "head")
    finally:
        release.join()
        blocker.close()
    retries = [r.getMessage() for r in caplog.records if "trying it again" in r.getMessage()]
    assert retries and "autocommit block" in retries[0] and "ALTER TABLE t ADD COLUMN z" in retries[0]
    assert not [r for r in caplog.records if "trying again in" in r.getMessage()]  # not the whole migration
    assert _one(db, "SELECT count(*) FROM ran") == 1
    assert _version(db) == "m1"


def test_a_migration_that_committed_part_of_its_work_is_not_retried(db, tmp_path, caplog):
    with db.cursor() as cur:
        cur.execute("CREATE TABLE t (id int)")
    blocker = _blocker(settings.DATABASE_URL_SYNC, "LOCK TABLE t IN ACCESS SHARE MODE")
    cfg = _chain(tmp_path, """
        with op.get_context().autocommit_block():
            op.execute('CREATE TABLE committed_part (id int)')
        op.execute('ALTER TABLE t ADD COLUMN x int')
    """)
    try:
        with caplog.at_level(logging.WARNING, logger="alembic.env"):
            with pytest.raises(MigrationLockTimeout, match="was not run again"):
                command.upgrade(cfg, "head")
    finally:
        blocker.close()
    assert not [r for r in caplog.records if "trying again" in r.getMessage()]
    assert _one(db, "SELECT to_regclass('committed_part')::text") == "committed_part"
    assert _version(db) is None


def test_the_module_logger_is_alembics():
    """Retries are logged where `alembic upgrade` prints (alembic.ini's
    `alembic` logger), so the deploy log shows them."""
    assert migration_session.logger.name.startswith("alembic.")
