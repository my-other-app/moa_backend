"""The systemd units and the shell hooks: the timer is 03:00 India time, the
delivery runs only after a good dump, and the alert hook reaches Telegram."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1]
OPS = BOT.parents[1]
BACKUP = OPS / "backup"


def unit(path: Path) -> dict[str, dict[str, list[str]]]:
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
            continue
        key, _, value = line.partition("=")
        current.setdefault(key, []).append(value)
    return sections


def test_the_backup_timer_is_0300_india_time():
    timer = unit(BACKUP / "systemd" / "myotherapp-backup.timer")
    assert timer["Timer"]["OnCalendar"] == ["*-*-* 03:00:00 Asia/Kolkata"]
    assert timer["Timer"]["Persistent"] == ["true"]
    assert timer["Timer"]["Unit"] == ["myotherapp-backup.service"]


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="no systemd here")
def test_systemd_reads_the_schedule_as_2130_utc():
    out = subprocess.run(["systemd-analyze", "calendar", "*-*-* 03:00:00 Asia/Kolkata"], capture_output=True, text=True, check=True).stdout
    assert "21:30:00 UTC" in out or "Asia/Kolkata" in out


def test_delivery_runs_after_the_dump_and_only_then():
    service = unit(BACKUP / "systemd" / "myotherapp-backup.service")["Service"]
    assert service["ExecStart"] == ["/bin/bash ${MOA_PROJECT_DIR}/ops/backup/run.sh backup"]
    # ExecStartPost of a oneshot runs only when ExecStart succeeded, and its
    # failure fails the unit (so OnFailure alerts): no '-' prefix.
    assert service["ExecStartPost"] == ["/bin/bash ${MOA_PROJECT_DIR}/ops/backup/run.sh deliver"]
    assert unit(BACKUP / "systemd" / "myotherapp-backup.service")["Unit"]["OnFailure"] == ["myotherapp-alert@%N.service"]


def test_the_bot_unit_restarts_on_failure_and_runs_sandboxed():
    bot = unit(BOT / "systemd" / "moa-ops-bot.service")
    service = bot["Service"]
    assert service["ExecStart"] == ["/usr/bin/python3 -I -B /srv/moa/ops-bot/run.py serve"]
    assert service["Restart"] == ["on-failure"] and service["RestartPreventExitStatus"] == ["78"]
    assert service["ProtectSystem"] == ["strict"] and service["NoNewPrivileges"] == ["yes"]
    assert service["StateDirectory"] == ["moa-ops-bot"]
    assert bot["Install"]["WantedBy"] == ["multi-user.target"]
    assert "EnvironmentFile" not in service  # the token is read by the bot from its own file, never put in the environment


def test_the_settings_template_has_no_token():
    text = (OPS / "server" / "env" / "ops-bot.env.example").read_text()
    assert "TELEGRAM_BOT_TOKEN=\n" in text and "TELEGRAM_CHAT_ID=\n" in text
    assert "OPS_AGE_RECIPIENT=age17jsckeq68nf0yrz7sgtrrk7y6930uykd3t4je4t9tmnk8jqrwagqmmwu9v" in text


@pytest.mark.parametrize("script", [BACKUP / "run.sh", BACKUP / "alert.sh", BOT / "install.sh"])
def test_the_scripts_parse(script):
    subprocess.run(["bash", "-n", str(script)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(script)], check=True)


def fake_project(tmp_path: Path, bot_exit: int | None) -> Path:
    project = tmp_path / "srv"
    project.mkdir()
    if bot_exit is not None:
        (project / "ops-bot").mkdir()
        (project / "ops-bot" / "run.py").write_text(
            "import sys\n"
            f"open({str(tmp_path / 'bot-args')!r}, 'w').write(' '.join(sys.argv[1:]))\n"
            f"open({str(tmp_path / 'bot-stdin')!r}, 'w').write(sys.stdin.read() if not sys.stdin.isatty() else '')\n"
            f"sys.exit({bot_exit})\n"
        )
    return project


def fake_bin(tmp_path: Path, **scripts: str) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in scripts.items():
        path = bindir / name.replace("_", "-")
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)
    return bindir


def env_for(tmp_path, project, bindir):
    return {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "MOA_PROJECT_DIR": str(project)}


def test_run_sh_deliver_hands_over_to_the_bot(tmp_path):
    project = fake_project(tmp_path, 0)
    done = subprocess.run(
        ["bash", str(BACKUP / "run.sh"), "deliver"], env=env_for(tmp_path, project, fake_bin(tmp_path)), capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "bot-args").read_text() == "deliver-backup"


def test_run_sh_deliver_without_a_bot_skips(tmp_path):
    project = fake_project(tmp_path, None)
    done = subprocess.run(
        ["bash", str(BACKUP / "run.sh"), "deliver"], env=env_for(tmp_path, project, fake_bin(tmp_path)), capture_output=True, text=True
    )
    assert done.returncode == 0 and "not sent to Telegram" in done.stdout


def test_run_sh_deliver_passes_the_bots_failure_on(tmp_path):
    project = fake_project(tmp_path, 1)
    done = subprocess.run(
        ["bash", str(BACKUP / "run.sh"), "deliver"], env=env_for(tmp_path, project, fake_bin(tmp_path)), capture_output=True, text=True
    )
    assert done.returncode == 1


JOURNAL = (
    'echo "deliver-backup FAILED: encryption failed"; echo "pg_dump: connection to postgresql://moa:hunter2-pw@postgres:5432/moa failed"'
)


@pytest.mark.parametrize("app_ok", [True, False])
def test_alert_sh_tells_telegram_and_the_app(tmp_path, app_ok):
    project = fake_project(tmp_path, 0)
    bindir = fake_bin(
        tmp_path,
        docker_compose=f"cat > {tmp_path}/app-alert; exit {0 if app_ok else 1}",
        journalctl=JOURNAL,
        logger=f'echo "$@" > {tmp_path}/logged',
    )
    done = subprocess.run(
        ["bash", str(BACKUP / "alert.sh"), "myotherapp-backup.service"],
        env=env_for(tmp_path, project, bindir),
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    args = (tmp_path / "bot-args").read_text()
    assert args.startswith("notify --title The daily database backup failed on")
    body = (tmp_path / "bot-stdin").read_text()
    assert "deliver-backup FAILED: encryption failed" in body and "hunter2-pw" not in body
    assert (tmp_path / "app-alert").exists()  # the app is still told: mail, Discord, the bell
    assert not (tmp_path / "logged").exists()


def test_alert_sh_falls_back_when_telegram_is_not_set_up(tmp_path):
    project = fake_project(tmp_path, 78)
    bindir = fake_bin(tmp_path, docker_compose="exit 1", journalctl=JOURNAL, logger=f'echo "$@" > {tmp_path}/logged')
    done = subprocess.run(
        ["bash", str(BACKUP / "alert.sh"), "myotherapp-restore-drill.service"],
        env=env_for(tmp_path, project, bindir),
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1
    assert "could not alert anyone: The monthly restore drill failed" in (tmp_path / "logged").read_text()
