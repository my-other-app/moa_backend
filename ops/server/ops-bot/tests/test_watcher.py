"""Alerts: one when something goes wrong, one when it recovers, never a
stream; bursts at most once per 10 minutes with a count."""

from conftest import container

from opsbot.alerts import Bursts, Conditions
from opsbot.state import State
from opsbot.system import CommandError
from opsbot.watcher import Watcher, disk_level, scan_errors


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def make(cfg, host, tmp_path):
    clock = Clock()
    return Watcher(cfg, host, State(tmp_path / "state.json"), clock=clock), clock


def ticks(watcher, clock, n, step=60):
    out = []
    for _ in range(n):
        clock.now += step
        out.append(watcher.tick())
    return out


def test_conditions_alert_once_and_recover_once():
    c = Conditions()
    assert c.update("k", False, "bad", "ok") is None
    assert c.update("k", True, "bad", "ok") == "bad"
    assert c.update("k", True, "bad", "ok") is None
    assert c.update("k", None, "bad", "ok") is None  # cannot tell: no change
    assert c.update("k", False, "bad", "ok") == "ok"
    assert c.update("k", False, "bad", "ok") is None


def test_bursts_are_rate_limited_with_counts():
    b = Bursts(every=600, threshold=3)
    assert b.feed("backend", 2, "x", 0) is None  # under the threshold: not a burst
    assert b.feed("backend", 5, "first", 60) == (5, "first")
    assert b.feed("backend", 4, "second", 120) is None  # within 10 minutes: counted, not sent
    assert b.feed("backend", 1, "third", 300) is None
    assert b.quiet(600) == []  # errors 5 minutes ago: not quiet yet
    assert b.feed("backend", 2, "fourth", 661) == (7, "second")  # the next alert carries the count
    assert b.quiet(1200) == []
    assert b.quiet(1261) == [("backend", 12, 60)]
    assert b.quiet(5000) == []  # once


def test_an_unhealthy_container_alerts_once_then_recovers_once(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    assert ticks(w, clock, 2) == [None, None]
    host.box[0] = container("backend", health="unhealthy")
    out = ticks(w, clock, 5)
    assert "[ALERT] backend is unhealthy" in out[0] and out[1:] == [None] * 4
    host.box[0] = container("backend", health="starting")
    assert ticks(w, clock, 1) == [None]  # starting: neither
    host.box[0] = container("backend", health="healthy")
    out = ticks(w, clock, 3)
    assert "[OK] backend is running and healthy" in out[0] and out[1:] == [None, None]


def test_an_exited_container_and_an_oom_kill(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    host.box[1] = container("postgres", status="exited", exit_code=137, oom=True)
    [out] = ticks(w, clock, 1)
    assert "postgres exited with status 137 (killed: out of memory)" in out


def test_restarts_alert_at_most_once_per_ten_minutes(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    ticks(w, clock, 1)
    sent = []
    for n in range(1, 8):  # a crash loop: one more restart every minute
        host.box[0] = container("backend", restarts=n)
        sent.append(ticks(w, clock, 1)[0])
    alerts = [s for s in sent if s]
    assert len(alerts) == 1 and "restarted 1 time(s)" in alerts[0]
    for n in range(8, 13):
        host.box[0] = container("backend", restarts=n)
        sent.append(ticks(w, clock, 1)[0])
    later = [s for s in sent[7:] if s]
    assert len(later) == 1 and "restarted" in later[0] and "12 restarts in all" not in later[0]
    out = [s for s in ticks(w, clock, 12) if s]
    assert len(out) == 1 and "no restarts for 10 minutes" in out[0]


def test_a_recreated_container_is_not_a_restart(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    host.box[0] = container("backend", restarts=4, cid="old")
    ticks(w, clock, 1)
    host.box[0] = container("backend", restarts=0, cid="new")  # a deploy
    host.box[0] = container("backend", restarts=2, cid="new2")
    assert ticks(w, clock, 2) == [None, None]


def test_a_container_that_disappears(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    ticks(w, clock, 1)
    gone = host.box.pop(1)
    out = ticks(w, clock, 5)
    assert [bool(o) for o in out] == [False, False, True, False, False]
    assert "moa-postgres-1 is gone" in out[2]
    host.box.append(gone)
    out = ticks(w, clock, 2)
    assert "[OK] postgres is running" in out[0] and out[1] is None


def test_docker_not_answering(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    host.docker_error = CommandError("docker ps timed out after 20s")
    out = ticks(w, clock, 3)
    assert "Docker is not answering" in out[0] and out[1:] == [None, None]
    host.docker_error = None
    assert "Docker answers again" in ticks(w, clock, 1)[0]


def test_disk_80_then_90_then_back(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    seen = []
    for pct in (50, 81, 85, 91, 95, 89, 81, 79, 77, 70):
        host.disk_rows = [("/", pct, 100)]
        seen.append(ticks(w, clock, 1)[0])
    alerts = [(pct, s) for pct, s in zip((50, 81, 85, 91, 95, 89, 81, 79, 77, 70), seen, strict=False) if s]
    assert [p for p, _ in alerts] == [81, 91, 77]
    assert "81% full" in alerts[0][1] and "91% full" in alerts[1][1] and "[OK] disk / is back to 77%" in alerts[2][1]


def test_disk_level_hysteresis():
    assert disk_level(79.9, 0) == 0 and disk_level(80, 0) == 1 and disk_level(90, 1) == 2
    assert disk_level(88.5, 2) == 2 and disk_level(87.9, 2) == 1 and disk_level(78.5, 1) == 1 and disk_level(77.9, 1) == 0


def test_memory_pressure_once_and_back(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    total = host.mem["MemTotal"]
    host.mem["MemAvailable"] = int(total * 0.05)
    out = ticks(w, clock, 3)
    assert "[ALERT] memory pressure: 5% available" in out[0] and out[1:] == [None, None]
    host.mem["MemAvailable"] = int(total * 0.12)  # better, but within the margin
    assert ticks(w, clock, 1) == [None]
    host.mem["MemAvailable"] = int(total * 0.40)
    assert "[OK] memory: 40% available" in ticks(w, clock, 1)[0]
    host.psi = 35.0
    assert "stalled 35%" in ticks(w, clock, 1)[0]


def test_oom_kills_from_the_kernel(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    host.journals["kernel"] = [
        {"MESSAGE": "python invoked oom-killer: gfp_mask=0x140cca", "__CURSOR": "k1"},
        {"MESSAGE": "Memory cgroup out of memory: Killed process 4242 (python) total-vm:1kB", "__CURSOR": "k2"},
    ]
    [out] = ticks(w, clock, 1)
    assert "the kernel killed python (1 kill(s))" in out
    assert w.state.get("cursor_kernel") == "k2"
    host.journals["kernel"] = [{"MESSAGE": "Out of memory: Killed process 1 (node)", "__CURSOR": "k3"}]
    assert ticks(w, clock, 1) == [None]  # within 10 minutes
    assert host.journal_calls[-1] == ("kernel", "k2", None)  # read on from the cursor


def test_ssh_logins_say_who_and_from_where(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    host.journals["ssh"] = [
        {"MESSAGE": "Accepted publickey for ubuntu from 203.0.113.7 port 50122 ssh2: ED25519 SHA256:abc", "__CURSOR": "s1"},
        {"MESSAGE": "Failed password for root from 198.51.100.1 port 22 ssh2", "__CURSOR": "s2"},
        {"MESSAGE": "Accepted publickey for moa-deploy from 2001:db8::1 port 4000 ssh2", "__CURSOR": "s3"},
    ]
    [out] = ticks(w, clock, 1)
    assert "[SSH] login accepted: ubuntu from 203.0.113.7 (publickey); moa-deploy from 2001:db8::1 (publickey)" in out
    assert "198.51.100.1" not in out
    assert host.journal_calls[0][0:2] == ("ssh", None) and host.journal_calls[0][2].startswith("@")
    assert w.state.get("cursor_ssh") == "s3"


def test_error_bursts_one_alert_per_service_per_ten_minutes(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    burst = "\n".join(
        [
            'INFO: 1.2.3.4:5 - "GET /api/v1/tickets/TKN/pdf?token=abc HTTP/1.1" 500 Internal Server Error',
            "ERROR app.x: payment failed for someone@example.com",
            "Traceback (most recent call last):",
            '  File "x.py", line 1, in <module>',
            "ValueError: bad",
            "INFO fine",
        ]
    )
    sent = []
    for _ in range(9):  # errors every minute for nine minutes
        host.log_text["moa-backend-1"] = burst
        sent.append(ticks(w, clock, 1)[0])
    alerts = [s for s in sent if s]
    assert len(alerts) == 1
    assert "[ERRORS] backend: 3 error line(s)" in alerts[0]
    assert "TKN" not in alerts[0] and "abc" not in alerts[0] and "<pre>" not in alerts[0]
    host.log_text["moa-backend-1"] = burst
    out = ticks(w, clock, 2)
    # Ten minutes after the first alert: the next, with all held back since.
    assert out[0] is None and "[ERRORS] backend: 30 error line(s)" in out[1]
    host.log_text["moa-backend-1"] = ""
    out = [s for s in ticks(w, clock, 12) if s]
    assert len(out) == 1 and "[OK] backend: no errors for 10 minutes (33 since" in out[0]


def test_errors_are_read_in_contiguous_windows_and_ignored_services_skipped(cfg, host, tmp_path):
    from dataclasses import replace

    cfg = replace(cfg, error_ignore_services=frozenset({"postgres"}))
    w, clock = make(cfg, host, tmp_path)
    ticks(w, clock, 3)
    windows = [(since, until) for name, _, since, until in host.log_calls if name == "moa-backend-1"]
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:], strict=False))
    assert not [c for c in host.log_calls if c[0] == "moa-postgres-1"]


def test_a_few_errors_under_the_threshold_are_not_a_burst(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    host.log_text["moa-backend-1"] = "ERROR one\nERROR two"
    assert ticks(w, clock, 3) == [None, None, None]


def test_scan_errors_samples_the_exception_line():
    count, sample = scan_errors("Traceback (most recent call last):\n  File \"a\"\n    x()\nKeyError: 'club_id'\nINFO ok")
    assert count == 1 and sample == "Traceback … KeyError: 'club_id'"
    assert scan_errors('{"level":"error","msg":"x"}\n{"status":502}')[0] == 2
    assert scan_errors("INFO all good\nWARNING slow")[0] == 0


def test_one_broken_check_does_not_stop_the_others(cfg, host, tmp_path, monkeypatch):
    w, clock = make(cfg, host, tmp_path)

    def broken(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(host, "meminfo", broken)
    host.disk_rows = [("/", 95, 100)]
    [out] = ticks(w, clock, 1)
    assert "95% full" in out


def test_conditions_survive_a_restart_of_the_bot(cfg, host, tmp_path):
    w, clock = make(cfg, host, tmp_path)
    host.box[0] = container("backend", health="unhealthy")
    assert ticks(w, clock, 1)[0]
    again, clock2 = make(cfg, host, tmp_path)  # same state file
    assert ticks(again, clock2, 1) == [None]


def test_a_cursor_the_journal_forgot_starts_over(cfg, host, tmp_path, monkeypatch):
    w, clock = make(cfg, host, tmp_path)
    w.state.set("cursor_ssh", "s=gone")
    real = host.journal

    def journal(matches, *, cursor, since=None, limit=300):
        if cursor == "s=gone":
            raise CommandError("journalctl exited 1: Failed to seek to cursor: Invalid argument")
        return real(matches, cursor=cursor, since=since, limit=limit)

    monkeypatch.setattr(host, "journal", journal)
    host.journals["ssh"] = [{"MESSAGE": "Accepted publickey for ubuntu from 203.0.113.7 port 1 ssh2", "__CURSOR": "s4"}]
    [out] = ticks(w, clock, 1)
    assert "ubuntu from 203.0.113.7" in out and w.state.get("cursor_ssh") == "s4"


def test_journal_and_docker_commands_are_argument_lists():
    from conftest import FakeRunner

    from opsbot.system import Host

    runner = FakeRunner(
        {"docker ps": "abc\n", "docker inspect": "[]", "journalctl": '{"MESSAGE": "x", "__CURSOR": "c"}\n-- No entries --\n'}
    )
    host = Host(runner, project="moa")
    assert host.containers() == []
    assert runner.calls[0] == ["docker", "ps", "-aq", "--no-trunc"] + [
        "--filter",
        "label=com.docker.compose.oneoff=False",
        "--filter",
        "label=com.docker.compose.project=moa",
    ]
    assert host.journal(["-k"], cursor="c0") == [{"MESSAGE": "x", "__CURSOR": "c"}]
    assert runner.calls[-1][-2:] == ["--after-cursor", "c0"]
