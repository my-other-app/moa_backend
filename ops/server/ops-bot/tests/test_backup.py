"""deliver-backup: encrypt first, then send -- never a plaintext dump --
and the >50 MB and failure branches."""

import calendar
import os
import shutil
import subprocess
import time
from dataclasses import replace

import pytest
from conftest import FakeRunner, FakeTelegram

from opsbot import backup, main
from opsbot.system import CommandError, Runner
from opsbot.telegram import AGE_HEADER, NotEncrypted

PLAIN = b"PGDMP\x01\x0e\x00" + b"users clubs events secret-row-data " * 200
TOC = "\n".join(
    [
        ";",
        "; Archive created at 2026-11-08 03:00:01 IST",
        "215; 1259 16390 TABLE public users moa",
        "216; 1259 16391 TABLE public clubs moa",
        "3401; 0 16390 TABLE DATA public users moa",
        "217; 1259 16392 SEQUENCE public users_id_seq moa",
    ]
)


def make_dump(cfg, name="backup-20261107-213000.dump", age_s=5.0):
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.backup_dir / name
    path.write_bytes(PLAIN)
    began = calendar.timegm(time.strptime(name[7:22], "%Y%m%d-%H%M%S"))
    os.utime(path, (began + 12, began + 12))  # a 12-second dump
    return path


def fake_age(events, fail=False):
    def run(argv):
        events.append("encrypt")
        if fail:
            raise CommandError("age exited 1: age: error: malformed recipient")
        out = argv[argv.index("-o") + 1]
        source = argv[-1]
        data = open(source, "rb").read()
        with open(out, "wb") as handle:
            handle.write(AGE_HEADER + b"-> X25519 stanza\n" + bytes(b ^ 0x5A for b in data))
        return ""

    return run


def runner_for(events, **kw):
    return FakeRunner({"age": fake_age(events, **kw), "/srv/moa/dc exec": TOC})


def now_for(dump):
    return dump.stat().st_mtime + 30


def test_encrypts_then_sends_and_never_sends_plaintext(cfg):
    dump = make_dump(cfg)
    telegram = FakeTelegram()
    runner = runner_for(telegram.events)
    assert backup.deliver(cfg, telegram, runner, now=now_for(dump)) == 0

    assert telegram.events == ["encrypt", "sendDocument"]
    [doc] = telegram.documents
    assert doc["data"].startswith(AGE_HEADER)
    assert PLAIN not in doc["data"] and b"secret-row-data" not in doc["data"]
    assert doc["filename"] == "backup-20261107-213000.dump.age"
    assert doc["path"].parent == cfg.work_dir / "outbox"
    caption = doc["fields"]["caption"]
    assert "Database backup" in caption and "moa-prod" in caption and "2 tables" in caption and "took 12s" in caption
    assert "MB encrypted" in caption and "age -d -i backup-age.key" in caption
    # age got the owner's recipient; the encrypted copy is gone, the dump stays.
    [age_call] = [c for c in runner.calls if c[0] == "age"]
    assert age_call[:4] == ["age", "--encrypt", "-r", cfg.age_recipient]
    assert not list((cfg.work_dir / "outbox").iterdir())
    assert dump.read_bytes() == PLAIN


def test_send_document_refuses_a_plaintext_file(cfg, tmp_path):
    plain = tmp_path / "backup.dump"
    plain.write_bytes(PLAIN)
    telegram = FakeTelegram()
    with pytest.raises(NotEncrypted):
        telegram.send_document(plain, "x")
    renamed = tmp_path / "backup.dump.age"  # a name is not encryption
    shutil.copy(plain, renamed)
    with pytest.raises(NotEncrypted):
        telegram.send_document(renamed, "x")
    assert telegram.documents == []


def test_an_encryption_failure_sends_nothing_and_fails_the_unit(cfg, capsys):
    dump = make_dump(cfg)
    telegram = FakeTelegram()
    assert backup.deliver(cfg, telegram, runner_for(telegram.events, fail=True), now=now_for(dump)) == 1
    assert telegram.documents == [] and telegram.requests == []
    assert "deliver-backup FAILED: encryption failed" in capsys.readouterr().out


def test_age_writing_something_else_is_caught(cfg, capsys):
    dump = make_dump(cfg)

    def copies_plaintext(argv):
        shutil.copy(argv[-1], argv[argv.index("-o") + 1])
        return ""

    telegram = FakeTelegram()
    assert backup.deliver(cfg, telegram, FakeRunner({"age": copies_plaintext}), now=now_for(dump)) == 1
    assert telegram.documents == []
    assert "no age file" in capsys.readouterr().out
    assert not list((cfg.work_dir / "outbox").iterdir())


def test_over_50_mb_says_so_instead_of_failing(cfg):
    dump = make_dump(cfg)
    telegram = FakeTelegram()
    assert backup.deliver(cfg, telegram, runner_for(telegram.events), now=now_for(dump), limit=1000) == 0
    assert telegram.documents == []
    [text] = telegram.texts
    assert "not sent" in text and "50 MB" in text and dump.name in text
    assert not list((cfg.work_dir / "outbox").iterdir())


def test_the_real_limit_is_under_50_mb():
    from opsbot.telegram import UPLOAD_LIMIT

    assert 45_000_000 < UPLOAD_LIMIT < 50 * 1000 * 1000


def test_send_document_refuses_over_the_limit_without_uploading(cfg, tmp_path, monkeypatch):
    from opsbot import telegram as tg

    monkeypatch.setattr(tg, "UPLOAD_LIMIT", 10)
    big = tmp_path / "x.age"
    big.write_bytes(AGE_HEADER + b"y" * 100)
    client = FakeTelegram()
    with pytest.raises(tg.TelegramError, match="50 MB"):
        client.send_document(big, "x")
    assert client.documents == []


def test_a_telegram_failure_fails_the_unit(cfg, capsys):
    dump = make_dump(cfg)
    telegram = FakeTelegram(answers=[{"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}])
    assert backup.deliver(cfg, telegram, runner_for(telegram.events), now=now_for(dump)) == 1
    assert "Telegram did not take the file" in capsys.readouterr().out
    assert not list((cfg.work_dir / "outbox").iterdir())


def test_a_stale_dump_is_not_sent(cfg, capsys):
    dump = make_dump(cfg)
    telegram = FakeTelegram()
    assert backup.deliver(cfg, telegram, runner_for(telegram.events), now=dump.stat().st_mtime + 5 * 3600) == 1
    assert telegram.events == [] and "this run made none" in capsys.readouterr().out


def test_no_dump_or_not_a_dump(cfg, capsys):
    telegram = FakeTelegram()
    assert backup.deliver(cfg, telegram, runner_for(telegram.events)) == 1
    cfg.backup_dir.mkdir(parents=True, exist_ok=True)
    junk = cfg.backup_dir / "backup-20261107-213000.dump"
    junk.write_bytes(b"not a dump")
    assert backup.deliver(cfg, telegram, runner_for(telegram.events), now=junk.stat().st_mtime) == 1
    out = capsys.readouterr().out
    assert "cannot read" in out and "not a pg_dump archive" in out
    assert telegram.events == []


def test_the_newest_dump_is_the_one(cfg):
    old = make_dump(cfg, "backup-20261106-213000.dump")
    new = make_dump(cfg, "backup-20261107-213000.dump")
    (cfg.backup_dir / ".backup-20261108-000000.dump.partial").write_bytes(PLAIN)
    (cfg.backup_dir / "before-restore.dump").write_bytes(PLAIN)
    assert backup.newest_dump(cfg.backup_dir, now=new.stat().st_mtime) == new
    assert old.exists()


def test_tables_not_counted_is_not_a_failure(cfg):
    dump = make_dump(cfg)
    telegram = FakeTelegram()
    runner = FakeRunner({"age": fake_age(telegram.events)})  # no dc: pg_restore -l cannot run
    assert backup.deliver(cfg, telegram, runner, now=now_for(dump)) == 0
    assert "tables: not counted" in telegram.documents[0]["fields"]["caption"]


def test_deliver_backup_skips_quietly_when_not_set_up(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPS_BOT_CONFIG", str(tmp_path / "missing.env"))
    assert main.main(["deliver-backup"]) == 0
    assert "not set up" in capsys.readouterr().out
    bad = tmp_path / "bad.env"
    bad.write_text("TELEGRAM_BOT_TOKEN=x\n")
    os.chmod(bad, 0o644)
    monkeypatch.setenv("OPS_BOT_CONFIG", str(bad))
    assert main.main(["deliver-backup"]) == 1  # set up, but wrong: the unit fails and alerts


@pytest.mark.skipif(not shutil.which("age") or not shutil.which("age-keygen"), reason="age is not installed")
def test_with_real_age_the_owner_can_decrypt_it(cfg, tmp_path):
    key = tmp_path / "backup-age.key"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = next(line.split(": ")[1] for line in key.read_text().splitlines() if line.startswith("# public key:"))
    cfg = replace(cfg, age_recipient=recipient)
    dump = make_dump(cfg)
    telegram = FakeTelegram()
    assert backup.deliver(cfg, telegram, Runner(), now=now_for(dump)) == 0
    [doc] = telegram.documents
    assert doc["data"].startswith(AGE_HEADER) and b"secret-row-data" not in doc["data"]
    sent = tmp_path / doc["filename"]
    sent.write_bytes(doc["data"])
    out = tmp_path / "backup.dump"
    subprocess.run(["age", "-d", "-i", str(key), "-o", str(out), str(sent)], check=True)
    assert out.read_bytes() == PLAIN
