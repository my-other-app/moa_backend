"""search_text: one normalised column per searchable row, kept by the
database on every write path, and the searches that read it.

- What an event, a club and an account are found by, and that discover's
  search, the app's lists and the admin's user search find them by it
  (unaccented, by interest and topic, by the club's city).
- Every write path keeps it right -- the ORM, raw SQL, interest links made,
  moved, deleted and hard-deleted, a club renamed or moved, an event's
  category or tag changed, the dashboard's create and edit -- checked
  against the rows (text.drift) after each; a category, interest or topic
  renamed, moved or deleted is put right by the outbox drain, in batches.
- The query is normalised as the column is: in Python for ASCII, by the
  database's own function otherwise, and the two agree.
- The backfills never wait on a row someone else holds, and commit as they
  go: the runtime one (text.backfill) and the migration's.
"""
import asyncio
import importlib.util
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubInterestsLink
from app.api.events.models import EventCategories, EventInterestsLink, Events
from app.api.interests.models import InterestCategory, Interests
from app.api.search import sync, text as stext
from app.db.listeners import add_loader_criteria
from tests.conftest import auth_header

API = "/api/v1"


@pytest.fixture(autouse=True)
def _as_production(session):
    add_loader_criteria(session)


async def _text(session, table: str, row_id: int) -> str:
    return await session.scalar(text(f"SELECT search_text FROM {table} WHERE id = :id"), {"id": row_id})


async def _no_drift(session):
    found = await stext.drift(session)
    assert found == {"events": [], "clubs": [], "users": []}, found


@pytest.fixture
async def scene(session, make_club):
    tag = uuid.uuid4().hex[:6]
    owner, club = await make_club(name=f"Kerala Robotics Guild {tag}", email=f"guild-{tag}@example.com")
    club.location_name = "Kochi"
    topic = InterestCategory(name=f"Deep Tech {tag}", slug=f"deep-tech-{tag}")
    other_topic = InterestCategory(name=f"Arts {tag}", slug=f"arts-{tag}")
    session.add_all([topic, other_topic])
    await session.flush()
    robotics = Interests(name=f"Robotics {tag}", slug=f"robotics-{tag}", category_id=topic.id)
    drones = Interests(name=f"Drones {tag}", slug=f"drones-{tag}", category_id=topic.id)
    painting = Interests(name=f"Painting {tag}", slug=f"painting-{tag}", category_id=other_topic.id)
    workshop = EventCategories(name=f"Workshop {tag}")
    session.add_all([robotics, drones, painting, workshop])
    await session.flush()
    e = Events(
        slug=f"st-{tag}", name="Café Build Night", images=[], event_datetime=datetime.now(timezone.utc) + timedelta(days=3), duration=2.0,
        club_id=club.id, category_id=workshop.id, location_name="Lab 3, CUSAT", event_tag="Featured", lifecycle=EventStatus.published,
    )
    session.add(e)
    await session.flush()
    session.add(EventInterestsLink(event_id=e.id, interest_id=robotics.id))
    await session.commit()
    return dict(tag=tag, owner=owner, club=club, topic=topic, other_topic=other_topic, robotics=robotics, drones=drones,
                painting=painting, workshop=workshop, event=e, h=None)


# ─── What a row is found by ───────────────────────────────────────────────


async def test_an_event_is_found_by_everything_it_is_made_of(session, scene, client):
    tag = scene["tag"]
    got = await _text(session, "events", scene["event"].id)
    for part in ("cafe build night", "lab 3, cusat", "featured", f"kerala robotics guild {tag}", "kochi", f"workshop {tag}",
                 f"robotics {tag}", f"deep tech {tag}"):
        assert part in got, (part, got)
    assert "é" not in got and got == got.lower()
    await _no_drift(session)

    # Discover's search finds it by each of those, and not by what it is not.
    for q in ("cusat", "featured", "guild kochi", f"deep tech {tag}", "CAFÉ", "cafe night", f"robotics {tag}"):
        body = (await client.get(f"{API}/discover/events", params={"q": q, "radius_km": 0})).json()
        assert scene["event"].id in [i["id"] for i in body["items"]], q
    body = (await client.get(f"{API}/discover/events", params={"q": f"painting {tag}", "radius_km": 0})).json()
    assert body["items"] == []


async def test_a_club_and_an_account_are_found_by_theirs(session, scene, client, make_user, make_admin, mint_token):
    tag = scene["tag"]
    session.add(ClubInterestsLink(club_id=scene["club"].id, interest_id=scene["drones"].id))
    scene["club"].tagline = "Bots & Büros"
    await session.commit()
    got = await _text(session, "clubs", scene["club"].id)
    assert all(p in got for p in (f"kerala robotics guild {tag}", "bots & buros", "kochi", f"drones {tag}", f"deep tech {tag}")), got
    body = (await client.get(f"{API}/discover/clubs", params={"q": "buros", "radius_km": 0})).json()
    assert [c["id"] for c in body["items"]] == [scene["club"].id]

    person = await make_user(email=f"jose.alvarez.{tag}@example.com", username=f"jalvarez{tag}")
    person.full_name = "José Álvarez"
    await session.commit()
    assert await _text(session, "users", person.id) == f"jose alvarez ‖ jalvarez{tag} ‖ jose.alvarez.{tag}@example.com"
    admin = await make_admin(email=f"admin-{tag}@example.com")
    h = auth_header(await mint_token(admin))
    for term in ("jose alv", "ÁLVAREZ", f"jalvarez{tag}", f"alvarez.{tag}@exa"):
        rows = (await client.get(f"{API}/user/admin/list", params={"search": term}, headers=h)).json()["items"]
        assert person.id in [r["id"] for r in rows], term
    # A term is one substring of one field, as before: never across two.
    rows = (await client.get(f"{API}/user/admin/list", params={"search": f"alvarez jalvarez{tag}"}, headers=h)).json()["items"]
    assert person.id not in [r["id"] for r in rows]
    await _no_drift(session)


async def test_the_apps_lists_search_the_same_way_and_show_only_what_is_public(session, scene, client, make_user, mint_token, make_club):
    tag = scene["tag"]
    h = auth_header(await mint_token(await make_user(email=f"app-{tag}@example.com")))
    draft = Events(slug=f"sd-{tag}", name="Café Build Draft", images=[], event_datetime=datetime.now(timezone.utc) + timedelta(days=4), duration=1.0,
                   club_id=scene["club"].id, category_id=scene["workshop"].id, lifecycle=EventStatus.draft)
    _, hidden = await make_club(name=f"Unverified Builders {tag}", email=f"unverified-{tag}@example.com", is_verified=False)
    unlisted = Events(slug=f"su-{tag}", name="Café Build Hidden", images=[], event_datetime=datetime.now(timezone.utc) + timedelta(days=4), duration=1.0,
                      club_id=hidden.id, category_id=scene["workshop"].id, lifecycle=EventStatus.published)
    session.add_all([draft, unlisted])
    await session.commit()
    rows = (await client.get(f"{API}/events/list", params={"search": "cafe build", "limit": 50}, headers=h)).json()["items"]
    assert [r["id"] for r in rows] == [scene["event"].id]  # not the draft, not the unverified club's
    rows = (await client.get(f"{API}/events/list", params={"search": f"deep tech {tag}", "limit": 50}, headers=h)).json()["items"]
    assert [r["id"] for r in rows] == [scene["event"].id]
    clubs = (await client.get(f"{API}/clubs/list", params={"search": "guild kochi", "limit": 50}, headers=h)).json()["items"]
    assert [c["id"] for c in clubs] == [scene["club"].id]


# ─── Every write path ─────────────────────────────────────────────────────


async def test_every_write_path_keeps_it_right(session, scene, client, mint_token):
    e, club, tag = scene["event"], scene["club"], scene["tag"]

    # The ORM: name, place, tag, category.
    e.name, e.location_name, e.event_tag = "Rover Rally", "Hall B, Thrissur", "Free"
    e.category_id = (await session.scalar(select(EventCategories.id).where(EventCategories.id == scene["workshop"].id)))
    await session.commit()
    assert (await _text(session, "events", e.id)).startswith("rover rally hall b, thrissur free")
    await _no_drift(session)

    # Raw SQL, as an import or a hand fix writes.
    await session.execute(text("UPDATE events SET name = 'Señor Rover' WHERE id = :id"), {"id": e.id})
    await session.commit()
    assert (await _text(session, "events", e.id)).startswith("senor rover")
    await _no_drift(session)

    # Interest links: made, soft-deleted, hard-deleted, moved to another event.
    link = EventInterestsLink(event_id=e.id, interest_id=scene["painting"].id)
    session.add(link)
    await session.commit()
    assert f"painting {tag}" in await _text(session, "events", e.id) and f"arts {tag}" in await _text(session, "events", e.id)
    link.is_deleted = True
    await session.commit()
    assert f"painting {tag}" not in await _text(session, "events", e.id)
    await _no_drift(session)
    await session.execute(text("DELETE FROM event_interests_link WHERE event_id = :id AND interest_id = :i"), {"id": e.id, "i": scene["robotics"].id})
    await session.commit()
    assert f"robotics {tag}" not in await _text(session, "events", e.id)
    other = Events(slug=f"so-{tag}", name="Second Event", images=[], event_datetime=e.event_datetime, duration=1.0, club_id=club.id,
                   category_id=scene["workshop"].id, lifecycle=EventStatus.published)
    session.add(other)
    await session.flush()
    moved = EventInterestsLink(event_id=e.id, interest_id=scene["drones"].id)
    session.add(moved)
    await session.commit()
    await session.execute(text("UPDATE event_interests_link SET event_id = :o WHERE id = :l"), {"o": other.id, "l": moved.id})
    await session.commit()
    assert f"drones {tag}" in await _text(session, "events", other.id) and f"drones {tag}" not in await _text(session, "events", e.id)
    await _no_drift(session)

    # The club: renamed through the ORM, moved by raw SQL; every one of its events follows.
    club.name = f"Malabar Makers {tag}"
    await session.commit()
    await session.execute(text("UPDATE clubs SET location_name = 'Kozhikode' WHERE id = :id"), {"id": club.id})
    await session.commit()
    for ev in (e, other):
        got = await _text(session, "events", ev.id)
        assert f"malabar makers {tag} kozhikode" in got and "guild" not in got, got
    session.add(ClubInterestsLink(club_id=club.id, interest_id=scene["painting"].id))
    await session.commit()
    assert f"painting {tag}" in await _text(session, "clubs", club.id)
    await session.execute(text("UPDATE clubs_interests_link SET is_deleted = true WHERE club_id = :id"), {"id": club.id})
    await session.commit()
    assert f"painting {tag}" not in await _text(session, "clubs", club.id)
    await _no_drift(session)

    # The dashboard's create and edit.
    h = auth_header(await mint_token(scene["owner"]))
    form = {"name": "Dashboard Démo", "event_datetime": (datetime.now(timezone.utc) + timedelta(days=9)).isoformat(), "duration": "2",
            "has_fee": "false", "status": "published", "category_id": str(scene["workshop"].id), "interest_ids": str(scene["robotics"].id),
            "location_name": "Seminar Hall"}
    made = await client.post(f"{API}/events/create", data=form, headers=h)
    assert made.status_code == 200, made.text
    got = await _text(session, "events", made.json()["id"])
    assert got.startswith("dashboard demo seminar hall") and f"robotics {tag}" in got, got
    edited = await client.put(f"{API}/events/update/{made.json()['id']}", data={**form, "name": "Dashboard Edit", "interest_ids": str(scene["painting"].id)}, headers=h)
    assert edited.status_code == 200, edited.text
    got = await _text(session, "events", made.json()["id"])
    assert got.startswith("dashboard edit") and f"painting {tag}" in got and f"robotics {tag}" not in got, got
    await _no_drift(session)


async def test_a_renamed_category_interest_or_topic_is_put_right_by_the_drain(session, scene, monkeypatch):
    e, tag = scene["event"], scene["tag"]
    monkeypatch.setattr(sync, "TAXONOMY_BATCH", 1)  # a batch a row: the batches are what is tested
    await session.execute(text("DELETE FROM search_outbox"))
    for i in range(3):
        session.add(Events(slug=f"sx{i}-{tag}", name=f"Extra {i}", images=[], event_datetime=e.event_datetime, duration=1.0, club_id=scene["club"].id,
                           category_id=scene["workshop"].id, lifecycle=EventStatus.published))
    await session.commit()

    steps = [
        (scene["workshop"], "name", f"Atelier {tag}", "events", f"atelier {tag}"),
        (scene["robotics"], "name", f"Robotique {tag}", "events", f"robotique {tag}"),
        (scene["topic"], "name", f"Frontier Tech {tag}", "events", f"frontier tech {tag}"),
        (scene["robotics"], "category_id", scene["other_topic"].id, "events", f"arts {tag}"),
    ]
    for row, attr, value, table, want in steps:
        setattr(row, attr, value)
        await session.commit()
        assert e.id in (await stext.drift(session))[table]  # not yet: a lakh of rows is the drain's work
        out = await sync.drain(session)
        assert out >= 1
        assert want in await _text(session, table, e.id)
        await _no_drift(session)

    # Deleted: its name leaves every text it was in.
    scene["robotics"].is_deleted = True
    await session.commit()
    await sync.drain(session)
    assert f"robotique {tag}" not in await _text(session, "events", e.id)
    await _no_drift(session)


# ─── The query's side ─────────────────────────────────────────────────────


@pytest.mark.parametrize("q", ["Robotics", "  robotics   kochi ", "AI\tSummit", "B'lore Hack-Night", "C++ & Go!", "MiXeD CaSe 2026"])
async def test_ascii_is_normalised_here_exactly_as_the_database_does(session, q):
    assert stext.norm_ascii(q) == (await session.scalar(text("SELECT coalesce(moa_search_norm(:q), '')"), {"q": q}))


@pytest.mark.parametrize("q,want", [("Café", "cafe"), ("ÉCOLE  Été", "ecole ete"), ("Straße", "strasse"), ("Ægir Øst", "aegir ost"), ("Thiruvananthapuram", "thiruvananthapuram")])
async def test_anything_else_is_normalised_by_the_database(session, q, want):
    assert await stext.normalise(session, q) == want


# ─── Drift, repair and lock-safe backfills ────────────────────────────────


async def test_drift_is_found_and_put_right(session, scene):
    await session.execute(text("UPDATE events SET search_text = 'wrong' WHERE id = :id"), {"id": scene["event"].id})
    await session.execute(text("UPDATE clubs SET search_text = NULL WHERE id = :id"), {"id": scene["club"].id})
    found = await stext.drift(session)
    assert found["events"] == [scene["event"].id] and found["clubs"] == [scene["club"].id]
    assert await stext.recompute(session) == {"events": 1, "clubs": 1, "users": 0}
    await _no_drift(session)


@pytest.fixture
async def committed_users(_engine):
    """Accounts committed for real (a backfill's concurrency cannot be staged
    inside one test transaction), removed afterwards."""
    tag = uuid.uuid4().hex[:6]
    async with AsyncSession(_engine) as s:
        ids = list((await s.execute(text(
            "INSERT INTO users (full_name, username, email, user_type, token_version, is_active, notify_new_device, admin_role, totp_enabled, "
            "created_at, updated_at, is_deleted) SELECT 'Backfill ' || g, :tag || g, :tag || g || '@example.com', 'app_user', 0, true, true, 'owner', false, "
            "now(), now(), false FROM generate_series(1, 7) g RETURNING id"
        ), {"tag": f"bf{tag}"})).scalars().all())
        await s.execute(text("UPDATE users SET search_text = NULL WHERE id = ANY(:ids)"), {"ids": ids})
        await s.commit()
    yield sorted(ids)
    async with AsyncSession(_engine) as s:
        await s.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": ids})
        await s.commit()


async def test_the_backfill_skips_a_row_someone_holds_and_commits_as_it_goes(_engine, committed_users):
    ids = committed_users
    async with _engine.connect() as holder:
        await holder.begin()
        await holder.execute(text("SELECT id FROM users WHERE id = :id FOR UPDATE"), {"id": ids[3]})
        async with AsyncSession(_engine) as s:
            commits = []
            real_commit = s.commit

            async def counted():
                commits.append(1)
                await real_commit()

            s.commit = counted
            # Never waits on the held row: done well inside the timeout.
            got = await asyncio.wait_for(stext.backfill(s, "users", batch=2, passes=2), timeout=10)
        assert got == {"written": 6, "left": 1}
        assert len(commits) >= 8  # a statement a batch, each committed: never one long transaction
        # The rows done so far are visible to anyone, while the backfill's work is over and the holder still waits.
        done = (await holder.execute(text("SELECT count(*) FROM users WHERE id = ANY(:ids) AND search_text IS NOT NULL"), {"ids": ids})).scalar()
        assert done == 6
        await holder.rollback()
    async with AsyncSession(_engine) as s:
        assert await stext.backfill(s, "users", batch=2) == {"written": 1, "left": 0}
        assert await s.scalar(text("SELECT count(*) FROM search_outbox")) == 0  # filling a column is no change to a document


def _migration():
    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / "w2_search.py"
    spec = importlib.util.spec_from_file_location("w2_search_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_the_migrations_backfill_is_lock_safe_too(_engine, committed_users, monkeypatch):
    """w2_search's own backfill: batches by id range that commit, rows held
    by another writer skipped and tried again after a pause, a 3 s lock
    timeout on each statement, no outbox rows."""
    migration = _migration()
    monkeypatch.setattr(migration, "BACKFILL_BATCH", 2)
    pauses = []
    monkeypatch.setattr(migration.time, "sleep", lambda s: pauses.append(s))
    monkeypatch.setattr(migration, "BACKFILL", tuple(b for b in migration.BACKFILL if b[0] == "users"))
    ids = committed_users
    statements = []
    async with _engine.connect() as holder:
        await holder.begin()
        await holder.execute(text("SELECT id FROM users WHERE id = :id FOR UPDATE"), {"id": ids[0]})

        def run(sync_conn):
            from sqlalchemy import event

            sync_conn = sync_conn.execution_options(isolation_level="AUTOCOMMIT")
            event.listen(sync_conn, "before_cursor_execute", lambda c, cur, stmt, *a: statements.append(stmt))
            migration._backfill(sync_conn)

        async with _engine.connect() as conn:
            await asyncio.wait_for(conn.run_sync(run), timeout=20)
        await holder.rollback()
    assert len(pauses) == migration.BACKFILL_PASSES  # the held row, tried again each pass
    async with AsyncSession(_engine) as s:
        assert await s.scalar(text("SELECT count(*) FROM users WHERE id = ANY(:ids) AND search_text IS NULL"), {"ids": ids}) == 1
        assert await s.scalar(text("SELECT count(*) FROM search_outbox")) == 0
    assert statements.count("COMMIT") >= len(ids) // 2 and any("lock_timeout = '3s'" in s for s in statements)
    assert all("FOR UPDATE SKIP LOCKED" in s for s in statements if s.startswith("UPDATE"))
