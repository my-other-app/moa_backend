"""The daily off-site backup and the monthly restore drill, as the server
runs them:

- end to end on a local Postgres and moto's S3 -- real boto3 calls, so the
  upload's encryption, metadata and listing are S3's own answers: pg_dump
  (compressed), the upload under a dated key with SSE, local retention, then
  the drill downloading the newest, restoring it into a scratch database and
  counting it, against the live database or on its own (--standalone, for
  staging);
- the failure path: scripts/backup.py --report-failure, the one alert;
- ops/backup: the systemd units (daily at 02:30 IST, monthly drill, the
  OnFailure alert) and the two shell scripts they run, with docker-compose,
  journalctl and curl faked on PATH."""
import hashlib
import io
import os
import re
import shutil
import subprocess
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import pytest

from app.api.backups import offsite, service
from app.config import settings
from scripts import backup as backup_script
from scripts import restore_drill
from tests.test_backup_offsite import _pg_bin, source_db  # noqa: F401 -- the fixture

REPO = Path(__file__).resolve().parents[2]
OPS = REPO / "ops" / "backup"
SECRET = "wJalrXUtnFEMI-not-real-K7MDENGbPxRfiCY"
DATED_KEY = re.compile(r"^backups/(daily|weekly)/backup-(\d{8})-\d{6}\.dump$")


@pytest.fixture
def moto_s3(monkeypatch, tmp_path):
    moto = pytest.importorskip("moto")
    import boto3

    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="ap-south-1", aws_access_key_id="AKIAMOTO", aws_secret_access_key="moto")
        s3.create_bucket(Bucket="moa-backups", CreateBucketConfiguration={"LocationConstraint": "ap-south-1"})
        monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
        monkeypatch.setattr(settings, "BACKUP_S3_BUCKET", "moa-backups")
        monkeypatch.setattr(settings, "BACKUP_S3_PREFIX", "backups/")
        monkeypatch.setattr(settings, "BACKUP_S3_REGION", "ap-south-1")
        monkeypatch.setattr(settings, "BACKUP_S3_SSE", "AES256")
        monkeypatch.setattr(settings, "BACKUP_S3_ACCESS_KEY", "AKIABACKUPONLY12345")
        monkeypatch.setattr(settings, "BACKUP_S3_SECRET_KEY", SECRET)
        yield s3


def _old_dump(directory: Path, day: int) -> Path:
    path = directory / f"backup-202610{day:02d}-210000.dump"
    path.write_bytes(b"PGDMP old")
    stamp = datetime(2026, 10, day, 21, 0, tzinfo=timezone.utc).timestamp()
    os.utime(path, (stamp, stamp))
    return path


def _compression(dump: Path) -> str:
    listing = subprocess.run(["pg_restore", "-l", str(dump)], capture_output=True, text=True, check=True).stdout
    match = re.search(r"Compression:\s*(\S+)", listing)
    assert match, listing[:500]
    return match.group(1)


async def test_backup_to_s3_then_the_drill_end_to_end(moto_s3, source_db, tmp_path):
    url, password = source_db
    for day in (1, 2, 3):
        _old_dump(tmp_path, day)
    out = io.StringIO()
    with redirect_stdout(out):
        assert await backup_script.run(keep=2, notify=False) == 0, out.getvalue()

    # Local retention: the new dump and the newest old one.
    local = sorted(p.name for p in tmp_path.glob("backup-*.dump"))
    assert len(local) == 2 and local[0] == "backup-20261003-210000.dump"
    [dump] = [tmp_path / n for n in local if n != "backup-20261003-210000.dump"]
    assert _compression(dump) not in ("0", "none"), "custom format, compressed"

    # Off-site: a dated key, encrypted at rest, its checksum in the metadata.
    [obj] = moto_s3.list_objects_v2(Bucket="moa-backups", Prefix="backups/")["Contents"]
    match = DATED_KEY.match(obj["Key"])
    assert match, obj["Key"]
    assert match.group(2) == datetime.now(timezone.utc).strftime("%Y%m%d")
    head = moto_s3.head_object(Bucket="moa-backups", Key=obj["Key"])
    assert head["ServerSideEncryption"] == "AES256"
    assert head["Metadata"]["sha256"] == hashlib.sha256(dump.read_bytes()).hexdigest()

    # The drill: newest from S3, checksum, restore, counts -- against the
    # live database, and on its own as staging runs it.
    with redirect_stdout(out):
        assert restore_drill.run(source="s3") == 0, out.getvalue()
        assert restore_drill.run(source="s3", standalone=True, max_age_hours=26) == 0, out.getvalue()
    text = out.getvalue()
    assert text.count("RESTORE DRILL PASSED") == 2
    assert "sha256 matches the upload" in text
    assert "every table in the dump was restored" in text
    assert password not in text and SECRET not in text
    assert not list(tmp_path.glob(".restore-drill-*")), "the downloaded copy is deleted"


async def test_kms_encryption_reaches_s3(moto_s3, source_db, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_S3_SSE", "aws:kms")
    out = io.StringIO()
    with redirect_stdout(out):
        assert await backup_script.run(keep=0, notify=False) == 0, out.getvalue()
    [obj] = moto_s3.list_objects_v2(Bucket="moa-backups")["Contents"]
    assert moto_s3.head_object(Bucket="moa-backups", Key=obj["Key"])["ServerSideEncryption"] == "aws:kms"


async def test_the_standalone_drill_fails_on_an_empty_restore(source_db, tmp_path, monkeypatch):
    url, _ = source_db
    conn = psycopg2.connect(url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE users")
    conn.close()
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))
    await service.create_backup()
    out = io.StringIO()
    with redirect_stdout(out):
        assert restore_drill.run(source="local", standalone=True) == 1
    assert "users restored empty" in out.getvalue()


def test_a_table_missing_from_the_restore_is_caught():
    toc = (
        ";\n; Archive created at 2026-10-24 02:30:01 IST\n"
        "215; 1259 16390 TABLE public users moa\n"
        "216; 1259 16391 TABLE public clubs moa\n"
        "3401; 0 16390 TABLE DATA public users moa\n"
        "217; 1259 16392 SEQUENCE public users_id_seq moa\n"
    )
    assert restore_drill.tables_in_toc(toc) == {"users", "clubs"}


# ---------------------------------------------------------------------------
# The one alert
# ---------------------------------------------------------------------------


async def test_report_failure_raises_the_admin_alert(monkeypatch):
    from app.api import admin_alerts

    raised = []

    async def notify(session, kind, **kw):
        raised.append((kind, kw["title"], kw["body"]))
        return object()

    monkeypatch.setattr(admin_alerts.service, "notify", notify)
    monkeypatch.setattr(settings, "BACKUP_S3_SECRET_KEY", SECRET)
    status = await backup_script.report_failure("The daily database backup failed on box", f"upload refused for key {SECRET}")
    assert status == 0
    [(kind, title, body)] = raised
    assert kind == "backup_failed" and title.startswith("The daily database backup failed")
    assert "upload refused" in body and SECRET not in body  # this process's own keys are scrubbed


async def test_report_failure_without_the_database_still_mails_and_posts(monkeypatch):
    from app.api import admin_alerts

    async def notify(session, kind, **kw):
        raise ConnectionRefusedError("database down")

    mailed, posted = [], []
    monkeypatch.setattr(admin_alerts.service, "notify", notify)
    monkeypatch.setattr(admin_alerts.service, "send_email", lambda alert: mailed.append(alert) or True)
    monkeypatch.setattr(admin_alerts.service, "post_discord", lambda alert: posted.append(alert) or False)
    assert await backup_script.report_failure("The daily database backup failed", "the database is down") == 0
    [alert] = mailed
    assert alert["kind"] == "backup_failed" and alert["title"] == "The daily database backup failed"
    assert posted == [alert]  # tried both; the mail alone is enough


async def test_report_failure_says_when_nobody_was_told(monkeypatch):
    from app.api import admin_alerts

    async def notify(session, kind, **kw):
        raise ConnectionRefusedError("database down")

    monkeypatch.setattr(admin_alerts.service, "notify", notify)
    # Neither ever raises: each says False when it could not (no address, no webhook, a refusal).
    monkeypatch.setattr(admin_alerts.service, "send_email", lambda alert: False)
    monkeypatch.setattr(admin_alerts.service, "post_discord", lambda alert: False)
    assert await backup_script.report_failure("title", "body") == 1


async def test_no_alert_leaves_the_alert_to_systemd(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "BACKUP_DIR", str(tmp_path))

    async def broken():
        raise service.CustomHTTPException(500, message="pg_dump failed: connection refused")

    alerts = []

    async def alert(title, body):
        alerts.append(title)

    monkeypatch.setattr(service, "create_backup", broken)
    monkeypatch.setattr(backup_script, "alert", alert)
    out = io.StringIO()
    with redirect_stdout(out):
        assert await backup_script.run(keep=0, notify=False) == 1
    assert alerts == []
    assert backup_script.parse_args(["--no-alert"]).no_alert is True


# ---------------------------------------------------------------------------
# ops/backup: the units and the scripts
# ---------------------------------------------------------------------------


def _unit(name: str) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for line in (OPS / "systemd" / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key, []).append(value)
    return sections


def test_the_daily_timer_is_0230_india_time_and_catches_up():
    timer = _unit("myotherapp-backup.timer")
    assert timer["Timer"]["OnCalendar"] == ["*-*-* 02:30:00 Asia/Kolkata"]
    assert timer["Timer"]["Persistent"] == ["true"]
    assert timer["Timer"]["Unit"] == ["myotherapp-backup.service"]
    assert timer["Install"]["WantedBy"] == ["timers.target"]
    drill = _unit("myotherapp-restore-drill.timer")
    assert drill["Timer"]["OnCalendar"][0].startswith("*-*-01 ")  # monthly


@pytest.mark.parametrize("name, what", [("myotherapp-backup.service", "backup"), ("myotherapp-restore-drill.service", "drill")])
def test_the_services_run_the_script_and_alert_on_failure(name, what):
    unit = _unit(name)
    assert unit["Unit"]["OnFailure"] == ["myotherapp-alert@%N.service"]
    assert "docker.service" in unit["Unit"]["Requires"][0]
    assert unit["Service"]["Type"] == ["oneshot"]
    [exec_start] = unit["Service"]["ExecStart"]
    assert exec_start.startswith(f"/bin/bash ${{MOA_PROJECT_DIR}}/ops/backup/run.sh {what}")
    assert (OPS / "run.sh").exists()
    alert = _unit("myotherapp-alert@.service")
    assert alert["Service"]["ExecStart"] == ["/bin/bash ${MOA_PROJECT_DIR}/ops/backup/alert.sh %i"]


@pytest.mark.parametrize("script", ["run.sh", "alert.sh"])
def test_the_scripts_parse(script):
    subprocess.run(["bash", "-n", str(OPS / script)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(OPS / script)], check=True)


def _fake_bin(tmp_path: Path, **scripts: str) -> dict:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in scripts.items():
        path = bindir / name.replace("_", "-")
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "MOA_PROJECT_DIR": str(tmp_path)}


def test_run_sh_execs_the_script_in_the_backend_container(tmp_path):
    calls = tmp_path / "calls"
    env = _fake_bin(tmp_path, docker_compose=f'echo "$@" >> {calls}')
    subprocess.run(["bash", str(OPS / "run.sh"), "backup"], env=env, check=True, capture_output=True)
    subprocess.run(["bash", str(OPS / "run.sh"), "drill", "--standalone", "--max-age-hours", "26"], env=env, check=True, capture_output=True)
    assert calls.read_text().splitlines() == [
        "-f docker-compose.prod.yml exec -T backend python scripts/backup.py --no-alert",
        "-f docker-compose.prod.yml exec -T backend python scripts/restore_drill.py --standalone --max-age-hours 26",
    ]
    bad = subprocess.run(["bash", str(OPS / "run.sh"), "restore-everything"], env=env, capture_output=True)
    assert bad.returncode == 64


JOURNAL = 'echo "dump: backup-20261024-210000.dump"; echo "pg_dump: connection to postgresql://moa:hunter2-pw@postgres:5432/moa failed"'


def _alert(tmp_path, compose_status: int):
    posted = tmp_path / "posted"
    (tmp_path / ".env").write_text(
        f"APP_SECRET_KEY={SECRET}\nAPP_ADMIN_ALERT_DISCORD_WEBHOOK=\"https://discord.test/api/webhooks/1/abc\"\n"
    )
    env = _fake_bin(
        tmp_path,
        docker_compose=f"cat > {tmp_path}/app-alert; exit {compose_status}",
        journalctl=JOURNAL,
        curl=f'while [ $# -gt 0 ]; do case "$1" in -d) shift; printf "%s" "$1" > {posted}.json;; https://*) echo "$1" > {posted}.url;; esac; shift; done',
        logger=f'echo "$@" > {tmp_path}/logged',
    )
    run = subprocess.run(["bash", str(OPS / "alert.sh"), "myotherapp-backup"], env=env, capture_output=True, text=True)
    return run, posted


def test_alert_sh_goes_through_the_app_first(tmp_path):
    run, posted = _alert(tmp_path, compose_status=0)
    assert run.returncode == 0, run.stderr
    body = (tmp_path / "app-alert").read_text()
    assert "myotherapp-backup ended with failed" in body and "hunter2-pw" not in body
    assert not posted.with_suffix(".url").exists()


def test_alert_sh_posts_to_discord_when_the_app_cannot(tmp_path):
    run, posted = _alert(tmp_path, compose_status=1)
    assert run.returncode == 0, run.stderr
    assert posted.with_suffix(".url").read_text().strip() == "https://discord.test/api/webhooks/1/abc"
    payload = posted.with_suffix(".json").read_text()
    assert "The daily database backup failed on" in payload
    assert "hunter2-pw" not in payload and SECRET not in payload and SECRET not in run.stdout + run.stderr


def test_alert_sh_logs_when_nobody_can_be_told(tmp_path):
    run, _ = _alert(tmp_path, compose_status=1)
    (tmp_path / ".env").write_text("APP_SECRET_KEY=x\n")
    run = subprocess.run(
        ["bash", str(OPS / "alert.sh"), "myotherapp-restore-drill"],
        env=_fake_bin(tmp_path), capture_output=True, text=True,
    )
    assert run.returncode == 1
    assert "The monthly restore drill failed" in (tmp_path / "logged").read_text()
