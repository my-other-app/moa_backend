"""An admin changes a club's or an event's slug; the old one keeps working
(app/api/slugs, feature-register/mail-and-slugs-contract.md Part S1).

Printed QR codes, shared links and a club's website subdomain all carry the
slug, so an old one must answer forever: every lookup falls back to the
alias and answers with the canonical row, whose slug is the new one.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.slugs import service as slugs
from app.api.slugs.models import SlugAliases
from app.api.users.models import AdminRoles
from tests.conftest import auth_header

API = "/api/v1"
NOW = datetime.now(timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, make_admin, mint_token):
    owner, club = await make_club(name="GDG Cloud Trivandrum", email="gdg@example.com")
    club.about = "Cloud people in Trivandrum."
    event = await make_event(club=club, created_by=owner, name="DevFest Kochi", event_datetime=NOW + timedelta(days=9),
                             reg_startdate=NOW - timedelta(days=1), reg_enddate=NOW + timedelta(days=8))
    _, other = await make_club(name="Circuit Society", email="circuit@example.com")
    other_event = await make_event(club=other, created_by=owner, name="Robo Sumo", event_datetime=NOW + timedelta(days=5))
    support = await make_admin(email="support-slugs@example.com", role=AdminRoles.support)
    viewer = await make_admin(email="viewer-slugs@example.com", role=AdminRoles.viewer)
    owner_admin = await make_admin(email="owner-slugs@example.com", role=AdminRoles.owner)
    await session.commit()
    return dict(
        owner=owner, club=club, event=event, other=other, other_event=other_event,
        club_slug=club.slug, event_slug=event.slug,
        support=auth_header(await mint_token(support)), viewer=auth_header(await mint_token(viewer)),
        owner_admin=auth_header(await mint_token(owner_admin)), club_h=auth_header(await mint_token(owner)),
    )


def _club(w, slug: str, who: str = "support"):
    return f"{API}/admin/clubs/{w['club'].id}/slug", {"json": {"slug": slug}, "headers": w[who]}


def _event(w, slug: str, who: str = "support"):
    return f"{API}/admin/events/{w['event'].id}/slug", {"json": {"slug": slug}, "headers": w[who]}


# ─── Changing one ────────────────────────────────────────────────────────


async def test_support_changes_a_club_slug_and_the_old_one_becomes_an_alias(client, session, world):
    url, kw = _club(world, "  GDG-Cloud-TVM ")
    r = await client.patch(url, **kw)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "slug": "gdg-cloud-tvm",
        "aliases": [world["club_slug"]],
        "url": "https://myotherapp.in/clubs/gdg-cloud-tvm",
        "site_url": "https://gdg-cloud-tvm.myotherapp.in",
    }
    await session.refresh(world["club"])
    assert world["club"].slug == "gdg-cloud-tvm"
    row = await session.scalar(select(SlugAliases).where(SlugAliases.slug == world["club_slug"]))
    assert (row.kind, row.target_id, row.created_by_user_id is not None) == ("club", world["club"].id, True)

    logged = (await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "club.slug_change"))).all()
    assert len(logged) == 1 and logged[0].entity_id == str(world["club"].id)
    assert world["club_slug"] in logged[0].summary and "gdg-cloud-tvm" in logged[0].summary


async def test_an_event_slug_changes_the_same_way(client, session, world):
    url, kw = _event(world, "devfest-kochi-2026", who="owner_admin")
    r = await client.patch(url, **kw)
    assert r.status_code == 200, r.text
    assert r.json() == {"slug": "devfest-kochi-2026", "aliases": [world["event_slug"]],
                        "url": "https://myotherapp.in/events/devfest-kochi-2026", "site_url": None}
    assert (await session.scalar(select(AdminAuditLogs.action).where(AdminAuditLogs.entity_type == "event"))) == "event.slug_change"


async def test_a_viewer_and_a_club_cannot_change_a_slug(client, world):
    for who in ("viewer", "club_h"):
        url, kw = _club(world, "gdg-cloud-tvm", who=who)
        r = await client.patch(url, **kw)
        assert r.status_code in (401, 403), (who, r.text)
        url, kw = _event(world, "devfest-kochi-2026", who=who)
        assert (await client.patch(url, **kw)).status_code in (401, 403)
    url, kw = _club(world, "gdg-cloud-tvm", who="viewer")
    assert (await client.patch(url, **kw)).json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"


async def test_bad_reserved_and_taken_slugs_are_refused(client, session, world):
    for bad in ("ab", "-gdg", "gdg-", "gdg--tvm", "gdg_tvm", "gdg tvm", "gdg.tvm", "x" * 61, "2026", "", "ഗൂഗിൾ"):
        url, kw = _club(world, bad)
        r = await client.patch(url, **kw)
        assert r.status_code == 422 and r.json()["error_code"] == "SLUG_INVALID", (bad, r.text)
    for reserved in ("www", "api", "club-dashboard", "main-portal", "sites", "mail", "Admin"):
        url, kw = _club(world, reserved)
        r = await client.patch(url, **kw)
        assert r.status_code == 422 and r.json()["error_code"] == "SLUG_RESERVED", (reserved, r.text)
    # An event may be called what a club subdomain may not.
    url, kw = _event(world, "events")
    assert (await client.patch(url, **kw)).status_code == 200

    # Another club's live slug, and another club's old one.
    url, kw = _club(world, world["other"].slug)
    r = await client.patch(url, **kw)
    assert r.status_code == 409 and r.json()["error_code"] == "SLUG_TAKEN"
    other_old = world["other"].slug
    r = await client.patch(f"{API}/admin/clubs/{world['other'].id}/slug", json={"slug": "circuit"}, headers=world["support"])
    assert r.status_code == 200, r.text
    url, kw = _club(world, other_old)
    r = await client.patch(url, **kw)
    assert r.status_code == 409 and r.json()["error_code"] == "SLUG_TAKEN"
    # Slugs are per kind: an event may take a club's.
    url, kw = _event(world, "circuit")
    assert (await client.patch(url, **kw)).status_code == 200
    # Another event's slug is taken.
    url, kw = _event(world, world["other_event"].slug)
    assert (await client.patch(url, **kw)).json()["error_code"] == "SLUG_TAKEN"
    # Nothing was written by the refusals.
    assert world["club_slug"] == (await client.get(f"{API}/clubs/admin/{world['club'].id}", headers=world["support"])).json()["slug"]


async def test_changing_back_takes_the_old_slug_back(client, session, world):
    for slug in ("gdg-tvm", "gdg-cloud-tvm"):
        url, kw = _club(world, slug)
        assert (await client.patch(url, **kw)).status_code == 200
    url, kw = _club(world, "gdg-tvm")
    r = await client.patch(url, **kw)
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "gdg-tvm"
    assert r.json()["aliases"] == ["gdg-cloud-tvm", world["club_slug"]]
    # The same slug again changes nothing.
    url, kw = _club(world, "gdg-tvm")
    again = await client.patch(url, **kw)
    assert again.status_code == 200 and again.json() == r.json()
    assert len((await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "club.slug_change"))).all()) == 3


async def test_no_such_or_a_deleted_club_or_event_is_a_404(client, session, world):
    r = await client.patch(f"{API}/admin/clubs/999999/slug", json={"slug": "nobody-here"}, headers=world["support"])
    assert r.status_code == 404
    r = await client.patch(f"{API}/admin/events/999999/slug", json={"slug": "nobody-here"}, headers=world["support"])
    assert r.status_code == 404
    world["other_event"].soft_delete()
    await session.commit()
    r = await client.patch(f"{API}/admin/events/{world['other_event'].id}/slug", json={"slug": "gone-event"}, headers=world["support"])
    assert r.status_code == 404


async def test_the_admin_gets_list_the_old_slugs(client, session, world):
    url, kw = _club(world, "gdg-cloud-tvm")
    await client.patch(url, **kw)
    url, kw = _event(world, "devfest-kochi-2026")
    await client.patch(url, **kw)

    club = (await client.get(f"{API}/clubs/admin/{world['club'].id}", headers=world["viewer"])).json()
    assert club["slug"] == "gdg-cloud-tvm" and club["slug_aliases"] == [world["club_slug"]]
    event = (await client.get(f"{API}/events/info/{world['event'].id}", headers=world["viewer"])).json()
    assert event["slug"] == "devfest-kochi-2026" and event["slug_aliases"] == [world["event_slug"]]
    # Anyone else is not told.
    assert (await client.get(f"{API}/events/info/{world['event'].id}")).json()["slug_aliases"] is None


# ─── Old slugs keep answering ─────────────────────────────────────────────


async def test_an_old_event_slug_opens_the_event_everywhere(client, session, world):
    url, kw = _event(world, "devfest-kochi-2026")
    assert (await client.patch(url, **kw)).status_code == 200
    old = world["event_slug"]

    page = await client.get(f"{API}/discover/events/{old}")
    assert page.status_code == 200, page.text
    assert page.json()["event"]["id"] == world["event"].id and page.json()["event"]["slug"] == "devfest-kochi-2026"
    assert (await client.get(f"{API}/discover/events/devfest-kochi-2026")).json()["event"]["id"] == world["event"].id

    info = await client.get(f"{API}/events/info/{old}")
    assert info.status_code == 200 and info.json()["slug"] == "devfest-kochi-2026"
    assert (await client.get(f"{API}/events/count-view/{old}")).status_code == 200

    from app.api.events.registration.service import resolve_event

    assert (await resolve_event(session, old)).id == world["event"].id
    assert await slugs.resolve_event_slug(session, old) == world["event"].id
    assert await slugs.resolve_event_slug(session, "devfest-kochi-2026") == world["event"].id
    assert await slugs.resolve_event_slug(session, "never-was") is None

    # Registering through a QR code printed with the old slug.
    r = await client.post(f"{API}/events/registration/{old}/register",
                          json={"full_name": "Asha Nair", "email": "asha@example.com", "phone": "9123456780"})
    assert r.status_code == 200, r.text

    # A slug nobody ever had is still a 404.
    assert (await client.get(f"{API}/discover/events/never-was")).status_code == 404
    assert (await client.get(f"{API}/events/info/never-was")).status_code == 404


async def test_an_old_club_slug_opens_the_club_page(client, session, world):
    url, kw = _club(world, "gdg-cloud-tvm")
    assert (await client.patch(url, **kw)).status_code == 200
    old = world["club_slug"]

    page = await client.get(f"{API}/discover/clubs/{old}")
    assert page.status_code == 200, page.text
    assert page.json()["club"]["id"] == world["club"].id and page.json()["club"]["slug"] == "gdg-cloud-tvm"
    events = await client.get(f"{API}/discover/clubs/{old}/events")
    assert events.status_code == 200 and [e["id"] for e in events.json()["items"]] == [world["event"].id]
    assert await slugs.resolve_club_slug(session, old) == world["club"].id
    assert (await client.get(f"{API}/discover/clubs/never-was")).status_code == 404


async def test_the_club_site_answers_at_the_old_slug_and_names_the_new_one(client, session, world):
    assert (await client.post(f"{API}/clubs/site/publish", headers=world["club_h"])).status_code == 200
    url, kw = _club(world, "gdg-cloud-tvm")
    assert (await client.patch(url, **kw)).status_code == 200
    old = world["club_slug"]

    # The page was made again: its own links carry the new slug.
    page = await client.get(f"{API}/sites/gdg-cloud-tvm")
    assert page.status_code == 200 and page.headers["x-site-slug"] == "gdg-cloud-tvm"
    assert "/sites/gdg-cloud-tvm/visit" in page.text and f"/sites/{old}/visit" not in page.text

    # The old slug: the same page, and the header the Worker redirects on.
    by_old = await client.get(f"{API}/sites/{old}")
    assert by_old.status_code == 200 and by_old.headers["x-site-slug"] == "gdg-cloud-tvm" and by_old.text == page.text

    # The Worker's resolver: the old subdomain belongs to the club, at its new subdomain.
    r = await client.get(f"{API}/sites/resolve", params={"host": f"{old}.myotherapp.in"})
    assert r.status_code == 200, r.text
    assert r.json() == {"club_slug": "gdg-cloud-tvm", "status": "active", "canonical_host": "gdg-cloud-tvm.myotherapp.in"}
    r = await client.get(f"{API}/sites/resolve", params={"host": "gdg-cloud-tvm.myotherapp.in"})
    assert r.json() == {"club_slug": "gdg-cloud-tvm", "status": "active", "canonical_host": "gdg-cloud-tvm.myotherapp.in"}

    # A visit counted through a page still carrying the old slug.
    from sqlalchemy import func

    from app.api.links.models import TrackingClicks

    before = await session.scalar(select(func.count()).select_from(TrackingClicks))
    r = await client.post(f"{API}/sites/{old}/visit", content="{}", headers={"content-type": "text/plain", "user-agent": "Mozilla/5.0 Chrome/131", "x-forwarded-for": "49.36.1.1"})
    assert r.status_code == 204
    assert await session.scalar(select(func.count()).select_from(TrackingClicks)) == before + 1


async def test_an_event_slug_change_remakes_the_hosts_site(client, session, world):
    assert (await client.post(f"{API}/clubs/site/publish", headers=world["club_h"])).status_code == 200
    url, kw = _event(world, "devfest-kochi-2026")
    assert (await client.patch(url, **kw)).status_code == 200
    page = await client.get(f"{API}/sites/{world['club'].slug}")
    assert "/events/devfest-kochi-2026" in page.text and f"/events/{world['event_slug']}" not in page.text


async def test_a_lookup_is_still_one_statement(session, world):
    """The fallback rides in the lookup's own WHERE: no second round trip."""
    from sqlalchemy import event as sa_event

    from app.api.sites.service import published_page

    world["club"].slug, old = "gdg-cloud-tvm", world["club"].slug
    session.add(SlugAliases(kind="club", slug=old, target_id=world["club"].id))
    await session.commit()
    statements = []
    engine = session.bind.engine.sync_engine if hasattr(session.bind, "engine") else session.bind.sync_engine

    def count(conn, cursor, statement, *args):
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", count)
    try:
        assert await slugs.resolve_club_slug(session, old) == world["club"].id
        assert await published_page(session, old) is None  # not published: nothing, still one statement
    finally:
        sa_event.remove(engine, "before_cursor_execute", count)
    assert len([s for s in statements if s.lstrip().upper().startswith("SELECT")]) == 2


# ─── The rule, without a database (last: see tests/conftest.py) ───────────


def test_check_lower_cases_and_keeps_the_rule():
    assert slugs.check("event", " DevFest-2026 ") == "devfest-2026"
    assert slugs.check("club", "a1b") == "a1b"
    assert slugs.check("club", "x" * 60) == "x" * 60
    assert slugs.check("event", "www") == "www"
    with pytest.raises(Exception):
        slugs.check("club", None)
