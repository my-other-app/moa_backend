"""ops/server/db/app-role.sql, run with psql against the suite's full schema
(every table the models make): the role it makes reads and writes every
table and uses every sequence, reads but cannot change the migration head,
and cannot create, alter or drop anything, run programs, or make databases or
roles. What the owner creates afterwards is granted to it as well, and a
second run changes nothing. Skips without psql.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager
from glob import glob
from pathlib import Path
from urllib.parse import urlsplit

import psycopg2
import pytest

from app.config import settings

SQL = Path(__file__).resolve().parents[2] / "ops" / "server" / "db" / "app-role.sql"
ROLE = "moa_app_role_test"
PASSWORD = "0123456789abcdef0123456789abcdef"


def _psql() -> str | None:
    found = shutil.which("psql")
    if found:
        return found
    for candidate in sorted(
        glob("/opt/homebrew/opt/postgresql@*/bin/psql")
        + glob("/usr/lib/postgresql/*/bin/psql"),
        reverse=True,
    ):
        return candidate
    return None


@contextmanager
def _cursor(**connect):
    """An autocommit cursor (psycopg2's `with conn` would open a transaction,
    and one refused statement would then abort the rest)."""
    conn = (
        psycopg2.connect(**connect)
        if connect
        else psycopg2.connect(settings.DATABASE_URL_SYNC)
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def _apply(role: str = ROLE, password: str = PASSWORD) -> subprocess.CompletedProcess:
    with _cursor() as cur:
        cur.execute("SELECT current_user, current_database()")
        owner, db = cur.fetchone()
    stdin = (
        f"\\set app_role {role}\n\\set app_password '{password}'\n" + SQL.read_text()
    )
    return subprocess.run(
        [
            _psql(),
            "-X",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"owner={owner}",
            "-v",
            f"db={db}",
            "-d",
            settings.DATABASE_URL_SYNC,
            "-f",
            "-",
        ],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _app_cursor():
    parts = urlsplit(settings.DATABASE_URL_SYNC)
    return _cursor(
        host=parts.hostname or "localhost",
        port=parts.port or 5432,
        dbname=parts.path.lstrip("/"),
        user=ROLE,
        password=PASSWORD,
    )


def _refused(cur, sql: str) -> bool:
    try:
        cur.execute(sql)
    except psycopg2.Error as error:
        return (
            error.pgcode in ("42501", "42P01")
            or "permission denied" in str(error)
            or "must be" in str(error)
        )
    return False


@pytest.fixture
def app_role(_engine):
    if _psql() is None:
        pytest.skip("needs psql")
    with _cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS alembic_version (version_num varchar(32) PRIMARY KEY)"
        )
        cur.execute(
            "INSERT INTO alembic_version VALUES ('stability_indexes') ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS app_role_items (id serial PRIMARY KEY, name text)"
        )
    yield ROLE
    with _cursor() as cur:
        for table in ("app_role_items", "app_role_later", "app_role_made_by_app"):
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (ROLE,))
        if cur.fetchone():
            cur.execute(f"DROP OWNED BY {ROLE}")
            cur.execute(f"DROP ROLE {ROLE}")


async def test_the_app_role_reads_and_writes_rows_and_changes_nothing_else(app_role):
    run = _apply()
    assert run.returncode == 0, run.stdout + run.stderr
    assert "every table and sequence in public is granted" in run.stdout
    assert PASSWORD not in run.stdout + run.stderr

    with _app_cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        assert cur.fetchone() == (False, False, False, False)
        # Rows and ids: yes.
        cur.execute("INSERT INTO app_role_items (name) VALUES ('a') RETURNING id")
        cur.execute("UPDATE app_role_items SET name = 'b'")
        cur.execute("SELECT count(*) FROM app_role_items")
        cur.execute("DELETE FROM app_role_items")
        cur.execute("SELECT nextval(pg_get_serial_sequence('app_role_items', 'id'))")
        cur.execute("SELECT count(*) FROM users")
        cur.execute("SELECT version_num FROM alembic_version")
        assert cur.fetchone() == ("stability_indexes",)
        # The schema, the migration head, programs, databases, roles: no.
        assert _refused(cur, "UPDATE alembic_version SET version_num = 'x'")
        assert _refused(cur, "CREATE TABLE app_role_made_by_app (x int)")
        assert _refused(cur, "ALTER TABLE users ADD COLUMN app_role_x int")
        assert _refused(cur, "DROP TABLE app_role_items")
        assert _refused(cur, "TRUNCATE app_role_items")
        assert _refused(cur, "CREATE INDEX ON app_role_items (name)")
        assert _refused(cur, "COPY app_role_items TO PROGRAM 'true'")
        assert _refused(cur, "CREATE DATABASE app_role_db")
        assert _refused(cur, "CREATE ROLE app_role_other")


async def test_what_the_owner_creates_later_is_granted_and_a_second_run_is_the_same(
    app_role,
):
    assert _apply().returncode == 0
    with _cursor() as cur:
        cur.execute(
            "CREATE TABLE app_role_later (id bigserial PRIMARY KEY, note text)"
        )  # a later migration
    with _app_cursor() as cur:
        cur.execute("INSERT INTO app_role_later (note) VALUES ('x')")
        cur.execute("SELECT count(*) FROM app_role_later")
        assert cur.fetchone() == (1,)
    again = _apply()
    assert again.returncode == 0, again.stdout + again.stderr


async def test_it_refuses_to_make_the_owner_the_app_role(app_role):
    with _cursor() as cur:
        cur.execute("SELECT current_user")
        (owner,) = cur.fetchone()
    run = _apply(role=owner)
    assert (
        run.returncode != 0 and "must not be the owner role" in run.stdout + run.stderr
    )
