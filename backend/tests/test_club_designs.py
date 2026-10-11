"""The club's own designs: what every event starts from, designed in the
same editor an event uses, and the pictures it uploads usable by the events
forked from it."""
import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from sqlalchemy import select

from app.api.clubs.models import ClubUsersLink
from app.api.ticket_templates.models import TicketTemplates
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from app.core.storage import objects
from tests.conftest import auth_header


@pytest.fixture
def store(monkeypatch):
    held: dict[str, bytes] = {}
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, bytes(data)))
    monkeypatch.setattr(objects, "get_bytes", lambda key: held.get(key))
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://files.test/{key}")
    return held


@pytest.fixture
async def designing(session, make_club, make_event, make_admin, make_user, mint_token, store):
    owner, club = await make_club(name="Yukthi", email="club-designs@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=9))
    for i in range(3):
        fan = await make_user(email=f"fan{i}-cd@example.com")
        session.add(ClubUsersLink(club_id=club.id, user_id=fan.id, is_following=True))
    admin = await make_admin(email="admin-cd@example.com")
    await session.commit()
    return {"owner": owner, "club": club, "event": event, "h": auth_header(await mint_token(owner)), "admin": auth_header(await mint_token(admin)), "store": store}


def _body(name="classic"):
    st = ticket_starter(name)
    return dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"]))


async def test_the_club_opens_on_a_starter_and_saving_makes_it_the_default(client, session, designing):
    h, club, event = designing["h"], designing["club"], designing["event"]
    opened = await client.get("/api/v1/clubs/designs/ticket/layout", headers=h)
    assert opened.status_code == 200, opened.text
    d = opened.json()
    assert d["fromStarter"] is True and d["isOwn"] is False and d["eventId"] == 0 and d["kind"] == "ticket"
    assert d["brand"]["orgName"] == "Yukthi" and d["sampleValues"]["event_name"] == "Sample event" and d["qrRequired"] is True

    saved = await client.put("/api/v1/clubs/designs/ticket/layout", json=_body(), headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["isOwn"] is True and saved.json()["version"] == 1 and saved.json()["fromStarter"] is False
    rows = list((await session.scalars(select(TicketTemplates).where(TicketTemplates.club_id == club.id, TicketTemplates.is_deleted == False))).all())  # noqa: E712
    assert len(rows) == 1 and rows[0].event_id is None and rows[0].is_default is True and rows[0].config["layout"]["version"] == 1

    # Saving again edits the same row; the version climbs.
    again = await client.put("/api/v1/clubs/designs/ticket/layout", json=_body("minimal"), headers=h)
    assert again.json()["version"] == 2
    rows = list((await session.scalars(select(TicketTemplates).where(TicketTemplates.club_id == club.id, TicketTemplates.is_deleted == False))).all())  # noqa: E712
    assert len(rows) == 1

    # An event with no design of its own now opens on the club's.
    on_event = await client.get(f"/api/v1/events/{event.id}/designs/ticket/layout", headers=h)
    assert on_event.status_code == 200
    assert on_event.json()["fromStarter"] is False and on_event.json()["isOwn"] is False and on_event.json()["version"] == 2


async def test_an_events_fork_may_keep_the_clubs_pictures(client, designing):
    h, club, event = designing["h"], designing["club"], designing["event"]
    body = _body()
    body["canvas"]["templateKey"] = f"{club.id}-not-ours/template.png"
    refused = await client.put("/api/v1/clubs/designs/ticket/layout", json=body, headers=h)
    assert refused.status_code == 422 and "canvas.templateKey" in refused.json().get("errors", {}).get("fields", {})

    uploaded = await client.post(
        "/api/v1/clubs/designs/ticket/layout/images?purpose=template", files={"file": ("art.png", _png(), "image/png")}, headers=h
    )
    assert uploaded.status_code == 200, uploaded.text
    key = uploaded.json()["key"]
    assert f"/clubs/{club.id}/layout/template-" in key and key in designing["store"]

    body["canvas"]["templateKey"] = key
    assert (await client.put("/api/v1/clubs/designs/ticket/layout", json=body, headers=h)).status_code == 200
    # The event reads the club's design, picture included, and may save it as its own.
    on_event = (await client.get(f"/api/v1/events/{event.id}/designs/ticket/layout", headers=h)).json()
    assert on_event["canvas"]["templateKey"] == key and key in on_event["imageUrls"]
    forked = await client.put(f"/api/v1/events/{event.id}/designs/ticket/layout", json={"canvas": on_event["canvas"], "layers": on_event["layers"], "page": on_event["page"]}, headers=h)
    assert forked.status_code == 200, forked.text
    assert forked.json()["isOwn"] is True


async def test_starters_and_the_preview_render_for_the_club(client, designing):
    h = designing["h"]
    starters = (await client.get("/api/v1/clubs/designs/certificate/layout", headers=h)).json()["starters"]
    name = next(iter(starters))
    s = await client.get(f"/api/v1/clubs/designs/certificate/layout/starters/{name}", headers=h)
    assert s.status_code == 200 and s.json()["layers"] and s.json()["page"]
    assert (await client.get("/api/v1/clubs/designs/certificate/layout/starters/nope", headers=h)).status_code == 404

    png = await client.post("/api/v1/clubs/designs/ticket/layout/preview", json=_body(), headers=h)
    assert png.status_code == 200, png.text
    assert png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"


async def test_every_kind_has_its_own_default_and_resetting_drops_it(client, designing):
    h = designing["h"]
    for kind in ("ticket", "badge", "certificate", "agenda"):
        starters = (await client.get(f"/api/v1/clubs/designs/{kind}/layout", headers=h)).json()["starters"]
        body = (await client.get(f"/api/v1/clubs/designs/{kind}/layout/starters/{next(iter(starters))}", headers=h)).json()
        saved = await client.put(f"/api/v1/clubs/designs/{kind}/layout", json=body, headers=h)
        assert saved.status_code == 200, (kind, saved.text)
        assert saved.json()["isOwn"] is True and saved.json()["kind"] == kind
    assert (await client.delete("/api/v1/clubs/designs/badge/layout", headers=h)).status_code == 200
    assert (await client.get("/api/v1/clubs/designs/badge/layout", headers=h)).json()["isOwn"] is False
    assert (await client.get("/api/v1/clubs/designs/ticket/layout", headers=h)).json()["isOwn"] is True
    assert (await client.delete("/api/v1/clubs/designs/badge/layout", headers=h)).status_code == 404


async def test_only_a_club_has_designs_of_its_own(client, designing):
    assert (await client.get("/api/v1/clubs/designs/ticket/layout", headers=designing["admin"])).status_code == 404
    assert (await client.get("/api/v1/clubs/designs/ticket/layout")).status_code == 401


async def test_the_followers_page_knows_how_many_there_are(client, designing):
    r = await client.get(f"/api/v1/clubs/followers/{designing['club'].id}?limit=2&offset=0", headers=designing["h"])
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 3 and len(r.json()["items"]) == 2


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (40, 20), (20, 120, 100)).save(out, "PNG")
    return out.getvalue()
