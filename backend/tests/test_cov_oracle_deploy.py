"""ops/server/deploy.sh run for real against a stand-in Docker: a healthy
image goes live pinned by digest with the worker after it; an unhealthy one
is swapped back to the previous image; a worker that fails takes the API back
with it; an image whose migrations the database lacks is refused unless
--migrate, which migrates with the new image first; `rollback` puts the image
that ran before the last good deploy back.

The stand-ins (on PATH, ahead of the real ones): `docker` keeps what each
service runs in a JSON file and answers health from the image's tag
(bad-api-* never gets healthy; bad-worker-* is fine for the API and never
for the worker); `id` says root; `flock`, `sleep` and `curl` do nothing much.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "ops" / "server" / "deploy.sh"
REGISTRY = "ghcr.io/my-other-app"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE_DOCKER = r'''
import hashlib, json, os, sys

state_path = os.environ["FAKE_DOCKER_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
state["calls"].append(args)
VARS = {"backend": "BACKEND_IMAGE", "worker": "BACKEND_IMAGE", "website": "WEBSITE_IMAGE", "clubs": "CLUBS_IMAGE", "admin": "ADMIN_IMAGE"}


def done(code=0, out=""):
    json.dump(state, open(state_path, "w"))
    if out:
        print(out)
    sys.exit(code)


def healthy(service):
    ref = state["running"].get(service, "")
    tag = ref.split("@")[0].rsplit(":", 1)[-1]
    if tag.startswith("bad-api"):
        return False
    return not (service == "worker" and tag.startswith("bad-worker"))


if args[:1] == ["pull"]:
    done(0)
if args[:2] == ["image", "inspect"]:
    ref = args[-1]
    repo = ref.split("@")[0].rsplit(":", 1)[0]
    done(0, f"{repo}@sha256:{hashlib.sha256(ref.encode()).hexdigest()}")
if args[:2] in (["image", "ls"], ["image", "rm"], ["image", "prune"]):
    done(0)
if args[:1] == ["inspect"]:
    fmt, cid = args[2], args[3]
    service = cid.removeprefix("c-")
    if "RestartCount" in fmt:
        done(0, "running 0")
    done(0, "healthy" if healthy(service) else "unhealthy")
if args[:1] == ["compose"]:
    rest, i = args[1:], 0
    while i < len(rest) and rest[i] in ("--project-directory", "-f", "--env-file"):
        i += 2
    rest = rest[i:]
    verb = rest[0]
    if verb == "version":
        done(0, "Docker Compose version v2.29.0")
    if verb == "up":
        service = rest[-1]
        state["running"][service] = os.environ.get(VARS[service], "")
        state["ups"].append([service, state["running"][service]])
        done(0)
    if verb == "ps":
        done(0, f"c-{rest[-1]}" if rest[-1] in state["running"] else "")
    if verb == "exec":
        done(0 if healthy("backend") else 1, "/health/ready 200 ok")
    if verb == "run":
        image = os.environ.get("BACKEND_IMAGE", "")
        if "alembic" in rest:
            state["migrated_with"].append(image)
            state["db_behind"] = False
            done(0)
        done(3 if state["db_behind"] else 0, "migrations: compared")
    if verb in ("logs", "stop"):
        done(0)
done(97, "fake docker: unexpected " + " ".join(args))
'''


@pytest.fixture
def box(tmp_path):
    """/srv/moa as deploy.sh expects it, and the stand-ins on PATH."""
    moa = tmp_path / "moa"
    (moa / "state").mkdir(parents=True)
    (moa / "compose.yml").write_text("services: {}\n")
    (moa / ".env").write_text("MOA_API_HOST=api.example.test\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(f"#!{sys.executable}\n{FAKE_DOCKER}")
    (bin_ / "id").write_text("#!/bin/sh\necho 0\n")
    (bin_ / "flock").write_text("#!/bin/sh\nexit 0\n")
    (bin_ / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bin_ / "curl").write_text("#!/bin/sh\necho 200\n")
    for f in bin_.iterdir():
        f.chmod(0o755)
    state = tmp_path / "docker.json"
    state.write_text(json.dumps({"calls": [], "running": {}, "ups": [], "migrated_with": [], "db_behind": False}))

    class Box:
        dir = moa

        def run(self, *args):
            env = {"PATH": f"{bin_}:/usr/bin:/bin", "MOA_DIR": str(moa), "FAKE_DOCKER_STATE": str(state), "HOME": str(tmp_path)}
            return subprocess.run(["bash", str(DEPLOY), *args], env=env, capture_output=True, text=True, timeout=120)

        @property
        def docker(self) -> dict:
            return json.loads(state.read_text())

        def set(self, **values):
            s = self.docker
            s.update(values)
            state.write_text(json.dumps(s))

        def images(self) -> dict:
            path = moa / "images.env"
            if not path.exists():
                return {}
            return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))

        def history(self) -> list[list[str]]:
            path = moa / "state" / "history.log"
            return [line.split()[1:] for line in path.read_text().splitlines()] if path.exists() else []

    return Box()


def _ref(tag: str) -> str:
    return f"{REGISTRY}/moa-backend:{tag}"


def _ok(r):
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.rstrip().endswith("MOA_DEPLOY_OK")


async def test_a_healthy_image_goes_live_pinned_with_the_worker_after_it(box):
    _ok(box.run("backend", "good-1", "--timeout", "5"))
    pinned = box.images()["BACKEND_IMAGE"]
    assert pinned.startswith(_ref("good-1") + "@sha256:") and len(pinned.split("@sha256:")[1]) == 64
    # The API first, then the worker on the very same image.
    assert box.docker["ups"] == [["backend", pinned], ["worker", pinned]]
    assert box.history() == [["backend", "ok", pinned, "-"], ["worker", "ok", pinned, "-"]]


async def test_an_unhealthy_image_is_swapped_back_to_the_previous_one(box):
    _ok(box.run("backend", "good-1", "--timeout", "5"))
    before = box.images()["BACKEND_IMAGE"]
    r = box.run("backend", "bad-api-2", "--timeout", "2")
    assert r.returncode != 0 and "MOA_DEPLOY_OK" not in r.stdout
    assert "rolled back" in r.stderr
    assert box.images()["BACKEND_IMAGE"] == before  # images.env never named the bad one
    assert box.docker["running"]["backend"] == before
    assert [h[:2] for h in box.history()[-2:]] == [["backend", "failed"], ["backend", "rolled-back"]]
    assert box.docker["running"]["worker"] == before  # the worker never moved


async def test_a_worker_that_fails_takes_the_api_back_with_it(box):
    _ok(box.run("backend", "good-1", "--timeout", "5"))
    before = box.images()["BACKEND_IMAGE"]
    r = box.run("backend", "bad-worker-2", "--timeout", "2")
    assert r.returncode != 0 and "backend and worker rolled back" in r.stderr
    assert box.images()["BACKEND_IMAGE"] == before
    assert box.docker["running"]["backend"] == before and box.docker["running"]["worker"] == before
    assert ["worker", "failed"] == box.history()[-2][:2] and ["backend", "rolled-back"] == box.history()[-1][:2]


async def test_an_image_ahead_of_the_database_needs_migrate_which_runs_with_the_new_image_first(box):
    _ok(box.run("backend", "good-1", "--timeout", "5"))
    before = box.images()["BACKEND_IMAGE"]
    box.set(db_behind=True)
    refused = box.run("backend", "good-2", "--timeout", "5")
    assert refused.returncode != 0 and "--migrate" in refused.stderr and "Nothing was changed" in refused.stderr
    assert box.images()["BACKEND_IMAGE"] == before and box.docker["running"]["backend"] == before

    _ok(box.run("backend", "good-2", "--migrate", "--timeout", "5"))
    new = box.images()["BACKEND_IMAGE"]
    assert new.startswith(_ref("good-2")) and box.docker["migrated_with"] == [new]
    # Migrated before the swap: the migration is on record ahead of the deploy.
    assert [h[:2] for h in box.history()[-3:]] == [["backend", "migrated"], ["backend", "ok"], ["worker", "ok"]]


async def test_rollback_puts_back_what_ran_before_the_last_good_deploy(box):
    _ok(box.run("backend", "good-1", "--timeout", "5"))
    first = box.images()["BACKEND_IMAGE"]
    _ok(box.run("backend", "good-2", "--timeout", "5"))
    _ok(box.run("rollback", "backend"))
    assert box.images()["BACKEND_IMAGE"] == first and box.docker["running"]["backend"] == first
    assert box.docker["running"]["worker"] == first


async def test_what_it_refuses_before_touching_anything(box, tmp_path):
    for args, said in ((("backend", "no/such:tag"), "not an image tag"), (("rollback", "nginx"), "unknown service"),
                       (("website", "good-1", "--migrate"), "backend only"), (("rollback", "backend"), "no earlier image")):
        r = box.run(*args)
        assert r.returncode != 0 and said in r.stderr, (args, r.stderr)
    # A verb it does not know: the usage, exit 64.
    r = box.run("nginx", "good-1")
    assert r.returncode == 64 and "deploy.sh <service> <tag>" in r.stdout
    assert box.docker["ups"] == [] and box.images() == {}
    # Not root: nothing at all.
    (tmp_path / "bin" / "id").write_text("#!/bin/sh\necho 1000\n")
    r = box.run("backend", "good-1")
    assert r.returncode != 0 and "run as root" in r.stderr
