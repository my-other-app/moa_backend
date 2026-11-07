"""Command parsing, limits and answers."""

import pytest
from conftest import container

from opsbot import commands
from opsbot.commands import Commands, parse, parse_log_args
from opsbot.system import CommandError


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/status", ("status", ())),
        ("  /STATUS  ", ("status", ())),
        ("/logs@MoaOpsBot backend 20", ("logs", ("backend", "20"))),
        ("/logs   worker", ("logs", ("worker",))),
        ("status", None),
        ("", None),
        (None, None),
        ("/", None),
        ("/rm;-rf", None),
    ],
)
def test_parse(text, expected):
    parsed = parse(text)
    assert (parsed.name, parsed.args) == expected if expected else parsed is None


def test_overlong_input_is_cut():
    assert len(" ".join(parse("/logs " + "a" * 5000).args)) < commands.MAX_COMMAND_CHARS


@pytest.mark.parametrize(
    "args, expected",
    [
        (("backend",), ("backend", 30, None)),
        (("Backend", "10"), ("backend", 10, None)),
        (("backend", "50"), ("backend", 50, None)),
        (("backend", "5000"), ("backend", 50, "(at most 50 lines)")),
        (("backend", "0"), ("backend", 1, None)),
        (("backend", "-3"), ("backend", 1, None)),
    ],
)
def test_log_limits(args, expected):
    assert parse_log_args(args) == expected


@pytest.mark.parametrize("args", [(), ("../../etc/passwd",), ("backend", "lots"), ("backend", "10", "extra"), ("$(id)",), ("-f",)])
def test_bad_log_arguments_are_refused(args):
    with pytest.raises(ValueError):
        parse_log_args(args)


def test_logs_reads_the_service_container_and_redacts(cfg, host):
    host.log_text["moa-backend-1"] = "\n".join(f"line {i} token=secret{i}" for i in range(80))
    answer = Commands(cfg, host).handle("/logs backend 5")
    assert host.log_calls == [("moa-backend-1", 5, None, None)]
    assert "line 79" in answer and "secret" not in answer and "line 74" not in answer


def test_logs_caps_lines_and_says_so(cfg, host):
    host.log_text["moa-backend-1"] = "\n".join(f"l{i}" for i in range(200))
    answer = Commands(cfg, host).handle("/logs backend 999")
    assert host.log_calls[0][1] == 50 and "at most 50" in answer


def test_logs_of_an_unknown_service_lists_the_known_ones(cfg, host):
    answer = Commands(cfg, host).handle("/logs nginx")
    assert "No service nginx" in answer and "backend" in answer and host.log_calls == []


def test_status_lists_every_container_and_the_host(cfg, host):
    host.box = [
        container("backend"),
        container("worker", health="unhealthy", restarts=3),
        container("admin", status="exited", exit_code=137),
    ]
    answer = Commands(cfg, host).handle("/status")
    assert "<pre>" in answer and "backend" in answer and "UNHLTH" in answer and "EXIT137" in answer
    assert "load 0.50" in answer and "mem 6.0G/12G" in answer and "/ 40%" in answer
    assert "512M/2G" in answer and "Needs a look: admin, worker" in answer


def test_disk(cfg, host, tmp_path):
    cfg.backup_dir.mkdir()
    (cfg.backup_dir / "backup-20261107-213000.dump").write_bytes(b"PGDMP" + b"x" * 2048)
    answer = Commands(cfg, host).handle("/disk")
    assert "images" in answer and "4.1GB" in answer and "1 dump(s)" in answer and "backup-20261107-213000.dump" in answer


def test_backup_starts_the_unit_once(cfg, host):
    clock = [1000.0]
    cmds = Commands(cfg, host, clock=lambda: clock[0])
    assert "Backup started" in cmds.handle("/backup")
    assert host.started_units == ["myotherapp-backup.service"]
    clock[0] += 30
    assert "try again" in cmds.handle("/backup")  # a double tap
    host.units["myotherapp-backup.service"] = "activating"
    clock[0] += 3600
    assert "already running" in cmds.handle("/backup")
    assert host.started_units == ["myotherapp-backup.service"]


def test_unknown_and_plain_text(cfg, host):
    cmds = Commands(cfg, host)
    assert "Unknown command /reboot" in cmds.handle("/reboot")
    assert "/help" in cmds.handle("hello")
    assert cmds.handle("/start") == commands.HELP


def test_a_failing_command_answers_redacted(cfg, host):
    host.docker_error = CommandError("docker ps exited 1: cannot connect, password=hunter2")
    answer = Commands(cfg, host).handle("/logs backend")
    assert "Could not do that" in answer and "hunter2" not in answer
