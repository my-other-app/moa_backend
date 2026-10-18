"""The production deploy workflow (.github/workflows/live-deploy.yml).

Its Deploy step renders a shell script on the runner and runs it on the
server. Here it is rendered the way the runner does (the step's own text, run
by bash with the inputs as environment), checked with `bash -n`, and then run
against fake `sudo`, `docker` and `docker-compose` that only write down what
they were asked -- so the order of build, migrate, recreate and the extra
services is pinned without a server. The smoke check runs against a fake
`curl`."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "live-deploy.yml"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FLAGS = ("SYNC_CODE", "RESTART_BACKEND", "INSTALL_REQUIREMENTS", "RESTART_DATABASE", "RESTART_ALL",
         "MIGRATE", "UP_BACKEND", "BUILD_BACKEND")


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _step(name: str) -> dict:
    return next(s for s in _workflow()["jobs"]["deploy"]["steps"] if s.get("name") == name)


def _render_part() -> str:
    """The Deploy step up to the point where it ships the script."""
    run = _step("Deploy")["run"]
    return run[: run.index('REMOTE_SCRIPT="/tmp/moa-deploy-')]


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture
def fakes(tmp_path):
    """bin/ with sudo, docker and docker-compose that log their arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _executable(bin_dir / "sudo", 'exec "$@"\n')
    _executable(bin_dir / "docker", f'echo "docker $*" >> "{log}"\nexit "${{FAKE_DOCKER_STATUS:-0}}"\n')
    _executable(bin_dir / "docker-compose", f'''
args="$*"
args="${{args#-f docker-compose.prod.yml }}"
echo "compose $args [buildkit=${{DOCKER_BUILDKIT:-}}]" >> "{log}"
case "$args" in
  "config --services") printf 'postgres\\nbackend\\nredis\\nworker\\nmeilisearch\\n' ;;
  *"alembic current"*) echo "topics (head)" ;;
esac
exit 0
''')
    return bin_dir, log


def _render(tmp_path, **inputs) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ["PATH"], "PROJECT_PATH": str(tmp_path / "project"), "SERVICES": ""}
    env.update({flag: "false" for flag in FLAGS})
    env.update({k.upper(): ("true" if v is True else "false" if v is False else v) for k, v in inputs.items()})
    work = tmp_path / "runner"
    work.mkdir(exist_ok=True)
    return subprocess.run(["bash", "-c", _render_part()], cwd=work, env=env, capture_output=True, text=True)


def _deploy(tmp_path, fakes, **inputs) -> tuple[subprocess.CompletedProcess, list[str]]:
    bin_dir, log = fakes
    rendered = _render(tmp_path, **inputs)
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    (tmp_path / "project").mkdir(exist_ok=True)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **{k: v for k, v in os.environ.items() if k == "HOME"}}
    if "FAKE_DOCKER_STATUS" in inputs:
        env["FAKE_DOCKER_STATUS"] = inputs["FAKE_DOCKER_STATUS"]
    run = subprocess.run(["bash", str(tmp_path / "runner" / "deploy-remote.sh")], env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return run, calls


# ---------------------------------------------------------------------------
# The inputs
# ---------------------------------------------------------------------------


def test_every_input_is_kept_and_the_new_ones_are_there():
    inputs = _workflow()[True]["workflow_dispatch"]["inputs"]  # YAML 1.1 reads `on` as true
    for name in ("sync_code", "restart_backend", "sync_env", "install_requirements", "restart_database",
                 "restart_all", "migrate", "up_backend", "build_backend"):
        assert inputs[name]["type"] == "boolean", name
    assert inputs["services"]["type"] == "string" and inputs["services"]["default"] == ""
    assert inputs["smoke_timeout"]["type"] == "number" and inputs["smoke_timeout"]["default"] == 180


def test_the_backend_is_never_started_with_run():
    """A one-off `run` container of the backend cannot start while the
    running one holds its fixed ipv4_address (the 2026-10-18 13:45 deploy)."""
    text = WORKFLOW.read_text()
    assert "run --rm" not in text and " run -" not in _step("Deploy")["run"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"sync_code": True, "restart_backend": True},
        {"sync_code": True, "install_requirements": True, "migrate": True},
        {"restart_all": True, "migrate": True},
        {"build_backend": True, "up_backend": True, "restart_database": True},
        {"migrate": True, "services": "redis worker meilisearch"},
    ],
)
def test_the_rendered_script_is_valid_bash(tmp_path, inputs):
    rendered = _render(tmp_path, **inputs)
    assert rendered.returncode == 0, rendered.stderr
    script = tmp_path / "runner" / "deploy-remote.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    assert script.read_text().rstrip().endswith('echo "MOA_DEPLOY_COMPLETE"')
    if shutil.which("shellcheck"):
        check = subprocess.run(["shellcheck", "-s", "bash", "-S", "error", str(script)], capture_output=True, text=True)
        assert check.returncode == 0, check.stdout


def test_the_step_checks_its_own_rendering():
    assert "bash -n deploy-remote.sh" in _render_part()


@pytest.mark.parametrize("services", ["redis; rm -rf /", "worker $(id)", "Redis", "../x", "backend", "postgres", "*"])
def test_services_that_are_not_plain_names_are_refused_on_the_runner(tmp_path, services):
    rendered = _render(tmp_path, services=services)
    assert rendered.returncode == 1
    assert "::error::services" in rendered.stdout
    assert not (tmp_path / "runner" / "deploy-remote.sh").exists()


# ---------------------------------------------------------------------------
# What the server would be asked to do
# ---------------------------------------------------------------------------


def test_install_requirements_builds_migrates_then_recreates(tmp_path, fakes):
    run, calls = _deploy(tmp_path, fakes, sync_code=True, install_requirements=True, migrate=True)
    assert run.returncode == 0, run.stdout + run.stderr
    order = [c for c in calls if not c.startswith("docker buildx")]
    assert order == [
        "compose build backend [buildkit=1]",
        "compose exec -T backend alembic upgrade head [buildkit=]",
        "compose up -d --no-deps --force-recreate backend [buildkit=]",
        "compose exec -T backend alembic current [buildkit=]",
    ]
    assert calls[0] == "docker buildx version"  # BuildKit checked before anything
    assert run.stdout.rstrip().endswith("MOA_DEPLOY_COMPLETE")


def test_a_missing_buildx_stops_before_anything_changes(tmp_path, fakes):
    run, calls = _deploy(tmp_path, fakes, install_requirements=True, migrate=True, FAKE_DOCKER_STATUS="1")
    assert run.returncode == 1
    assert "docker buildx (BuildKit) is not installed" in run.stdout
    assert calls == ["docker buildx version"]


def test_sync_and_restart_restarts_once(tmp_path, fakes):
    run, calls = _deploy(tmp_path, fakes, sync_code=True, restart_backend=True)
    assert run.returncode == 0
    assert calls == ["compose restart backend [buildkit=]"]


def test_services_start_after_the_migration_one_at_a_time(tmp_path, fakes):
    run, calls = _deploy(tmp_path, fakes, sync_code=True, migrate=True, services="redis worker")
    assert run.returncode == 0, run.stdout + run.stderr
    assert calls == [
        "compose exec -T backend alembic upgrade head [buildkit=]",
        "compose exec -T backend alembic current [buildkit=]",
        "compose config --services [buildkit=]",
        "compose up -d --no-deps redis [buildkit=1]",
        "compose up -d --no-deps worker [buildkit=1]",
        "compose restart backend [buildkit=]",
    ]


def test_an_unknown_service_fails_before_any_is_started(tmp_path, fakes):
    run, calls = _deploy(tmp_path, fakes, services="redis typo")
    assert run.returncode == 1
    assert "has no service typo" in run.stdout
    assert not [c for c in calls if " up " in f" {c} "]
    assert "MOA_DEPLOY_COMPLETE" not in run.stdout


# ---------------------------------------------------------------------------
# The smoke check
# ---------------------------------------------------------------------------


def _smoke(tmp_path, codes: list[str], timeout: int) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "smokebin"
    bin_dir.mkdir()
    queue = tmp_path / "codes"
    queue.write_text("\n".join(codes) + "\n")
    asked = tmp_path / "asked"
    _executable(bin_dir / "curl", f'''
echo "$*" >> "{asked}"
code=$(head -n 1 "{queue}")
tail -n +2 "{queue}" > "{queue}.next" && mv "{queue}.next" "{queue}"
printf '%s' "${{code:-503}}"
''')
    step = _step("Smoke check")
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "SMOKE_URL": "https://api.example/api/v1/plans",
           "SMOKE_TIMEOUT": str(timeout), "SMOKE_INTERVAL": "0", "GITHUB_RUN_ID": "77"}
    return subprocess.run(["bash", "-c", step["run"]], env=env, capture_output=True, text=True, timeout=60)


def test_smoke_passes_on_three_200s_in_a_row(tmp_path):
    run = _smoke(tmp_path, ["502", "000", "200", "502", "200", "200", "200"], timeout=30)
    assert run.returncode == 0, run.stdout
    assert "attempt 7: HTTP 200 (3 of 3 in a row)" in run.stdout
    asked = (tmp_path / "asked").read_text()
    assert "X-Request-ID: deploy-smoke-77-1" in asked and "plans?smoke=77-1" in asked


def test_smoke_fails_the_job_when_the_api_stays_down(tmp_path):
    run = _smoke(tmp_path, ["502"] * 10000, timeout=1)
    assert run.returncode == 1
    assert "::error::The API did not answer 200" in run.stdout


def test_smoke_defaults():
    env = _step("Smoke check")["env"]
    assert "https://api.myotherapp.in/api/v1/plans" in env["SMOKE_URL"] and "vars.PROD_SMOKE_URL" in env["SMOKE_URL"]
    assert "smoke_timeout" in env["SMOKE_TIMEOUT"]
