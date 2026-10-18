"""Everything on one box (docker-compose.prod.yml, docs/OPERATIONS.md section
10): every service has its memory and CPU limits, comes back after a restart
and keeps its log small; the limits together fit what an 8 GB box has for
them; Redis holds no data of record; and the job worker's database pool is
counted with the API's against Postgres's max_connections."""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from app.config import settings

BACKEND = Path(__file__).resolve().parents[1]
COMPOSE = BACKEND.parent / "docker-compose.prod.yml"

#: What an 8 GB box has for these services, in MiB: about 7.6 GiB usable,
#: less the two other projects (0.95 GB measured, 1 GiB kept), the OS,
#: Docker and the host's nginx (0.5 GiB), and builds and the host's own page
#: cache (0.85 GiB). docs/OPERATIONS.md, section 10, has the table.
OURS_ON_8GB_MIB = 5376


def _services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def _mib(limit: str) -> int:
    number, unit = re.fullmatch(r"(\d+)([mg])", str(limit).lower()).groups()
    return int(number) * (1024 if unit == "g" else 1)


def test_every_service_has_a_memory_and_cpu_limit_and_comes_back_after_a_restart():
    for name, service in _services().items():
        assert "mem_limit" in service and _mib(service["mem_limit"]) > 0, name
        assert 0 < float(service["cpus"]) <= 2, f"{name}: the box has two vCPUs"
        assert service.get("restart") == "unless-stopped", name
        # docker-compose v2 refuses both ways of saying it at once.
        assert "resources" not in (service.get("deploy") or {}), name
        # Its log is a file on a small disk: rotated.
        assert service["logging"]["options"]["max-size"] and service["logging"]["options"]["max-file"], name


def test_the_limits_fit_an_8gb_box_and_the_background_ones_leave_the_api_a_cpu():
    services = _services()
    assert sum(_mib(s["mem_limit"]) for s in services.values()) <= OURS_ON_8GB_MIB
    for background in ("worker", "redis", "meilisearch"):
        assert float(services[background]["cpus"]) <= 1, background


def test_redis_holds_nothing_of_record_and_is_not_published():
    redis = _services()["redis"]
    command = redis["command"]
    assert command[command.index("--maxmemory") + 1] == "192mb"
    assert command[command.index("--maxmemory-policy") + 1] == "allkeys-lru"
    assert command[command.index("--appendonly") + 1] == "no"
    assert command[command.index("--save") + 1] == ""
    assert "ports" not in redis
    # maxmemory is Redis's data; the process needs a little over it.
    assert _mib(redis["mem_limit"]) > 192


def test_meilisearch_indexes_inside_its_limit():
    meili = _services()["meilisearch"]
    env = meili["environment"]
    indexing = int(re.fullmatch(r"(\d+)MiB", env["MEILI_MAX_INDEXING_MEMORY"]).group(1))
    assert indexing * 2 < _mib(meili["mem_limit"])
    assert int(env["MEILI_MAX_INDEXING_THREADS"]) <= float(meili["cpus"])
    assert "ports" not in meili


def test_the_worker_runs_two_jobs_on_its_own_pool_and_drains_before_it_is_killed():
    worker = _services()["worker"]
    env = worker["environment"]
    assert env["APP_JOBS_CONCURRENCY"] == "2"
    # Its pool comes from APP_WORKER_DB_* (which the API reads too, for the
    # budget), never from an override the API cannot see.
    assert "APP_DB_POOL_SIZE" not in env and "APP_DB_MAX_OVERFLOW" not in env
    grace = int(re.fullmatch(r"(\d+)s", worker["stop_grace_period"]).group(1))
    assert grace > settings.JOBS_DRAIN_SECONDS


# ─── The connection budget, with the job worker ─────────────────────────────


def test_the_budget_counts_the_job_worker_on_top_of_the_api():
    from app.db.core import connection_budget

    # Three API workers at 30 fit in 97; the job worker's 12 more do not.
    assert connection_budget(3, 10, 20, 100, 3) is None
    problem = connection_budget(3, 10, 20, 100, 3, job_worker=(4, 8))
    assert "the job worker (pool 4 + overflow 8)" in problem and "102 connections" in problem
    assert connection_budget(2, 10, 20, 100, 3, job_worker=(4, 8)) is None


def test_the_job_worker_is_counted_in_the_worker_and_in_an_api_that_leaves_work_to_it(monkeypatch):
    from app.db.core import job_worker_pool

    monkeypatch.setattr(settings, "WORKER_DB_POOL_SIZE", 4)
    monkeypatch.setattr(settings, "WORKER_DB_MAX_OVERFLOW", 8)
    assert job_worker_pool() is None
    monkeypatch.setattr(settings, "JOBS_WORKER", True)
    assert job_worker_pool() == (4, 8)
    monkeypatch.setattr(settings, "JOBS_WORKER", False)
    monkeypatch.setattr(settings, "SWEEPS_IN_WORKER", True)
    assert job_worker_pool() == (4, 8)
    monkeypatch.setattr(settings, "SWEEPS_IN_WORKER", False)
    monkeypatch.setattr(settings, "PROCESS", "worker")
    assert job_worker_pool() == (4, 8)


async def test_startup_warns_when_the_job_worker_tips_the_budget_over(_engine, monkeypatch, caplog):
    import logging

    from app.db.core import check_connection_budget

    async with _engine.connect() as conn:
        from sqlalchemy import text

        max_connections = int(await conn.scalar(text("SHOW max_connections")))
        reserved = int(await conn.scalar(text("SHOW superuser_reserved_connections")))
    room = max_connections - reserved
    monkeypatch.setattr(settings, "WORKERS", 1)
    monkeypatch.setattr(settings, "DB_POOL_SIZE", room - 1)
    monkeypatch.setattr(settings, "DB_MAX_OVERFLOW", 0)
    monkeypatch.setattr(settings, "WORKER_DB_POOL_SIZE", 1)
    monkeypatch.setattr(settings, "WORKER_DB_MAX_OVERFLOW", 1)
    assert await check_connection_budget(_engine) is None
    monkeypatch.setattr(settings, "JOBS_WORKER", True)
    with caplog.at_level(logging.WARNING, logger="app.db.budget"):
        problem = await check_connection_budget(_engine)
    assert problem and "the job worker" in problem


def test_the_pool_is_the_job_workers_own_in_the_worker():
    from app.db.core import pool_for

    class Config:
        PROCESS = "worker"
        DB_POOL_SIZE, DB_MAX_OVERFLOW = 10, 20
        WORKER_DB_POOL_SIZE, WORKER_DB_MAX_OVERFLOW = 4, 8

    assert pool_for(Config) == (4, 8)
    Config.PROCESS = "api"
    assert pool_for(Config) == (10, 20)


def test_a_process_started_as_the_worker_builds_its_engine_with_the_workers_pool():
    """`python -m app.worker` says APP_PROCESS=worker before the settings are
    read, and the engine app.db.core builds at import takes that pool."""
    env = {**os.environ, "APP_PROCESS": "worker", "APP_WORKER_DB_POOL_SIZE": "3", "APP_WORKER_DB_MAX_OVERFLOW": "5"}
    code = "from app.db.core import engine; p = engine.sync_engine.pool; print(p.size(), p._max_overflow)"
    out = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[-2:] == ["3", "5"]


def test_the_worker_entrypoint_says_so_before_anything_reads_the_settings():
    text = (BACKEND / "app" / "worker.py").read_text()
    main = text[text.index('if __name__ == "__main__":'):]
    assert main.index('os.environ["APP_PROCESS"] = "worker"') < main.index("from app.")
    # Nothing at the module's top level reads the settings first.
    assert not re.search(r"^(from app|import app)", text[: text.index('if __name__ == "__main__":')], re.M)


@pytest.mark.parametrize("process", ["api", "worker"])
def test_sentry_tags_events_with_the_process(monkeypatch, process):
    """The worker imports doth, which starts Sentry: its events say worker."""
    sentry_sdk = pytest.importorskip("sentry_sdk")
    from app.core import observability

    tags = {}
    monkeypatch.setattr(sentry_sdk, "init", lambda **_kw: None)
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda key, value: tags.__setitem__(key, value))
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://key@o0.ingest.example/1")
    monkeypatch.setattr(settings, "PROCESS", process)
    monkeypatch.setattr(observability, "_sentry_started", False, raising=False)
    assert observability.init_sentry() is True
    assert tags["component"] == process
