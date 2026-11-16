"""The one-box Oracle stack (ops/server, docs/MOVE_TO_ORACLE.md): every
service's memory and CPU fit the 4 vCPU, 15 GB box with room to spare; only
Caddy faces the internet; the API's connections fit Postgres; Caddy trusts
Cloudflare alone for the visitor's address, writes no access log and keeps
every old name redirecting; and the scripts parse.

Reads the files only (no database, no app import), so it also runs with
`pytest --noconftest tests/test_oracle_stack.py`.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "ops" / "server"
COMPOSE = SERVER / "compose.yml"
CADDY = SERVER / "caddy"

#: What the services may take together, in MiB: 15 GB less the OS and
#: Docker (about 1 GB) and a few GB for the page cache. The limits are caps;
#: the swapfile is the last resort, never the plan.
ALL_LIMITS_MIB = 11264

#: The budget agreed for the move (docs/MOVE_TO_ORACLE.md, "Capacity").
BUDGET_MIB = {
    "postgres": 2560,
    "backend": 3072,
    "worker": 1024,
    "redis": 300,
    "meilisearch": 768,
    "website": 640,
    "clubs": 512,
    "admin": 448,
    "caddy": 128,
    "media": 256,
    "render": 1024,
}

#: https://www.cloudflare.com/ips/ on 2026-10-24 (ops/server/cloudflare-ips.sh
#: checks the live list on the box).
CLOUDFLARE = {
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32",
    "2a06:98c0::/29", "2c0f:f248::/32",
}


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _services() -> dict:
    """The services `up` starts: the one-off tools (a profile) are not."""
    return {name: svc for name, svc in _compose()["services"].items() if not svc.get("profiles")}


def _mib(limit) -> int:
    number, unit = re.fullmatch(r"(\d+)([mg])", str(limit).lower()).groups()
    return int(number) * (1024 if unit == "g" else 1)


def _default(expr: str) -> int:
    """The default a compose `${VAR:-n}` falls back to."""
    return int(re.fullmatch(r"\$\{\w+:-(\d+)\}", expr).group(1))


def _pg_settings() -> dict:
    command = _services()["postgres"]["command"]
    return dict(arg.split("=", 1) for arg in command if "=" in arg)


def test_every_service_has_limits_a_share_a_rotated_log_and_comes_back():
    for name, service in _services().items():
        assert _mib(service["mem_limit"]) == BUDGET_MIB[name], name
        assert 0 < float(service["cpus"]) <= 3, f"{name}: the box has four cores; none may take them all"
        assert 0 < int(service["cpu_shares"]) <= 1024, name
        assert service.get("restart") == "unless-stopped", name
        assert "resources" not in (service.get("deploy") or {}), name
        options = service["logging"]["options"]
        assert options["max-size"] and options["max-file"], name


def test_the_limits_fit_the_box_and_the_api_and_postgres_come_first():
    services = _services()
    assert sum(_mib(s["mem_limit"]) for s in services.values()) <= ALL_LIMITS_MIB
    shares = {name: int(s["cpu_shares"]) for name, s in services.items()}
    assert shares["backend"] == shares["postgres"] == max(shares.values())
    for background in ("worker", "redis", "meilisearch", "admin", "media", "render"):
        assert shares[background] < shares["backend"], background
        assert float(services[background]["cpus"]) <= (1.5 if background == "worker" else 1), background


def test_only_caddy_faces_the_internet_and_postgres_listens_on_loopback():
    for name, service in _services().items():
        ports = service.get("ports") or []
        if name == "caddy":
            assert sorted(ports) == ["443:443", "80:80"]
        elif name == "postgres":
            assert ports == ["127.0.0.1:5432:5432"]
        else:
            assert not ports, f"{name} must not publish a port"


def test_the_apps_run_pinned_images_from_variables():
    services = _services()
    for name, var in (("backend", "BACKEND_IMAGE"), ("worker", "BACKEND_IMAGE"), ("website", "WEBSITE_IMAGE"),
                      ("clubs", "CLUBS_IMAGE"), ("admin", "ADMIN_IMAGE")):
        assert services[name]["image"].startswith("${" + var + ":-ghcr.io/my-other-app/"), name
    assert services["backend"]["image"] == services["worker"]["image"]


def test_the_api_settings_this_stack_fixes():
    env = _services()["backend"]["environment"]
    assert env["APP_PORT"] == "8000"
    assert env["APP_WORKERS"] == "${MOA_API_WORKERS:-3}"
    assert env["APP_RENDER_THREADS"] == "${MOA_RENDER_THREADS:-2}"
    assert env["APP_DB_MAX_OVERFLOW"] == "${APP_DB_MAX_OVERFLOW:-15}"
    assert env["WALLET_CERTS_DIR"] == "/secrets/walletethings"
    assert "@postgres:5432/" in env["APP_DATABASE_URL"]
    assert env["APP_REDIS_URL"].endswith("redis://${REDIS_PASSWORD:+:${REDIS_PASSWORD}@}redis:6379/0}")
    worker = _services()["worker"]
    assert worker["environment"]["APP_JOBS_CONCURRENCY"] == "${MOA_JOBS_CONCURRENCY:-6}"
    assert worker["environment"]["APP_DATABASE_URL"] == env["APP_DATABASE_URL"]
    assert int(re.fullmatch(r"(\d+)s", worker["stop_grace_period"]).group(1)) > 25


def test_no_long_job_kind_may_hold_more_than_one_worker_slot():
    """A bulk send, an import or a broadcast takes at most one of the worker's
    slots, so one of them alone never starves approval mails, receipts and
    offers. (Several different long kinds at once can still fill the slots;
    priority lanes in the queue are the fix for that.)"""
    env = _services()["worker"]["environment"]
    limits = re.fullmatch(r"\$\{APP_JOBS_LIMITS:-(.+)\}", env["APP_JOBS_LIMITS"]).group(1)
    pairs = dict(item.split("=") for item in limits.split(","))
    assert {"deliveries.send", "emails.send_message", "registrations.import", "notifications.push"} <= set(pairs)
    assert all(int(v) == 1 for v in pairs.values())
    assert _default(env["APP_JOBS_CONCURRENCY"]) >= 6


def test_the_connections_fit_postgres():
    pg = _pg_settings()
    room = int(pg["max_connections"]) - 3  # superuser_reserved_connections
    env = _services()["backend"]["environment"]
    api_workers, api_pool, worker_pool = _default(env["APP_WORKERS"]), 10 + _default(env["APP_DB_MAX_OVERFLOW"]), 4 + 8
    assert api_workers * api_pool + worker_pool <= room - 5, "and a migration, a backup and a psql besides"


def test_postgres_is_tuned_inside_its_limit_and_logs_no_values():
    pg = _pg_settings()
    shared = _mib(pg["shared_buffers"].lower().replace("mb", "m"))
    limit = _mib(_services()["postgres"]["mem_limit"])
    assert shared <= limit * 0.6
    assert pg["log_parameter_max_length"] == "0"
    assert int(pg["max_parallel_workers"]) <= 2


def test_redis_holds_nothing_of_record():
    script = " ".join(_services()["redis"]["command"])
    assert "--appendonly no" in script and '--save ""' in script
    maxmemory = _mib(re.search(r"--maxmemory (\d+)mb", script).group(1) + "m")
    assert maxmemory < _mib(_services()["redis"]["mem_limit"])
    assert script.rstrip().endswith('exec redis-server "$$@"')


def _caddy(name: str) -> str:
    return (CADDY / name).read_text()


def test_caddy_trusts_cloudflare_alone_for_the_visitor():
    global_options = _caddy("global.caddy")
    line = next(l for l in global_options.splitlines() if l.strip().startswith("trusted_proxies static"))
    ranges = {word for word in line.split() if "/" in word}
    assert ranges == CLOUDFLARE
    assert "client_ip_headers CF-Connecting-IP" in global_options
    snippets = _caddy("snippets.caddy")
    # The visitor replaces any X-Forwarded-For a client sent; never appended.
    assert "header_up X-Forwarded-For {client_ip}" in snippets
    assert "header_up X-Real-IP {client_ip}" in snippets


@pytest.mark.parametrize("caddyfile", ["Caddyfile", "Caddyfile.staging"])
def test_every_site_uses_the_origin_certificate_and_writes_no_access_log(caddyfile):
    text = _caddy(caddyfile)
    assert text.splitlines()[0].startswith("#")
    assert "import global.caddy" in text and "import snippets.caddy" in text
    blocks = re.findall(r"^(\S[^\n{]*)\{\n(.*?)^\}", text, flags=re.M | re.S)
    sites = [(head.strip(), body) for head, body in blocks if not head.startswith("(")]
    assert len(sites) >= 4
    for head, body in sites:
        assert "import origin_tls" in body, head
        assert not re.search(r"^\s*log\b", body, flags=re.M), f"{head}: no access log (keys ride in URLs)"
        if "reverse_proxy" in body:
            assert "import upstream" in body and "import common" in body, head


def test_every_old_name_still_redirects_with_its_path():
    text = _caddy("Caddyfile")
    wanted = {
        "www.myotherapp.in": "https://myotherapp.in{uri} 308",
        "events.myotherapp.in": "https://myotherapp.in{uri} 308",
        "clubs-next.myotherapp.in": "https://club-dashboard.myotherapp.in{uri} 308",
        "admin-next.myotherapp.in": "https://main-portal.myotherapp.in{uri} 308",
        "admin-dashboard.myotherapp.in": "https://main-portal.myotherapp.in{uri} 308",
    }
    for host, target in wanted.items():
        block = re.search(r"^[^\n#]*\b" + re.escape(host) + r"\b[^\n]*\{\n(.*?)^\}", text, flags=re.M | re.S)
        assert block and f"redir {target}" in block.group(1), host


def test_the_site_gets_cloudflares_place_under_vercels_names():
    for caddyfile in ("Caddyfile", "Caddyfile.staging"):
        text = _caddy(caddyfile)
        for vercel, cloudflare in (("Latitude", "CF-IPLatitude"), ("Longitude", "CF-IPLongitude"), ("City", "CF-IPCity"),
                                   ("Country-Region", "CF-Region-Code"), ("Country", "CF-IPCountry")):
            assert f"header_up X-Vercel-IP-{vercel} {{http.request.header.{cloudflare}}}" in text, (caddyfile, vercel)


@pytest.mark.parametrize("script", ["deploy.sh", "deploy-gate.sh", "setup.sh", "cloudflare-ips.sh", "dc", "build/build.sh",
                                    "db/app-role.sh"])
def test_the_scripts_parse(script):
    path = SERVER / script
    assert path.read_text().startswith("#!/usr/bin/env bash")
    subprocess.run(["bash", "-n", str(path)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(path)], check=True)


# ─── build/build.sh: the images built on the box ─────────────────────────────

SHAS = {"backend": "37b04f8", "web": "43ec75a", "events": "2c3e7de"}


@pytest.fixture
def build_box(tmp_path):
    """/srv/moa-src and /srv/moa-build as build.sh expects them, a `docker`
    that writes down every build and fails the ones FAIL_BUILDS names, and
    a `flock` that lets it through."""
    src, build, bindir = tmp_path / "src", tmp_path / "build", tmp_path / "bin"
    build_box_dir = str(tmp_path)
    for path in (src / "backend/backend/render", src / "web", src / "events", build, bindir):
        path.mkdir(parents=True)
    (src / "VERSIONS").write_text(" ".join(f"{k}={v}" for k, v in SHAS.items()) + "\n")
    for dockerfile in ("backend/backend/Dockerfile", "backend/backend/render/Dockerfile", "web/Dockerfile", "events/Dockerfile"):
        (src / dockerfile).write_text("FROM scratch\n")
    for app in ("clubs", "admin", "website"):
        (build / f"{app}.production.env").write_text("NEXT_PUBLIC_X=1\n")
    (build / "api_site_token").write_text("not-a-real-token\n")
    calls = tmp_path / "docker-calls"
    (bindir / "docker").write_text(
        "#!/bin/bash\n"
        f'printf "%s\\n" "$*" >> {calls}\n'
        'for word in "$@"; do case " $FAIL_BUILDS " in *" $word "*) exit 1 ;; esac; done\n'
    )
    (bindir / "flock").write_text("#!/bin/sh\nexit 0\n")
    for f in bindir.iterdir():
        f.chmod(0o755)

    def run(*apps, **env):
        environ = {"PATH": f"{bindir}:/usr/bin:/bin", "MOA_SRC": str(src), "MOA_BUILD_DIR": str(build), "HOME": str(tmp_path), **env}
        r = subprocess.run(["bash", str(SERVER / "build/build.sh"), *apps], env=environ, cwd=build_box_dir, capture_output=True, text=True, timeout=60)
        r.calls = calls.read_text().splitlines() if calls.exists() else []
        r.summary = (build / "logs/summary.log").read_text() if (build / "logs/summary.log").exists() else ""
        return r

    run.src, run.build = src, build
    return run


def test_build_sh_tags_each_app_by_its_repositorys_sha_with_its_settings(build_box):
    r = build_box("backend", "clubs", "website", "render")
    assert r.returncode == 0, r.stderr
    backend, clubs, website, render = r.calls
    assert backend.startswith("build ") and f"-t ghcr.io/my-other-app/moa-backend:{SHAS['backend']} " in backend
    assert backend.endswith(str(build_box.src / "backend/backend"))
    assert (build_box.src / "backend/backend/REVISION").read_text().strip() == SHAS["backend"]
    assert f"-t ghcr.io/my-other-app/moa-clubs:{SHAS['web']}" in clubs and "--build-arg APP=clubs" in clubs
    assert f"--secret id=build_env,src={build_box.build}/clubs.production.env" in clubs
    assert re.search(r"--build-arg NODE_IMAGE=node:22-alpine@sha256:[0-9a-f]{64}( |$)", clubs)
    assert f"-t ghcr.io/my-other-app/moa-website:{SHAS['events']}" in website
    assert f"--secret id=api_site_token,src={build_box.build}/api_site_token" in website
    assert "sentry_auth_token" not in website  # only when the file is there
    assert "-t moa-render:8.39.0-1" in render and "render/Dockerfile" in render
    assert "--no-cache" not in " ".join(r.calls)
    assert r.summary.splitlines()[-1].endswith("ALL DONE ok: backend clubs website render failed: none")


def test_build_sh_checks_everything_before_building_anything(build_box):
    (build_box.build / "admin.production.env").unlink()
    r = build_box("backend", "admin")
    assert r.returncode == 1 and "admin.production.env is missing" in r.stderr
    assert r.calls == []


def test_build_sh_says_all_done_with_what_failed_and_retries_the_backend_once(build_box):
    r = build_box("backend", "admin", NO_CACHE_BUILD="1", FAIL_BUILDS=f"ghcr.io/my-other-app/moa-backend:{SHAS['backend']}")
    assert r.returncode == 1
    assert len(r.calls) == 3 and all("--no-cache" in c for c in r.calls)  # backend twice, then admin
    assert r.summary.splitlines()[-1].endswith("ALL DONE ok: admin failed: backend")


def test_build_sh_reads_versions_without_running_it(build_box):
    (build_box.src / "VERSIONS").write_text("backend=$(touch executed) web=43ec75a events=2c3e7de\n")
    r = build_box("backend")
    assert r.returncode == 1 and "no sha for backend" in r.stderr
    assert not (build_box.src.parent / "executed").exists() and r.calls == []


# ─── Settings that change only when .env says so ─────────────────────────────


class Missing(Exception):
    pass


def _interpolate(text, env: dict):
    """Compose's interpolation, for the forms this file uses: $$, ${A},
    ${A:-x}, ${A-x}, ${A:?x}, ${A?x}, ${A:+x}, ${A+x}, nested in x; the
    operator is the one right after the name."""
    if not isinstance(text, str):
        return text
    out, i = [], 0
    while i < len(text):
        if text.startswith("$$", i):
            out.append("$")
            i += 2
        elif text.startswith("${", i):
            depth, j = 1, i + 2
            while depth:
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            out.append(_resolve(text[i + 2:j - 1], env))
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _resolve(inner: str, env: dict) -> str:
    name = re.match(r"[A-Za-z_][A-Za-z0-9_]*", inner).group(0)
    rest, value = inner[len(name):], env.get(name)
    if not rest:
        return value or ""
    op = next(o for o in (":?", "?", ":-", "-", ":+", "+") if rest.startswith(o))
    arg = rest[len(op):]
    if op in (":?", "?"):
        if value is None or (op == ":?" and value == ""):
            raise Missing(arg)
        return value
    if op == ":-":
        return value if value else _interpolate(arg, env)
    if op == "-":
        return value if value is not None else _interpolate(arg, env)
    if op == ":+":
        return _interpolate(arg, env) if value else ""
    return _interpolate(arg, env) if value is not None else ""


def _resolved(env: dict) -> dict:
    def walk(node):
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return _interpolate(node, env)

    return walk(_compose())


#: A .env as the box has it today: none of the settings this file added.
TODAY = {"POSTGRES_USER": "moa", "POSTGRES_PASSWORD": "pg-owner-pw", "POSTGRES_DB": "moa",
         "MEILI_MASTER_KEY": "m" * 24, "GARAGE_RPC_SECRET": "r" * 64, "GARAGE_ADMIN_TOKEN": "a" * 64}
#: Every new setting turned on.
HARDENED = {**TODAY, "MOA_APP_DB_USER": "moa_app", "MOA_APP_DB_PASSWORD": "appdbpw0123456789abcdef0",
            "REDIS_PASSWORD": "redispw0123456789abcdef", "MOA_DATA_NETWORK": "data", "MOA_BACKEND_USER": "10001:10001"}


def _networks(svc: dict) -> list:
    nets = svc.get("networks") or ["default"]
    return sorted(nets if isinstance(nets, list) else nets)


def test_an_unchanged_env_runs_the_stack_as_before():
    doc = _resolved(TODAY)
    services = doc["services"]
    for name in ("backend", "worker"):
        env = services[name]["environment"]
        assert env["APP_DATABASE_URL"] == "postgresql+asyncpg://moa:pg-owner-pw@postgres:5432/moa", name
        assert env["APP_DATABASE_URL_SYNC"] == "postgresql://moa:pg-owner-pw@postgres:5432/moa", name
        assert (env["POSTGRES_USER"], env["POSTGRES_PASSWORD"]) == ("moa", "pg-owner-pw")
        assert env["APP_REDIS_URL"] == "redis://redis:6379/0"
        assert services[name]["user"] == "0:0"  # root, as before
    # One network for everything, as before (the API also joins `data`, empty).
    for name in ("postgres", "redis", "meilisearch", "media", "caddy", "website", "clubs", "admin"):
        assert _networks(services[name]) == ["default"], name
    assert services["redis"]["environment"]["REDIS_PASSWORD"] == ""
    for name, svc in services.items():
        assert set(_networks(svc)) <= set(doc["networks"]), name


def test_with_every_setting_on_the_data_services_and_the_owner_stay_with_the_api():
    doc = _resolved(HARDENED)
    services = doc["services"]
    for name in ("postgres", "redis", "meilisearch"):
        assert _networks(services[name]) == ["data"], name
    for name in ("backend", "worker", "migrate"):
        assert "data" in _networks(services[name]) and "default" in _networks(services[name]), name
        assert services[name]["user"] == "10001:10001", name
    for name in ("caddy", "website", "clubs", "admin", "media", "render"):
        assert "data" not in _networks(services[name]), f"{name} must not reach the data services"
    for name in ("backend", "worker"):
        env = services[name]["environment"]
        assert env["APP_DATABASE_URL"] == "postgresql+asyncpg://moa_app:appdbpw0123456789abcdef0@postgres:5432/moa"
        assert env["APP_REDIS_URL"] == "redis://:redispw0123456789abcdef@redis:6379/0"
        # .env reaches them whole; the owner's password is overridden, the media store's secrets blanked.
        assert "pg-owner-pw" not in " ".join(str(v) for v in env.values()), name
        assert env["GARAGE_RPC_SECRET"] == env["GARAGE_ADMIN_TOKEN"] == ""
    # Migrations and the drill keep the owner.
    migrate = services["migrate"]
    assert migrate["environment"]["APP_DATABASE_URL_SYNC"] == "postgresql://moa:pg-owner-pw@postgres:5432/moa"
    assert migrate["profiles"] == ["tools"] and migrate["restart"] == "no" and not migrate.get("ports")
    assert migrate["image"] == services["backend"]["image"]
    assert services["redis"]["environment"]["REDIS_PASSWORD"] == "redispw0123456789abcdef"
    for name, svc in services.items():
        assert set(_networks(svc)) <= set(doc["networks"]), name


def test_a_missing_owner_setting_is_still_a_loud_error():
    for name in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
        env = {k: v for k, v in TODAY.items() if k != name}
        with pytest.raises(Missing):
            _resolved(env)


def test_redis_asks_for_its_password_only_when_there_is_one():
    redis = _compose()["services"]["redis"]
    script = redis["command"][-1]
    assert redis["command"][:2] == ["sh", "-c"] and redis["user"] == "redis"
    assert 'if [ -n "$$REDIS_PASSWORD" ]; then set -- "$$@" --requirepass "$$REDIS_PASSWORD"; fi' in script
    assert redis["environment"]["REDIS_PASSWORD"] == "${REDIS_PASSWORD:-}"
    check = redis["healthcheck"]["test"]
    assert check[0] == "CMD-SHELL" and "REDISCLI_AUTH" in check[1] and "PONG" in check[1]


#: Every image compose.yml runs that is not one of our own (images.env,
#: RENDER_IMAGE), with the tag that ran before the digests were added.
BASE_TAGS = {"caddy": "caddy:2.11-alpine", "postgres": "postgres:17", "redis": "redis:7-alpine",
             "meilisearch": "getmeili/meilisearch:v1.54.3", "media": "dxflrs/garage:v2.4.1"}


def test_base_images_are_pinned_by_digest_on_the_tags_in_use():
    services = _compose()["services"]
    for name, tag in BASE_TAGS.items():
        assert re.fullmatch(re.escape(tag) + r"@sha256:[0-9a-f]{64}", services[name]["image"]), name
    for name, svc in services.items():
        if name not in BASE_TAGS:
            assert svc["image"].startswith("${"), f"{name}: an image of ours comes from a variable"
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    assert re.search(r"^FROM python:3\.11-bookworm@sha256:[0-9a-f]{64}$", dockerfile, re.M)
    build = (SERVER / "build" / "build.sh").read_text()
    assert re.search(r"node:22-alpine@sha256:[0-9a-f]{64}", build)


def test_the_backend_image_runs_as_its_own_user():
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    users = re.findall(r"^USER (\S+)$", dockerfile, re.M)
    assert users and users[-1] == "10001:10001"
    assert "--uid 10001" in dockerfile and "--home-dir /home/moa" in dockerfile
