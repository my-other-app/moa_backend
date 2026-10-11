"""The run of show: blocks the organiser keeps, anyone may read, and the programme it prints as."""
from datetime import datetime, timezone

import pytest

from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import agenda_starter
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 3, 30, tzinfo=timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="agenda@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Day", event_datetime=START, duration=8.0, location_name="Innovation Hall")
    other_owner, other = await make_club(name="Other", email="other-agenda@example.com")
    return {"event": event, "club": auth_header(await mint_token(owner)), "other": auth_header(await mint_token(other_owner))}


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/agenda{path}"


async def test_blocks_order_by_start_and_overlapping_ones_read_as_parallel(client, world):
    h = world["club"]
    keynote = await client.post(_url(world), json={"starts_at": "2026-11-20T04:00:00Z", "ends_at": "2026-11-20T05:00:00Z", "title": "Opening keynote", "speaker": "Ananya Iyer", "location": "Main hall"}, headers=h)
    assert keynote.status_code == 201, keynote.text
    assert keynote.json()["parallel"] is False and keynote.json()["sort_order"] == 0
    reg = await client.post(_url(world), json={"starts_at": "2026-11-20T03:30:00Z", "ends_at": "2026-11-20T04:00:00Z", "title": "  Registration ", "location": "Foyer", "speaker": ""}, headers=h)
    assert reg.status_code == 201 and reg.json()["title"] == "Registration" and reg.json()["speaker"] is None
    workshop = await client.post(_url(world), json={"starts_at": "2026-11-20T04:30:00Z", "ends_at": "2026-11-20T05:30:00Z", "title": "Workshop", "location": "Studio 2"}, headers=h)
    assert workshop.status_code == 201

    # Anyone may read it; the other club may not write it.
    public = await client.get(_url(world))
    assert public.status_code == 200, public.text
    body = public.json()
    assert [i["title"] for i in body["items"]] == ["Registration", "Opening keynote", "Workshop"]
    assert [i["parallel"] for i in body["items"]] == [False, True, True]
    assert body["timezone"] == "Asia/Kolkata" and body["has_programme"] is False
    assert (await client.post(_url(world), json={"starts_at": "2026-11-20T06:00:00Z", "title": "Nope"}, headers=world["other"])).status_code in (403, 404)
    assert (await client.post(_url(world), json={"starts_at": "2026-11-20T06:00:00Z", "ends_at": "2026-11-20T05:00:00Z", "title": "Backwards"}, headers=h)).status_code == 422

    # A change re-sorts; a removal is quiet.
    moved = await client.patch(_url(world, f"/{workshop.json()['id']}"), json={"starts_at": "2026-11-20T06:00:00Z", "ends_at": None}, headers=h)
    assert moved.status_code == 200 and moved.json()["parallel"] is False and moved.json()["ends_at"] is None
    assert (await client.patch(_url(world, f"/{workshop.json()['id']}"), json={"ends_at": "2026-11-20T05:00:00Z"}, headers=h)).status_code == 422
    assert (await client.delete(_url(world, f"/{reg.json()['id']}"), headers=h)).status_code == 200
    assert [i["title"] for i in (await client.get(_url(world))).json()["items"]] == ["Opening keynote", "Workshop"]
    assert (await client.get("/api/v1/events/999999/agenda")).status_code == 404


async def test_the_programme_is_designed_like_a_card_and_prints_on_a4_with_the_real_sessions(client, world):
    h = world["club"]
    eid = world["event"].id
    layout = await client.get(f"/api/v1/events/{eid}/designs/agenda/layout", headers=h)
    assert layout.status_code == 200, layout.text
    body = layout.json()
    assert body["kind"] == "agenda" and body["qrRequired"] is False and body["isOwn"] is False
    assert set(body["starters"]) == {"screen", "screen_light", "portrait", "programme"}
    assert body["page"] == {"widthMm": 332, "heightMm": 186.75}
    # The stage draws the sample programme until the event has sessions of its own, on the event's day.
    assert len(body["agenda"]) == 6 and body["agenda"][0]["startsAt"].startswith("2026-11-20T09:00:00+05:30")
    assert any(l["kind"] == "agenda" for l in body["layers"])

    # The screen design, fitted onto A4 landscape; asked for portrait, it goes there.
    pdf = await client.get(_url(world, "/programme.pdf"), headers=h)
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"].startswith("application/pdf") and pdf.content[:4] == b"%PDF"
    assert (await client.get(_url(world, "/programme.pdf"), params={"page": "a4_portrait"}, headers=h)).status_code == 200

    # Saving the printed-programme starter: the design is the event's own, and the agenda says so.
    st = agenda_starter("programme")
    saved = await client.put(f"/api/v1/events/{eid}/designs/agenda/layout", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["isOwn"] is True and saved.json()["page"] == {"widthMm": 210, "heightMm": 297} and len(saved.json()["agenda"]) == 6
    assert (await client.get(_url(world))).json()["has_programme"] is True

    # Real sessions replace the samples everywhere the programme is drawn.
    assert (await client.post(_url(world), json={"starts_at": "2026-11-20T04:00:00Z", "ends_at": "2026-11-20T05:00:00Z", "title": "Only session", "speaker": "Sita Raman"}, headers=h)).status_code == 201
    again = await client.get(f"/api/v1/events/{eid}/designs/agenda/layout", headers=h)
    assert [a["title"] for a in again.json()["agenda"]] == ["Only session"]
    png = await client.post(f"/api/v1/events/{eid}/designs/agenda/layout/preview", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=h)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    # Two agenda layers in one design are refused.
    twice = {**dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"]))}
    twice["layers"] = twice["layers"] + [{**next(l for l in twice["layers"] if l["kind"] == "agenda"), "id": "agenda2"}]
    assert (await client.put(f"/api/v1/events/{eid}/designs/agenda/layout", json=twice, headers=h)).status_code == 422


async def test_a_kiosk_sees_what_is_still_to_come(client, world):
    h = world["club"]
    eid = world["event"].id
    for hour, title in ((3, "Doors"), (4, "Keynote"), (6, "Lunch")):
        assert (await client.post(_url(world), json={"starts_at": f"2026-11-20T0{hour}:30:00Z", "ends_at": f"2026-11-20T0{hour + 1}:00:00Z", "title": title}, headers=h)).status_code == 201
    paired = await client.post(f"/api/v1/events/{eid}/kiosks", json={"name": "Lobby", "shift_hours": 8}, headers=h)
    assert paired.status_code == 200, paired.text
    ctx = await client.get("/api/v1/kiosk/context", headers={"X-Kiosk-Token": paired.json()["token"]})
    assert ctx.status_code == 200, ctx.text
    # Before the day, everything is still to come; the programme is not saved yet.
    assert [a["title"] for a in ctx.json()["agenda"]] == ["Doors", "Keynote", "Lunch"] and ctx.json()["has_programme"] is False
