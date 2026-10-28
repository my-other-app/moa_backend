"""The one-box Oracle stack (ops/server, docs/MOVE_TO_ORACLE.md): every
service's memory and CPU fit a 2-core, 12 GB box with room to spare; only
Caddy faces the internet; the API's connections fit Postgres; Caddy trusts
Cloudflare alone for the visitor's address, writes no access log and keeps
every old name redirecting; and the scripts parse.

Reads the files only (no database, no app import), so it also runs with
`pytest --noconftest tests/test_oracle_stack.py`.
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "ops" / "server"
COMPOSE = SERVER / "compose.yml"
CADDY = SERVER / "caddy"

#: What the services may take together, in MiB: 12 GB less the OS and
#: Docker (about 1 GB) and room for render peaks and the page cache. The
#: limits are caps; the 4 GB swapfile is the last resort, never the plan.
ALL_LIMITS_MIB = 8448

#: The budget agreed for the move (docs/MOVE_TO_ORACLE.md, "Capacity").
BUDGET_MIB = {
    "postgres": 2560,
    "backend": 2048,
    "worker": 768,
    "redis": 300,
    "meilisearch": 768,
    "website": 640,
    "clubs": 512,
    "admin": 448,
    "caddy": 128,
    "media": 256,
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
    return _compose()["services"]


def _mib(limit) -> int:
    number, unit = re.fullmatch(r"(\d+)([mg])", str(limit).lower()).groups()
    return int(number) * (1024 if unit == "g" else 1)


def _pg_settings() -> dict:
    command = _services()["postgres"]["command"]
    return dict(arg.split("=", 1) for arg in command if "=" in arg)


def test_every_service_has_limits_a_share_a_rotated_log_and_comes_back():
    for name, service in _services().items():
        assert _mib(service["mem_limit"]) == BUDGET_MIB[name], name
        assert 0 < float(service["cpus"]) <= 2, f"{name}: the box has two cores"
        assert 0 < int(service["cpu_shares"]) <= 1024, name
        assert service.get("restart") == "unless-stopped", name
        assert "resources" not in (service.get("deploy") or {}), name
        options = service["logging"]["options"]
        assert options["max-size"] and options["max-file"], name


def test_the_limits_fit_a_12gb_box_and_the_api_and_postgres_come_first():
    services = _services()
    assert sum(_mib(s["mem_limit"]) for s in services.values()) <= ALL_LIMITS_MIB
    shares = {name: int(s["cpu_shares"]) for name, s in services.items()}
    assert shares["backend"] == shares["postgres"] == max(shares.values())
    for background in ("worker", "redis", "meilisearch", "admin", "media"):
        assert shares[background] < shares["backend"], background
        assert float(services[background]["cpus"]) <= 1, background


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
    assert env["APP_WORKERS"] == "${MOA_API_WORKERS:-2}"
    assert env["APP_RENDER_THREADS"] == "${MOA_RENDER_THREADS:-1}"
    assert env["WALLET_CERTS_DIR"] == "/secrets/walletethings"
    assert "@postgres:5432/" in env["APP_DATABASE_URL"]
    assert env["APP_REDIS_URL"].endswith("redis://redis:6379/0}")
    worker = _services()["worker"]
    assert worker["environment"]["APP_JOBS_CONCURRENCY"] == "2"
    assert worker["environment"]["APP_DATABASE_URL"] == env["APP_DATABASE_URL"]
    assert int(re.fullmatch(r"(\d+)s", worker["stop_grace_period"]).group(1)) > 25


def test_the_connections_fit_postgres():
    pg = _pg_settings()
    room = int(pg["max_connections"]) - 3  # superuser_reserved_connections
    api_workers, api_pool, worker_pool = 2, 10 + 20, 4 + 8
    assert api_workers * api_pool + worker_pool <= room - 5, "and a migration, a backup and a psql besides"


def test_postgres_is_tuned_inside_its_limit_and_logs_no_values():
    pg = _pg_settings()
    shared = _mib(pg["shared_buffers"].lower().replace("mb", "m"))
    limit = _mib(_services()["postgres"]["mem_limit"])
    assert shared <= limit * 0.6
    assert pg["log_parameter_max_length"] == "0"
    assert int(pg["max_parallel_workers"]) <= 2


def test_redis_holds_nothing_of_record():
    command = _services()["redis"]["command"]
    assert command[command.index("--appendonly") + 1] == "no"
    assert command[command.index("--save") + 1] == ""
    maxmemory = _mib(command[command.index("--maxmemory") + 1].replace("mb", "m"))
    assert maxmemory < _mib(_services()["redis"]["mem_limit"])


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


@pytest.mark.parametrize("script", ["deploy.sh", "deploy-gate.sh", "setup.sh", "cloudflare-ips.sh", "dc"])
def test_the_scripts_parse(script):
    path = SERVER / script
    assert path.read_text().startswith("#!/usr/bin/env bash")
    subprocess.run(["bash", "-n", str(path)], check=True)
