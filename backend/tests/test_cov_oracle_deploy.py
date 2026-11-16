"""ops/server/deploy.sh run for real against a stand-in Docker: a healthy
image goes live pinned by digest with the worker after it; an unhealthy one
is swapped back to the previous image; a worker that fails takes the API back
with it; an image whose migrations the database lacks is refused unless
--migrate, which migrates with the new image first; `rollback` puts the image
that ran before the last good deploy back.

The stand-ins (on PATH, ahead of the real ones): `docker` keeps what each
service runs in a JSON file and answers health from the image's tag
(bad-api never gets healthy; bad-worker is fine for the API and never for
the worker); a box-* tag was built on the box (there, no digest, and the
registry refuses it); a missing-* tag is nowhere; `id` says root; `flock`,
`sleep` and `curl` do nothing much.

With --local (box-built images) the same health wait and rollback apply and
nothing is ever pulled; --migrate dumps the database first and migrates as
the database's owner (the `migrate` service when compose.yml has one).
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
VARS = {"backend": "BACKEND_IMAGE", "worker": "BACKEND_IMAGE", "migrate": "BACKEND_IMAGE", "website": "WEBSITE_IMAGE", "clubs": "CLUBS_IMAGE", "admin": "ADMIN_IMAGE"}


def done(code=0, out=""):
    json.dump(state, open(state_path, "w"))
    if out:
        print(out)
    sys.exit(code)


def tag_of(ref):
    return ref.split("@")[0].rsplit(":", 1)[-1]


def built_here(ref):
    return tag_of(ref).startswith("box-")


def nowhere(ref):
    return tag_of(ref).startswith("missing-")


def healthy(service):
    tag = tag_of(state["running"].get(service, ""))
    if "bad-api" in tag:
        return False
    return not (service == "worker" and "bad-worker" in tag)


if args[:1] == ["pull"]:
    state["pulled"].append(args[-1])
    done(1 if built_here(args[-1]) or nowhere(args[-1]) else 0)
if args[:2] == ["image", "inspect"]:
    ref = args[-1]
    if nowhere(ref):
        done(1, "Error: No such image")
    repo = ref.split("@")[0].rsplit(":", 1)[0]
    if "RepoDigests" in " ".join(args):
        done(0, "" if built_here(ref) else f"{repo}@sha256:{hashlib.sha256(ref.encode()).hexdigest()}")
    done(0, "sha256:" + hashlib.sha256(ref.split("@")[0].encode()).hexdigest())
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
    while i < len(rest) and rest[i] in ("--project-directory", "-f", "--env-file", "--profile"):
        i += 2
    rest = rest[i:]
    verb = rest[0]
    if verb == "version":
        done(0, "Docker Compose version v2.29.0")
    if verb == "config":
        done(0, "\n".join(state["services"]))
    if verb == "up":
        service = rest[-1]
        state["running"][service] = os.environ.get(VARS[service], "")
        state["ups"].append([service, state["running"][service]])
        done(0)
    if verb == "ps":
        done(0, f"c-{rest[-1]}" if rest[-1] in state["running"] else "")
    if verb == "exec":
        if any("pg_dump" in a for a in rest):
            state["dumps"] += 1
            done(0, "PGDMP fake dump")
        done(0 if healthy("backend") else 1, "/health/ready 200 ok")
    if verb == "run":
        image = os.environ.get("BACKEND_IMAGE", "")
        service = next(a for a in rest[1:] if not a.startswith("-"))
        if "alembic" in rest:
            state["migrated_with"].append([service, image])
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
    (moa / "deploy.conf").write_text(f"MOA_BACKUP_DIR={tmp_path / 'backups'}\n")
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
    state.write_text(json.dumps({"calls": [], "running": {}, "ups": [], "migrated_with": [], "db_behind": False,
                                 "pulled": [], "dumps": 0, "services": ["backend", "worker", "migrate"]}))

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
    # As the database's owner (the migrate service), after a dump.
    assert new.startswith(_ref("good-2")) and box.docker["migrated_with"] == [["migrate", new]]
    assert box.docker["dumps"] == 1
    [dump] = (box.dir.parent / "backups").glob("pre-migrate-good-2-*.dump")
    assert dump.read_bytes().startswith(b"PGDMP")
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


# ─── Images built on the box (--local) ──────────────────────────────────────


async def test_a_box_built_image_goes_live_by_its_tag_and_nothing_is_pulled(box):
    _ok(box.run("backend", "box-1", "--local", "--timeout", "5"))
    ref = _ref("box-1")
    assert box.images()["BACKEND_IMAGE"] == ref  # no digest: it never came from a registry
    assert box.docker["ups"] == [["backend", ref], ["worker", ref]]
    assert box.docker["pulled"] == []
    assert box.history() == [["backend", "ok", ref, "-"], ["worker", "ok", ref, "-"]]


async def test_an_unhealthy_box_built_image_is_swapped_back_without_a_registry(box):
    _ok(box.run("backend", "box-1", "--local", "--timeout", "5"))
    r = box.run("backend", "box-bad-api-2", "--local", "--timeout", "2")
    assert r.returncode != 0 and "rolled back" in r.stderr, r.stderr
    assert box.images()["BACKEND_IMAGE"] == _ref("box-1")
    assert box.docker["running"]["backend"] == _ref("box-1") == box.docker["running"]["worker"]
    assert box.docker["pulled"] == []


async def test_a_box_built_worker_that_fails_takes_the_api_back_with_it(box):
    _ok(box.run("backend", "box-1", "--local", "--timeout", "5"))
    r = box.run("backend", "box-bad-worker-2", "--local", "--timeout", "2")
    assert r.returncode != 0 and "backend and worker rolled back" in r.stderr
    assert box.images()["BACKEND_IMAGE"] == _ref("box-1")
    assert box.docker["running"]["backend"] == _ref("box-1") == box.docker["running"]["worker"]


async def test_rollback_of_a_box_built_image_pulls_nothing(box):
    _ok(box.run("website", "box-1", "--local", "--timeout", "5"))
    _ok(box.run("website", "box-2", "--local", "--timeout", "5"))
    _ok(box.run("rollback", "website"))
    first = f"{REGISTRY}/moa-website:box-1"
    assert box.images()["WEBSITE_IMAGE"] == first and box.docker["running"]["website"] == first
    assert box.docker["pulled"] == []


async def test_local_refuses_an_image_that_is_not_on_the_box(box):
    r = box.run("clubs", "missing-1", "--local")
    assert r.returncode != 0 and "is not on this box" in r.stderr and "Nothing was changed" in r.stderr
    assert box.docker["ups"] == [] and box.images() == {} and box.docker["pulled"] == []


async def test_without_local_a_box_built_image_is_refused_with_a_hint(box):
    r = box.run("backend", "box-1", "--timeout", "5")
    assert r.returncode != 0 and "--local" in r.stderr and "Nothing was changed" in r.stderr
    assert box.docker["ups"] == [] and box.images() == {}


async def test_local_with_migrate_dumps_then_migrates_as_the_owner_with_the_new_image(box):
    _ok(box.run("backend", "box-1", "--local", "--timeout", "5"))
    box.set(db_behind=True)
    refused = box.run("backend", "box-2", "--local", "--timeout", "5")
    assert refused.returncode != 0 and "--migrate" in refused.stderr
    assert box.docker["dumps"] == 0 and box.images()["BACKEND_IMAGE"] == _ref("box-1")
    _ok(box.run("backend", "box-2", "--local", "--migrate", "--timeout", "5"))
    assert box.docker["migrated_with"] == [["migrate", _ref("box-2")]]
    assert box.docker["dumps"] == 1 and box.docker["pulled"] == []
    assert box.images()["BACKEND_IMAGE"] == _ref("box-2")


async def test_migrate_falls_back_to_the_api_service_on_an_older_compose_file(box):
    box.set(services=["backend", "worker"], db_behind=True)
    _ok(box.run("backend", "box-1", "--local", "--migrate", "--timeout", "5"))
    assert box.docker["migrated_with"] == [["backend", _ref("box-1")]]


async def test_images_env_keeps_its_other_lines_and_a_copy_is_kept_first(box):
    images = box.dir / "images.env"
    images.write_text(
        f"BACKEND_IMAGE={_ref('box-0')}\nRENDER_IMAGE=moa-render:8.39.0-1\n"
        f"WEBSITE_IMAGE={REGISTRY}/moa-website:box-0\n"
    )
    _ok(box.run("backend", "box-1", "--local", "--timeout", "5"))
    assert box.images() == {
        "BACKEND_IMAGE": _ref("box-1"),
        "RENDER_IMAGE": "moa-render:8.39.0-1",
        "WEBSITE_IMAGE": f"{REGISTRY}/moa-website:box-0",
    }
    [copy] = (box.dir / "state").glob("images.env.before-backend-*Z")
    assert f"BACKEND_IMAGE={_ref('box-0')}" in copy.read_text()
    # The previous image is the rollback target.
    assert box.history()[0] == ["backend", "ok", _ref("box-1"), _ref("box-0")]


async def test_start_local_pulls_nothing_and_rejects_other_words(box):
    r = box.run("start", "--everything")
    assert r.returncode == 64 and box.docker["pulled"] == []
