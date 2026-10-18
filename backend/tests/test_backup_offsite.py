"""Backups off the box: the upload of each dump (app/api/backups/offsite.py),
the daily script (scripts/backup.py) and the restore drill
(scripts/restore_drill.py).

S3 is an in-memory fake that records what it was asked. The drill's end to
end test is real: pg_dump of a small database, the upload, the download,
pg_restore into a scratch database, the counts, the cleanup."""
import getpass
import io
import os
import shutil
from contextlib import redirect_stdout
from datetime import datetime, timezone
from glob import glob
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg2
import pytest

from app.api.backups import offsite, service
from app.config import settings
from scripts import backup as backup_script
from scripts import restore_drill

SECRET = "AKIA-not-a-real-secret-9f8e7d"


class FakeS3:
    """The calls offsite.py makes, kept in a dict."""

    def __init__(self, fail_upload: Exception | None = None):
        self.objects: dict[tuple[str, str], dict] = {}
        self.uploads: list[dict] = []
        self.fail_upload = fail_upload

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        if self.fail_upload:
            raise self.fail_upload
        self.uploads.append({"bucket": bucket, "key": key, "extra": ExtraArgs})
        self.objects[(bucket, key)] = {
            "body": Path(filename).read_bytes(),
            "meta": dict((ExtraArgs or {}).get("Metadata") or {}),
            "modified": datetime.now(timezone.utc),
        }

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        s3 = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [
                    {"Key": k, "Size": len(v["body"]), "LastModified": v["modified"]}
                    for (b, k), v in s3.objects.items() if b == Bucket and k.startswith(Prefix)
                ]}

        return Paginator()

    def head_object(self, Bucket, Key):
        return {"Metadata": self.objects[(Bucket, Key)]["meta"]}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)]["body"])


@pytest.fixture
def offsite_on(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "BACKUP_S3_BUCKET", "moa-backups")
    monkeypatch.setattr(settings, "BACKUP_S3_PREFIX", "backups/")
    monkeypatch.setattr(settings, "BACKUP_S3_SSE", "AES256")
    monkeypatch.setattr(settings, "BACKUP_S3_SECRET_KEY", SECRET)
    s3 = FakeS3()
    monkeypatch.setattr(offsite, "client", lambda: s3)
    return s3


def _dump(directory: Path, name: str, body: bytes = b"PGDMP fake") -> Path:
    path = directory / name
    path.write_bytes(body)
    return path


# ---------------------------------------------------------------------------
# offsite.py
# ---------------------------------------------------------------------------


def test_off_by_default():
    assert settings.BACKUP_S3_BUCKET == ""
    assert not offsite.enabled()


def test_an_upload_is_encrypted_and_carries_its_checksum(offsite_on, tmp_path):
    path = _dump(tmp_path, "backup-20261018-213000.dump", b"x" * 1000)
    copy = offsite.upload(path, tier=offsite.DAILY)
    [call] = offsite_on.uploads
    assert call["bucket"] == "moa-backups"
    assert call["key"] == "backups/daily/backup-20261018-213000.dump" == copy.key
    assert call["extra"]["ServerSideEncryption"] == "AES256"
    assert call["extra"]["Metadata"]["sha256"] == offsite.sha256_of(path) == copy.sha256


def test_kms_encryption(offsite_on, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_S3_SSE", "aws:kms")
    monkeypatch.setattr(settings, "BACKUP_S3_KMS_KEY_ID", "alias/moa-backups")
    offsite.upload(_dump(tmp_path, "backup-20261018-213000.dump"))
    extra = offsite_on.uploads[0]["extra"]
    assert extra["ServerSideEncryption"] == "aws:kms" and extra["SSEKMSKeyId"] == "alias/moa-backups"
    monkeypatch.setattr(settings, "BACKUP_S3_SSE", "none")
    with pytest.raises(ValueError):
        offsite.encryption()


def test_weekly_on_sundays_and_when_one_is_overdue(tmp_path):
    sunday = datetime(2026, 10, 18, 21, 30, tzinfo=timezone.utc)
    monday = datetime(2026, 10, 19, 21, 30, tzinfo=timezone.utc)
    assert sunday.weekday() == offsite.WEEKLY_DAY
    assert offsite.tier_for(sunday, tmp_path, scheduled=True) == offsite.WEEKLY
    assert offsite.tier_for(sunday, tmp_path, scheduled=False) == offsite.DAILY  # by hand: a checkpoint
    # No weekly on record: this one is.
    assert offsite.tier_for(monday, tmp_path, scheduled=True) == offsite.WEEKLY
    (tmp_path / offsite.MARKER).write_text("2026-10-18")
    assert offsite.tier_for(monday, tmp_path, scheduled=True) == offsite.DAILY
    (tmp_path / offsite.MARKER).write_text("2026-10-10")  # the Sunday run was missed
    assert offsite.tier_for(monday, tmp_path, scheduled=True) == offsite.WEEKLY


def test_a_weekly_upload_is_noted(offsite_on, tmp_path):
    offsite.upload(_dump(tmp_path, "backup-20261018-213000.dump"), tier=offsite.WEEKLY)
    assert offsite_on.uploads[0]["key"].startswith("backups/weekly/")
    assert (tmp_path / offsite.MARKER).read_text() == datetime.now(timezone.utc).date().isoformat()


def test_scrub_hides_every_credential(monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_S3_SECRET_KEY", SECRET)
    monkeypatch.setattr(settings, "DATABASE_URL_SYNC", "postgresql://moa:pa%24%24word-123@postgres:5432/moa")
    text = (
        f"boom {SECRET} postgresql://moa:pa%24%24word-123@postgres:5432/moa "
        "and pa$$word-123 and redis://:hunter2-xyz@redis:6379/0"
    )
    out = offsite.scrub(text)
    for leaked in (SECRET, "pa%24%24word-123", "pa$$word-123", "hunter2-xyz"):
        assert leaked not in out
    assert "postgres:5432/moa" in out


# ---------------------------------------------------------------------------
# The admin endpoint's dump is copied too
# ---------------------------------------------------------------------------


async def test_copy_offsite_reports_failure_without_raising(offsite_on, tmp_path, monkeypatch):
    path = _dump(tmp_path, "backup-20261018-213000.dump")
    backup = service.Backup(name=path.name, size_bytes=10, created_at=datetime.now(timezone.utc))
    offsite_on.fail_upload = RuntimeError(f"AccessDenied for key {SECRET}")
    result = await service.copy_offsite(backup)
    assert result.key is None and "AccessDenied" in result.error and SECRET not in result.error
    offsite_on.fail_upload = None
    result = await service.copy_offsite(backup)
    assert result.key == "backups/daily/backup-20261018-213000.dump" and result.error is None


async def test_copy_offsite_is_none_when_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
    backup = service.Backup(name="backup-20261018-213000.dump", size_bytes=1, created_at=datetime.now(timezone.utc))
    assert await service.copy_offsite(backup) is None


# ---------------------------------------------------------------------------
# scripts/backup.py
# ---------------------------------------------------------------------------


def _fake_dumps(monkeypatch, tmp_path, names):
    """service.create_backup writing the next of `names`."""
    queue = list(names)

    async def create_backup():
        name = queue.pop(0)
        path = _dump(tmp_path, name)
        stamp = datetime.strptime(name[7:22], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        os.utime(path, (stamp.timestamp(), stamp.timestamp()))
        return service.Backup(name=name, size_bytes=path.stat().st_size, created_at=stamp)

    monkeypatch.setattr(service, "create_backup", create_backup)


async def test_the_daily_script_uploads_and_prunes(offsite_on, tmp_path, monkeypatch):
    for day in (10, 11, 12):
        _dump(tmp_path, f"backup-202610{day}-213000.dump")
        stamp = datetime(2026, 10, day, 21, 30, tzinfo=timezone.utc).timestamp()
        os.utime(tmp_path / f"backup-202610{day}-213000.dump", (stamp, stamp))
    _fake_dumps(monkeypatch, tmp_path, ["backup-20261013-213000.dump"])
    out = io.StringIO()
    with redirect_stdout(out):
        status = await backup_script.run(keep=2, notify=False)
    assert status == 0, out.getvalue()
    assert offsite_on.uploads[0]["key"] == "backups/weekly/backup-20261013-213000.dump"  # none on record yet
    assert sorted(p.name for p in tmp_path.glob("*.dump")) == ["backup-20261012-213000.dump", "backup-20261013-213000.dump"]
    assert "BACKUP OK" in out.getvalue()


async def test_the_daily_script_keeps_everything_when_the_upload_fails(offsite_on, tmp_path, monkeypatch):
    for day in (10, 11):
        _dump(tmp_path, f"backup-202610{day}-213000.dump")
    _fake_dumps(monkeypatch, tmp_path, ["backup-20261013-213000.dump"])
    offsite_on.fail_upload = RuntimeError(f"SignatureDoesNotMatch with {SECRET}")
    alerts = []

    async def alert(title, body):
        alerts.append((title, body))

    monkeypatch.setattr(backup_script, "alert", alert)
    out = io.StringIO()
    with redirect_stdout(out):
        status = await backup_script.run(keep=1)
    assert status == 1
    assert len(list(tmp_path.glob("*.dump"))) == 3  # nothing pruned
    assert "backup FAILED: the off-site copy" in out.getvalue()
    assert SECRET not in out.getvalue() and SECRET not in alerts[0][1]


async def test_the_daily_script_says_when_offsite_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
    _fake_dumps(monkeypatch, tmp_path, ["backup-20261013-213000.dump"])
    out = io.StringIO()
    with redirect_stdout(out):
        status = await backup_script.run(keep=0, notify=False)
    assert status == 0
    assert "off-site copy: off" in out.getvalue()


async def test_a_failed_dump_fails_the_script_and_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))

    async def broken():
        raise service.CustomHTTPException(500, message="pg_dump failed: server version mismatch")

    monkeypatch.setattr(service, "create_backup", broken)
    alerts = []

    async def alert(title, body):
        alerts.append(title)

    monkeypatch.setattr(backup_script, "alert", alert)
    out = io.StringIO()
    with redirect_stdout(out):
        assert await backup_script.run(keep=0) == 1
    assert "server version mismatch" in out.getvalue()
    assert alerts == ["The daily database backup failed"]


# ---------------------------------------------------------------------------
# pg_restore's output
# ---------------------------------------------------------------------------


def test_restore_errors_ignore_the_harmless_ones():
    benign = (
        "pg_restore: error: could not execute query: ERROR:  schema \"public\" already exists\n"
        "Command was: CREATE SCHEMA public;\n\n"
        "pg_restore: warning: errors ignored on restore: 1\n"
    )
    assert restore_drill.restore_errors(benign, 1) == []
    real = (
        "pg_restore: error: could not execute query: ERROR:  type \"geography\" does not exist\n"
        "Command was: CREATE TABLE public.places (...);\n"
        "pg_restore: warning: errors ignored on restore: 1\n"
    )
    [error] = restore_drill.restore_errors(real, 1)
    assert "geography" in error and "CREATE TABLE public.places" in error
    assert restore_drill.restore_errors("pg_restore: [archiver] input file is too short\n", 1)


# ---------------------------------------------------------------------------
# The drill, end to end, on real Postgres
# ---------------------------------------------------------------------------


def _pg_bin() -> str | None:
    """A directory with pg_dump and pg_restore, or None (then the test skips)."""
    found = shutil.which("pg_dump")
    if found and shutil.which("pg_restore"):
        return str(Path(found).parent)
    for candidate in sorted(glob("/opt/homebrew/opt/postgresql@*/bin") + glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if Path(candidate, "pg_dump").exists() and Path(candidate, "pg_restore").exists():
            return candidate
    return None


@pytest.fixture
def source_db(monkeypatch):
    """A small database shaped like ours, reached by a URL with a password in
    it (a made-up one where the local server does not ask)."""
    bindir = _pg_bin()
    if bindir is None:
        pytest.skip("pg_dump/pg_restore are not installed")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    parts = urlsplit(settings.DATABASE_URL_SYNC)
    name = parts.path.lstrip("/") + "_drill_src"
    user = parts.username or getpass.getuser()
    password = parts.password or "drill-pw-not-real-42"
    netloc = f"{user}:{password}@{parts.hostname or 'localhost'}:{parts.port or 5432}"
    url = urlunsplit(parts._replace(netloc=netloc, path=f"/{name}"))
    admin = psycopg2.connect(urlunsplit(parts._replace(path="/postgres")))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cur.execute(f'CREATE DATABASE "{name}"')
    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        cur.execute("INSERT INTO alembic_version VALUES ('topics')")
        for table in ("users", "clubs", "events", "event_registrations_link"):
            cur.execute(f"CREATE TABLE {table} (id serial PRIMARY KEY, note text)")
            cur.execute(f"INSERT INTO {table} (note) SELECT 'row ' || g FROM generate_series(1, 25) g")
    conn.close()
    monkeypatch.setattr(settings, "DATABASE_URL_SYNC", url)
    yield url, password
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        cur.execute("SELECT datname FROM pg_database WHERE datname LIKE 'restore_drill_%%'")
        leftovers = [r[0] for r in cur.fetchall()]
    admin.close()
    assert not leftovers, f"the drill left {leftovers} behind"


async def test_backup_then_restore_drill_end_to_end(offsite_on, source_db, tmp_path):
    url, password = source_db
    out = io.StringIO()
    with redirect_stdout(out):
        assert await backup_script.run(keep=0, notify=False) == 0, out.getvalue()
        assert restore_drill.run(source="s3") == 0, out.getvalue()
    text = out.getvalue()
    assert "sha256 matches the upload" in text
    assert "RESTORE DRILL PASSED" in text
    assert "users" in text and "25" in text
    assert password not in text and SECRET not in text
    assert not list(tmp_path.glob(".restore-drill-*")), "the downloaded copy is deleted"


async def test_the_drill_catches_a_corrupted_download(offsite_on, source_db, tmp_path):
    out = io.StringIO()
    with redirect_stdout(out):
        assert await backup_script.run(keep=0, notify=False) == 0
        [(key, obj)] = offsite_on.objects.items()
        obj["body"] = obj["body"][:-10] + b"0123456789"
        assert restore_drill.run(source="s3") == 1
    assert "sha256 mismatch" in out.getvalue()


async def test_the_drill_from_a_local_dump(source_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
    await service.create_backup()
    out = io.StringIO()
    with redirect_stdout(out):
        assert restore_drill.run(source="local") == 0, out.getvalue()
    assert "RESTORE DRILL PASSED" in out.getvalue()
