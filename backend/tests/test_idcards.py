"""ID cards: designed per event, printed per guest, the rule for a second one."""
import uuid
from datetime import datetime, timezone

import pytest

from app.api.events.volunteer.models import Volunteer
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import badge_starter
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="cards@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0, location_name="Innovation Hall")
    a = await make_user(email="ananya-i@example.com")
    s = await make_user(email="sita-i@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer")
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", role="speaker")
    vol_user = await make_user(email="vani-i@example.com")
    session.add(Volunteer(email=vol_user.email, event_id=event.id, user_id=vol_user.id, is_approved=True))
    await session.commit()
    return {"event": event, "a": r_a, "s": r_s, "club": auth_header(await mint_token(owner)), "vol": auth_header(await mint_token(vol_user))}


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/idcards{path}"


async def test_the_card_design_is_edited_like_a_ticket_and_starts_from_the_conference_card(client, world):
    h = world["club"]
    layout = await client.get(f"/api/v1/events/{world['event'].id}/designs/badge/layout", headers=h)
    assert layout.status_code == 200, layout.text
    assert layout.json()["kind"] == "badge" and layout.json()["qrRequired"] is True
    assert layout.json()["page"] == {"widthMm": 54, "heightMm": 85.6}
    assert "classic" in layout.json()["starters"] and "label_brother" in layout.json()["starters"]
    assert any(l["id"] == "photo" for l in layout.json()["layers"])

    st = badge_starter("simple", page=None)
    saved = await client.put(f"/api/v1/events/{world['event'].id}/designs/badge/layout", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=h)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 1 and saved.json()["isOwn"] is True
    assert saved.json()["page"] == {"widthMm": 87, "heightMm": 130}
    # A starter asked for now is drawn for the page the design is on.
    insert = await client.get(f"/api/v1/events/{world['event'].id}/designs/badge/layout/starters/insert", headers=h)
    assert insert.json()["page"] == {"widthMm": 87, "heightMm": 130}

    png = await client.post(f"/api/v1/events/{world['event'].id}/designs/badge/layout/preview", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=h)
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    summary = await client.get(_url(world, "/summary"), headers=h)
    assert summary.json()["design_version"] == 1


async def test_a_card_prints_for_a_guest_and_the_rule_decides_a_second_one(client, world):
    h = world["club"]
    first = await client.get(_url(world, f"/{world['a'].id}.pdf"), params={"source": "manual"}, headers=h)
    assert first.status_code == 200, first.text
    assert first.headers["content-type"].startswith("application/pdf") and first.content[:4] == b"%PDF"
    summary = await client.get(_url(world, "/summary"), headers=h)
    assert summary.json()["registered"] == 2 and summary.json()["printed"] == 1 and summary.json()["jobs"] == 1
    assert [r["role"] for r in summary.json()["by_role"]] == ["attendee", "speaker"]

    # Anyone may print again by default; a preview never counts.
    assert (await client.get(_url(world, f"/{world['a'].id}.pdf"), headers=world["vol"])).status_code == 200
    assert (await client.get(_url(world, f"/{world['s'].id}.pdf"), params={"record": "false"}, headers=world["vol"])).status_code == 200
    summary = await client.get(_url(world, "/summary"), headers=h)
    assert summary.json()["printed"] == 1 and summary.json()["jobs"] == 2

    # The organiser only: a volunteer's door prints a first card, not a second.
    assert (await client.patch(_url(world, "/settings"), json={"reprint_policy": "organiser"}, headers=h)).json()["reprint_policy"] == "organiser"
    refused = await client.get(_url(world, f"/{world['a'].id}.pdf"), headers=world["vol"])
    assert refused.status_code == 403 and "running it" in refused.json()["message"]
    assert (await client.get(_url(world, f"/{world['s'].id}.pdf"), headers=world["vol"])).status_code == 200
    assert (await client.get(_url(world, f"/{world['a'].id}.pdf"), headers=h)).status_code == 200

    # Never: even the organiser needs to say so.
    assert (await client.patch(_url(world, "/settings"), json={"reprint_policy": "never"}, headers=h)).status_code == 200
    assert (await client.get(_url(world, f"/{world['a'].id}.pdf"), headers=h)).status_code == 403
    overrode = await client.get(_url(world, f"/{world['a'].id}.pdf"), params={"override": "true"}, headers=h)
    assert overrode.status_code == 200
    jobs = await client.get(_url(world, "/jobs"), headers=h)
    assert jobs.json()[0]["overrode"] is True and jobs.json()[0]["guest"] == "Ananya Iyer"
    assert (await client.patch(_url(world, "/settings"), json={"preprinted_carries": ["nope"]}, headers=h)).status_code == 422
    assert (await client.patch(_url(world, "/settings"), json={"preprinted_carries": ["background", "org_logo"]}, headers=h)).json()["preprinted_carries"] == ["background", "org_logo"]


async def test_a_batch_prints_every_card_or_only_the_ones_not_yet_printed(client, world):
    h = world["club"]
    assert (await client.get(_url(world, f"/{world['a'].id}.pdf"), headers=h)).status_code == 200
    unprinted = await client.get(_url(world, "/batch.pdf"), params={"scope": "not_printed", "record": "true"}, headers=h)
    assert unprinted.status_code == 200, unprinted.text
    assert unprinted.headers["x-cards"] == "1" and unprinted.content[:4] == b"%PDF"
    everyone = await client.get(_url(world, "/batch.pdf"), params={"scope": "all", "role": "speaker"}, headers=h)
    assert everyone.headers["x-cards"] == "1"
    nothing = await client.get(_url(world, "/batch.pdf"), params={"scope": "not_printed"}, headers=h)
    assert nothing.status_code == 200 and nothing.headers["x-cards"] == "0"
    summary = await client.get(_url(world, "/summary"), headers=h)
    assert summary.json()["printed"] == 2
    assert [j["source"] for j in (await client.get(_url(world, "/jobs"), headers=h)).json()] == ["batch", "manual"]


async def test_a_check_in_says_whether_to_print(client, world):
    h = world["club"]
    scan = lambda: client.post(f"/api/v1/events/{world['event'].id}/gate/scan", json={"code": world["a"].ticket_id, "door": "Gate A", "client_ref": uuid.uuid4().hex}, headers=h)  # noqa: E731
    first = await scan()
    assert first.json()["outcome"] == "CHECKED_IN" and first.json()["badge"] == {"auto": False, "may_print": True, "printed_at": None, "policy": "anyone"}
    ctx = await client.get(f"/api/v1/events/{world['event'].id}/gate/context", headers=h)
    assert ctx.json()["event"]["auto_print_badges"] is False and ctx.json()["event"]["reprint_policy"] == "anyone"

    assert (await client.patch(_url(world, "/settings"), json={"auto_print_badges": True}, headers=h)).status_code == 200
    assert (await client.delete(f"/api/v1/events/{world['event'].id}/gate/check-in/{world['a'].id}", headers=h)).status_code == 200
    again = await scan()
    assert again.json()["badge"]["auto"] is True
    assert (await client.get(_url(world, f"/{world['a'].id}.pdf"), params={"source": "checkin", "door": "Gate A"}, headers=h)).status_code == 200
    resweep = await scan()
    assert resweep.json()["outcome"] == "ALREADY_CHECKED_IN" and resweep.json()["badge"]["auto"] is False
    assert resweep.json()["attendee"]["badge_printed_at"] is not None
