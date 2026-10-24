"""Restore drill: the latest backup, restored into a scratch database and
checked.

    docker-compose -f docker-compose.prod.yml exec -T backend python scripts/restore_drill.py
        --source local          the newest dump in BACKUP_DIR rather than S3
        --file NAME             that dump in BACKUP_DIR
        --keep                  leave the scratch database for a look (drop it yourself)
        --max-age-hours 26      fail when the dump is older than that (a missed backup)
        --standalone            check the restore on its own, not against this
                                server's database (staging: its database is not
                                the one backed up)

A backup nobody has restored is a hope, not a backup. Run this monthly, and
after any change to how backups are taken (docs/OPERATIONS.md, "Restore";
the monthly systemd timer for staging is in ops/backup, section 19).

What it does:
1. takes the newest dump under the off-site prefix (daily/ and weekly/;
   needs s3:ListBucket and s3:GetObject there), downloads it next to the
   local ones and checks it against the sha256 recorded at upload -- or the
   newest local one with --source local;
2. creates a new database, restore_drill_YYYYMMDD_HHMMSS, on the same server
   (or on the server of RESTORE_DRILL_DATABASE_URL, an environment variable
   naming any database there the drill may connect to and create from);
3. restores into it with pg_restore and fails on any error it reports, or
   on any table the dump holds (pg_restore -l) that the restore lacks;
4. reads it: the alembic revision (beside the live one), and the rows in the
   core tables beside the live database's, failing when a table is missing
   or empty where the live one has rows -- or, --standalone, when users,
   clubs or events restored empty;
5. drops the scratch database (unless --keep) and the downloaded file.

The restore costs CPU and disk on the server it runs on: off-peak on the
production box, or point RESTORE_DRILL_DATABASE_URL elsewhere. Read-only
towards the live database. Credentials go to pg_restore through its
environment, never its arguments, and nothing printed carries one.
Exit status 0 and "RESTORE DRILL PASSED", or 1 and the reasons.
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlsplit, urlunsplit

sys.path.insert(0, ".")

import psycopg2  # noqa: E402

from app.api.backups import offsite, service  # noqa: E402
from app.config import settings  # noqa: E402

#: Counted in the restore and in the live database, side by side.
CORE_TABLES = (
    "users", "clubs", "events", "event_registrations_link", "club_users_link",
    "payment_logs", "payment_orders", "notifications",
)
#: A live table with at least this many rows must not restore empty (one
#: that got its first rows since the dump may).
EMPTY_MATTERS_FROM = 10
#: --standalone: no live database to compare with, so these must simply have rows.
NEVER_EMPTY = ("users", "clubs", "events")
#: A table's line in `pg_restore -l`: "215; 1259 16390 TABLE public users moa".
TOC_TABLE = re.compile(r"^\d+;\s+\d+\s+\d+\s+TABLE\s+(\S+)\s+(\S+)(?:\s|$)")
#: pg_restore errors that mean nothing for a restore into a new database.
BENIGN = (
    'schema "public" already exists',
    'extension "plpgsql" already exists',
)
STAMP = re.compile(r"backup-(\d{8}-\d{6})")


def say(line: str = "") -> None:
    print(offsite.scrub(line), flush=True)


def _sync_url(url: str) -> str:
    return re.sub(r"^postgresql\+\w+://", "postgresql://", url)


def _params(url: str) -> dict:
    parts = urlsplit(_sync_url(url))
    return {
        "host": parts.hostname or "localhost",
        "port": str(parts.port or 5432),
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "dbname": (parts.path or "/").lstrip("/"),
    }


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(_sync_url(url))
    return urlunsplit(parts._replace(path=f"/{name}"))


def _connect(url: str, autocommit: bool = True):
    conn = psycopg2.connect(_sync_url(url))
    conn.autocommit = autocommit
    return conn


def _admin(url: str, sql: str) -> None:
    """One statement (CREATE or DROP DATABASE) on its own connection."""
    conn = _connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


def _age_hours(name: str, fallback: Optional[datetime] = None) -> Optional[float]:
    match = STAMP.search(name)
    when = datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc) if match else fallback
    return (datetime.now(timezone.utc) - when).total_seconds() / 3600 if when else None


def pick(source: str, file: Optional[str], s3=None) -> tuple[Path, Optional[str], bool, Optional[float]]:
    """The dump to restore: (path, recorded sha256, whether it was downloaded
    and must be deleted, its age in hours)."""
    if file:
        path = service.path_for_download(file)
        return path, None, False, _age_hours(path.name)
    if source == "local":
        local = service.list_backups()
        if not local:
            raise SystemExit(f"no dumps in {service.backup_dir()}")
        return service.backup_dir() / local[0].name, None, False, _age_hours(local[0].name, local[0].created_at)
    if not offsite.enabled():
        raise SystemExit("APP_BACKUP_S3_BUCKET is not set: use --source local, or set it")
    remote = offsite.remote_dumps(s3)
    if not remote:
        raise SystemExit(f"no dumps under s3://{settings.BACKUP_S3_BUCKET}/{offsite.key_for('', '')}")
    newest = remote[0]
    directory = service.backup_dir()
    directory.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=".restore-drill-", suffix=".dump", dir=directory)
    os.close(handle)
    say(f"downloading s3://{settings.BACKUP_S3_BUCKET}/{newest.key} ({newest.size_bytes} bytes)")
    offsite.download(newest, Path(temp), s3)
    return Path(temp), newest.sha256 or "", True, _age_hours(newest.name, newest.modified)


def restore_errors(stderr: str, returncode: int) -> list[str]:
    """The errors in pg_restore's output that matter. Each starts at a
    "pg_restore: error:" line and runs to the next "pg_restore:" line (the
    "Command was:" that follows belongs to it). pg_restore exits 1 after
    ignoring even a harmless error, so the exit status alone says little."""
    blocks, current = [], None
    for line in (stderr or "").splitlines():
        if line.startswith("pg_restore: error:"):
            current = [line.strip()]
            blocks.append(current)
        elif line.startswith("pg_restore:"):
            current = None
        elif current is not None and line.strip():
            current.append(line.strip())
    errors = [" / ".join(block)[:300] for block in blocks if not any(b in block[0] for b in BENIGN)]
    if returncode != 0 and not blocks:
        tail = [line for line in (stderr or "").splitlines() if line.strip()][-3:]
        errors = [f"pg_restore exited {returncode}" + (": " + " / ".join(tail) if tail else "")]
    return errors


def tables_in_toc(listing: str) -> set[str]:
    """The public tables a dump holds, from its `pg_restore -l` listing."""
    out = set()
    for line in listing.splitlines():
        match = TOC_TABLE.match(line.strip())
        if match and match.group(1) == "public":
            out.add(match.group(2))
    return out


def dump_tables(dump: Path) -> set[str]:
    listing = subprocess.run(["pg_restore", "-l", str(dump)], capture_output=True, text=True, timeout=600, check=True)
    return tables_in_toc(listing.stdout)


def restored_tables(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in cur.fetchall()}


def restore(dump: Path, server_url: str, name: str) -> list[str]:
    """pg_restore `dump` into the new database `name`; the errors it reported
    that matter (empty when it went cleanly)."""
    params = _params(server_url)
    env = {**os.environ, "PGPASSWORD": params["password"]}
    run = subprocess.run(
        [
            "pg_restore", "--no-owner", "--no-acl",
            f"--host={params['host']}", f"--port={params['port']}", f"--username={params['user']}",
            f"--dbname={name}", str(dump),
        ],
        env=env, capture_output=True, text=True, timeout=6 * 3600,
    )
    return restore_errors(run.stderr, run.returncode)


def _count(conn, table: str, *, exact: bool = True) -> Optional[tuple[int, bool]]:
    """(rows, exact) for `table`, None when it does not exist. The live side
    is counted under a short statement_timeout and falls back to the
    planner's estimate, so a big table is never scanned at length."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
        if cur.fetchone()[0] is None:
            return None
        if not exact:
            cur.execute("SET statement_timeout = '5s'")
        try:
            cur.execute(f'SELECT count(*) FROM public."{table}"')
            return int(cur.fetchone()[0]), True
        except psycopg2.errors.QueryCanceled:
            cur.execute("SELECT greatest(reltuples, 0)::bigint FROM pg_class WHERE oid = to_regclass(%s)", (f"public.{table}",))
            return int(cur.fetchone()[0]), False


def _revision(conn) -> Optional[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.alembic_version')")
        if cur.fetchone()[0] is None:
            return None
        cur.execute("SELECT string_agg(version_num, ',') FROM alembic_version")
        return cur.fetchone()[0]


def check_standalone(scratch) -> list[str]:
    """Read the restore on its own (staging); the problems found."""
    problems = []
    revision = _revision(scratch)
    say(f"alembic revision: restored {revision or '(none)'}")
    if not revision:
        problems.append("the restore has no alembic_version: not a dump of this application's database")
    say(f"{'table':<28}{'restored':>12}")
    for table in CORE_TABLES:
        restored = _count(scratch, table)
        say(f"{table:<28}{'missing' if restored is None else restored[0]:>12}")
        if table in NEVER_EMPTY and (restored is None or restored[0] == 0):
            problems.append(f"{table} restored empty" if restored is not None else f"{table} is missing from the restore")
    return problems


def check(scratch, live) -> list[str]:
    """Read the restore beside the live database; the problems found."""
    problems = []
    restored_rev, live_rev = _revision(scratch), _revision(live)
    say(f"alembic revision: restored {restored_rev or '(none)'}, live {live_rev or '(none)'}")
    if not restored_rev:
        problems.append("the restore has no alembic_version: not a dump of this application's database")
    say(f"{'table':<28}{'restored':>12}{'live':>14}")
    for table in CORE_TABLES:
        restored = _count(scratch, table)
        current = _count(live, table, exact=False)
        if current is None and restored is None:
            continue
        live_text = "-" if current is None else f"{'' if current[1] else '~'}{current[0]}"
        say(f"{table:<28}{'missing' if restored is None else restored[0]:>12}{live_text:>14}")
        if restored is None:
            problems.append(f"{table} is missing from the restore")
        elif restored[0] == 0 and current is not None and current[0] >= EMPTY_MATTERS_FROM:
            problems.append(f"{table} restored empty while the live one has {current[0]} rows")
    return problems


def run(*, source: str = "s3", file: Optional[str] = None, keep: bool = False,
        max_age_hours: Optional[float] = None, standalone: bool = False, s3=None) -> int:
    live_url = settings.DATABASE_URL_SYNC
    server_url = os.environ.get("RESTORE_DRILL_DATABASE_URL") or live_url
    name = f"restore_drill_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    problems: list[str] = []
    dump, downloaded, created = None, False, False
    try:
        dump, recorded, downloaded, age = pick(source, file, s3)
        say(f"dump: {dump.name if not downloaded else 'downloaded copy'} ({dump.stat().st_size} bytes)"
            + (f", {age:.1f} hours old" if age is not None else ""))
        if recorded is not None and downloaded:
            actual = offsite.sha256_of(dump)
            if not recorded:
                problems.append("the object has no sha256 in its metadata: cannot tell whether the download is intact")
            elif actual != recorded:
                problems.append(f"sha256 mismatch: recorded {recorded}, downloaded {actual}")
            else:
                say(f"sha256 matches the upload: {actual}")
        if max_age_hours is not None and age is not None and age > max_age_hours:
            problems.append(f"the newest dump is {age:.1f} hours old (more than {max_age_hours}): a backup was missed")
        if problems:
            return _done(problems)

        params = _params(server_url)
        say(f"restoring into a new database {name} on {params['host']}:{params['port']}")
        _admin(server_url, f'CREATE DATABASE "{name}"')
        created = True
        started = datetime.now(timezone.utc)
        errors = restore(dump, server_url, name)
        say(f"pg_restore took {(datetime.now(timezone.utc) - started).total_seconds():.1f}s")
        if errors:
            problems.append(f"pg_restore reported {len(errors)} error(s):")
            problems.extend(f"  {line}" for line in errors[:20])

        scratch = _connect(_with_db(server_url, name))
        try:
            expected, present = dump_tables(dump), restored_tables(scratch)
            lost = sorted(expected - present)
            if lost:
                problems.append(f"{len(lost)} table(s) in the dump are missing from the restore: {', '.join(lost[:20])}")
            else:
                say(f"every table in the dump was restored ({len(expected)})")
            if standalone:
                problems.extend(check_standalone(scratch))
            else:
                live = _connect(live_url)
                try:
                    with live.cursor() as cur:
                        # Never anything but reads on the live database.
                        cur.execute("SET default_transaction_read_only = on")
                    problems.extend(check(scratch, live))
                finally:
                    live.close()
        finally:
            scratch.close()
        return _done(problems)
    finally:
        if created and not keep:
            try:
                _admin(server_url, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
                say(f"dropped {name}")
            except Exception as error:  # noqa: BLE001
                say(f"could not drop {name}: {type(error).__name__}: {error} -- drop it by hand")
        elif created:
            say(f"kept {name} (--keep): drop it with DROP DATABASE \"{name}\" when done")
        if downloaded and dump is not None:
            dump.unlink(missing_ok=True)


def _done(problems: list[str]) -> int:
    if problems:
        say("RESTORE DRILL FAILED:")
        for problem in problems:
            say(f"  {problem}")
        return 1
    say("RESTORE DRILL PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore the newest backup into a scratch database and check it.")
    parser.add_argument("--source", choices=("s3", "local"), default="s3" if offsite.enabled() else "local")
    parser.add_argument("--file", help="a dump in BACKUP_DIR, by name")
    parser.add_argument("--keep", action="store_true", help="leave the scratch database")
    parser.add_argument("--max-age-hours", type=float, default=None)
    parser.add_argument("--standalone", action="store_true", help="check the restore on its own, not against this server's database")
    args = parser.parse_args()
    return run(source=args.source, file=args.file, keep=args.keep, max_age_hours=args.max_age_hours, standalone=args.standalone)


if __name__ == "__main__":
    sys.exit(main())
