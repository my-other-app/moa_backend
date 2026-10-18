"""Designed emails: a club designs once, every event starts from it, and
each send is a message that remembers who it reached.

The sends go through a fake SES; the tests read back what the messages,
the journey and the guests' inboxes say.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest

from app.api.deliveries import pipeline
from app.api.deliveries import service as deliveries
from app.api.emails import service
from app.core.email import email as email_mod
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from app.core.mail import links
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def outbox(monkeypatch):
    """Every designed mail handed to SES; the plain sender must not be used."""
    sent: list[dict] = []
    plain: list[dict] = []
    state = {"refuse": None}

    def send(**kwargs):
        if state["refuse"]:
            raise RuntimeError(state["refuse"])
        sent.append(kwargs)
        return {"MessageId": f"ses-{len(sent)}"}

    def send_plain(**kwargs):
        plain.append(kwargs)
        return {"MessageId": f"plain-{len(plain)}"}

    monkeypatch.setattr(email_mod, "send_email", send)
    monkeypatch.setattr(email_mod, "send_email_with_attachment", send_plain)
    return {"sent": sent, "plain": plain, "state": state}


@pytest.fixture
def background(monkeypatch, session):
    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(deliveries, "session_scope", scope)


@pytest.fixture(autouse=True)
async def jobs_finished(session):
    """Sends the sweep starts run beside it, on the test's session: every one
    is finished before that session goes."""
    pipeline.reset_for_tests()
    yield
    await pipeline.drain(timeout=30)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="emails@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0,
        location_name="Innovation Hall", contact_email="hello@yukthi.in",
    )
    a = await make_user(email="ananya-e@example.com")
    b = await make_user(email="bala-e@example.com")
    c = await make_user(email="chitra-e@example.com")
    s = await make_user(email="sita-e@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", is_attended=True)
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan")
    r_c = await make_registration(event=event, user=c, full_name="Chitra Nair", email="")
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", is_attended=True, role="speaker")
    other_owner, _ = await make_club(name="Other", email="other-e@example.com")
    await session.commit()
    headers = auth_header(await mint_token(owner))
    st = ticket_starter("classic")
    r = await client.put(
        f"/api/v1/events/{event.id}/designs/ticket/layout",
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return {
        "event": event, "club_row": club, "owner": owner, "a": r_a, "b": r_b, "c": r_c, "s": r_s, "club": headers,
        "other": auth_header(await mint_token(other_owner)),
    }


def _url(world, path=""):
    return f"/api/v1/events/{world['event'].id}/emails{path}"


async def _journey(client, world) -> dict:
    r = await client.get(_url(world), headers=world["club"])
    assert r.status_code == 200, r.text
    return r.json()


def _stage(journey: dict, stage: str) -> dict:
    return next(s for s in journey["journey"] if s["stage"] == stage)


async def test_an_event_starts_with_the_four_journey_emails_from_the_club_library(client, world):
    first = await _journey(client, world)
    assert [s["stage"] for s in first["journey"]] == ["INVITE", "PRE_EVENT", "REMINDER", "POST_EVENT"]
    assert first["custom"] == []
    invite = _stage(first, "INVITE")["template"]
    assert invite["eventId"] == world["event"].id
    assert invite["subject"] == "Your ticket for {{event_name}}"
    assert invite["attachments"]["ticketPdf"] is True
    assert invite["design"]["blocks"][0]["kind"] == "heading"

    library = await client.get("/api/v1/clubs/me/emails", headers=world["club"])
    assert library.status_code == 200, library.text
    assert [t["stage"] for t in library.json()] == ["INVITE", "PRE_EVENT", "REMINDER", "POST_EVENT"]
    assert all(t["eventId"] is None for t in library.json())
    assert invite["sourceId"] == next(t["id"] for t in library.json() if t["stage"] == "INVITE")

    # Asking again creates nothing more.
    second = await _journey(client, world)
    assert [s["template"]["id"] for s in second["journey"]] == [s["template"]["id"] for s in first["journey"]]
    assert (await client.get(_url(world), headers=world["other"])).status_code == 403


async def test_saving_checks_the_template_and_bumps_the_version(client, world):
    invite = _stage(await _journey(client, world), "INVITE")["template"]
    body = {k: v for k, v in invite.items() if k in ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")}

    same = await client.put(_url(world, f"/{invite['id']}"), json=body, headers=world["club"])
    assert same.status_code == 200, same.text
    assert same.json()["version"] == 1

    body["subject"] = "See you at {{event_name}}, {{first_name}}"
    changed = await client.put(_url(world, f"/{invite['id']}"), json=body, headers=world["club"])
    assert changed.json()["version"] == 2

    body["subject"] = "Your {{tikcet_url}}"
    bad = await client.put(_url(world, f"/{invite['id']}"), json=body, headers=world["club"])
    assert bad.status_code == 422, bad.text
    assert bad.json()["errors"]["fields"]["design"][0].startswith("subject:")

    body["subject"] = "Fine"
    body["design"]["blocks"].append({"kind": "image", "id": "im1", "source": "upload", "imageKey": "uploads/events/999/layout/image-x.png"})
    foreign = await client.put(_url(world, f"/{invite['id']}"), json=body, headers=world["club"])
    assert foreign.status_code == 422
    assert "another event" in foreign.json()["message"]

    # A journey email cannot be deleted, only switched off.
    assert (await client.delete(_url(world, f"/{invite['id']}"), headers=world["club"])).status_code == 422


async def test_preview_shows_a_sample_guest_or_a_real_one(client, world):
    invite = _stage(await _journey(client, world), "INVITE")["template"]
    sample = await client.post(_url(world, "/preview"), json={"subject": invite["subject"], "design": invite["design"]}, headers=world["club"])
    assert sample.status_code == 200, sample.text
    assert sample.json()["subject"] == "Your ticket for Build Night"
    assert "Ananya" in sample.json()["html"]
    assert 'src="data:image/png;base64,' in sample.json()["html"]
    assert sample.json()["problems"] == []

    real = await client.post(
        _url(world, "/preview"), json={"subject": invite["subject"], "design": invite["design"], "registrationId": str(world["b"].id)}, headers=world["club"]
    )
    assert "Bala" in real.json()["html"] and "Ananya" not in real.json()["html"]
    assert world["b"].ticket_id in real.json()["html"]
    assert f"/api/v1/files/tickets/{world['b'].ticket_id}.pdf?s=" in real.json()["html"]

    ctx = await client.get(_url(world, "/context"), headers=world["club"])
    assert ctx.status_code == 200
    assert any(t["key"] == "ticket_url" for t in ctx.json()["tokens"])
    assert ctx.json()["sampleValues"]["event_name"] == "Build Night"
    assert ctx.json()["sampleValues"]["organiser_email"] == "hello@yukthi.in"


async def test_sending_a_journey_email_reaches_everyone_with_an_address(client, world, outbox, background):
    reminder = _stage(await _journey(client, world), "REMINDER")["template"]
    # The reminder starter goes to guests not yet inside; here, to everyone.
    assert reminder["audience"]["who"] == "NOT_CHECKED_IN"
    body = {k: v for k, v in reminder.items() if k in ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")}
    body["audience"] = {"who": "ALL", "roles": []}
    assert (await client.put(_url(world, f"/{reminder['id']}"), json=body, headers=world["club"])).status_code == 200
    sent = await client.post(_url(world, f"/{reminder['id']}/send"), headers=world["club"])
    assert sent.status_code == 200, sent.text
    assert sent.json()["recipients"] == 4
    assert sent.json()["kind"] == "TEMPLATE"

    history = await client.get(f"/api/v1/events/{world['event'].id}/messages", headers=world["club"])
    assert history.status_code == 200, history.text
    (msg,) = history.json()["items"]
    assert (msg["status"], msg["sentCount"], msg["skippedCount"], msg["failedCount"]) == ("SENT", 3, 1, 0)
    assert msg["scheduled"] is False

    detail = await client.get(f"/api/v1/events/{world['event'].id}/messages/{msg['id']}", headers=world["club"])
    rows = {d["name"]: d for d in detail.json()["deliveries"]}
    assert rows["Chitra Nair"]["status"] == "skipped" and rows["Chitra Nair"]["error"] == "no email address"
    assert rows["Ananya Iyer"]["status"] == "sent"

    assert len(outbox["sent"]) == 3 and outbox["plain"] == []
    by_to = {m["recipients"][0]: m for m in outbox["sent"]}
    ananya = by_to["ananya-e@example.com"]
    assert "Build Night" in ananya["subject"]
    assert "Ananya" in ananya["html"] and "Ananya" in ananya["text"]
    assert ananya["sender_name"] == "Yukthi"
    # A refused send is a failed row, and the journey shows the last message.
    outbox["state"]["refuse"] = "SES said no"
    again = await client.post(_url(world, f"/{reminder['id']}/send"), headers=world["club"])
    assert again.status_code == 200
    journey = await _journey(client, world)
    last = _stage(journey, "REMINDER")["lastMessage"]
    assert (last["failedCount"], last["sentCount"]) == (3, 0)


async def test_a_ticket_send_uses_the_designed_invite_once_it_exists(client, world, outbox, background):
    invite = _stage(await _journey(client, world), "INVITE")["template"]
    r = await client.post(f"/api/v1/events/{world['event'].id}/tickets/send", json={"only": "unsent"}, headers=world["club"])
    assert r.status_code == 200, r.text
    assert r.json()["queued"] == 4
    assert outbox["plain"] == []
    assert len(outbox["sent"]) == 3
    mail = next(m for m in outbox["sent"] if m["recipients"] == ["ananya-e@example.com"])
    assert mail["subject"] == "Your ticket for Build Night"
    names = [a["filename"] for a in mail["attachments"]]
    assert f"ticket-{world['a'].ticket_id}.pdf" in names
    qr = next(a for a in mail["attachments"] if a.get("content_id") == "ticket-qr")
    assert qr["content"][:8] == b"\x89PNG\r\n\x1a\n"
    assert 'src="cid:ticket-qr"' in mail["html"]
    assert world["a"].ticket_id in mail["text"]

    # Switched off, the plain ticket email is back.
    body = {k: v for k, v in invite.items() if k in ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")}
    body["enabled"] = False
    assert (await client.put(_url(world, f"/{invite['id']}"), json=body, headers=world["club"])).status_code == 200
    r = await client.post(f"/api/v1/events/{world['event'].id}/tickets/{world['a'].id}/send", headers=world["club"])
    assert r.status_code == 200
    assert len(outbox["plain"]) == 1 and len(outbox["sent"]) == 3


async def test_a_timed_email_goes_out_once_when_due(client, session, world, outbox, background):
    pre = _stage(await _journey(client, world), "PRE_EVENT")["template"]
    body = {k: v for k, v in pre.items() if k in ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")}
    body["trigger"] = {"mode": "BEFORE_START", "minutes": 24 * 60}
    saved = await client.put(_url(world, f"/{pre['id']}"), json=body, headers=world["club"])
    assert saved.status_code == 200, saved.text
    due = _stage(await _journey(client, world), "PRE_EVENT")["dueAt"]
    assert due is not None and due.startswith("2026-11-19T18:00")

    assert await service.sweep(session, now=START - timedelta(hours=25)) == 0
    assert await service.sweep(session, now=START - timedelta(hours=23)) == 1
    # The sweep only queues: the sending is a job beside it.
    assert outbox["sent"] == []
    await pipeline.drain()
    assert await service.sweep(session, now=START - timedelta(hours=22)) == 0
    assert len(outbox["sent"]) == 3

    history = await client.get(f"/api/v1/events/{world['event'].id}/messages", headers=world["club"])
    (msg,) = history.json()["items"]
    assert msg["scheduled"] is True and msg["sentCount"] == 3
    assert _stage(await _journey(client, world), "PRE_EVENT")["dueAt"] is None

    # Long past its time -- a reminder for an event that already happened
    # is not sent on the day the timer is switched on.
    post = _stage(await _journey(client, world), "POST_EVENT")["template"]
    body = {k: v for k, v in post.items() if k in ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")}
    body["trigger"] = {"mode": "AFTER_END", "minutes": 60}
    assert (await client.put(_url(world, f"/{post['id']}"), json=body, headers=world["club"])).status_code == 200
    assert await service.sweep(session, now=START + timedelta(days=10)) == 0
    # Five hours after the start, two have come due: this one and the
    # reminder the starter times for just before the doors open.
    assert await service.sweep(session, now=START + timedelta(hours=5)) == 2
    await pipeline.drain()
    assert await service.sweep(session, now=START + timedelta(hours=6)) == 0


async def test_a_broadcast_is_sent_once_per_client_ref(client, world, outbox, background):
    count = await client.post(_url(world, "/audience"), json={"who": "CHECKED_IN", "roles": []}, headers=world["club"])
    assert count.json() == {"recipients": 2, "withoutEmail": 0}
    count = await client.post(_url(world, "/audience"), json={"who": "ALL", "roles": ["speaker"]}, headers=world["club"])
    assert count.json() == {"recipients": 1, "withoutEmail": 0}

    ref = "6f1b9b2e-5a0e-4b2a-9c4e-9a3c2d1e0f11"
    body = {
        "subject": "Doors at six, {{first_name}}", "body": "Hello **{{first_name}}**,\n\nParking is behind the hall. See [the map](https://maps.example/hall).",
        "audience": {"who": "CHECKED_IN", "roles": []}, "clientRef": ref,
    }
    first = await client.post(f"/api/v1/events/{world['event'].id}/messages", json=body, headers=world["club"])
    assert first.status_code == 200, first.text
    assert first.json()["recipients"] == 2 and first.json()["kind"] == "BROADCAST"
    second = await client.post(f"/api/v1/events/{world['event'].id}/messages", json=body, headers=world["club"])
    assert second.json()["id"] == first.json()["id"]
    assert len(outbox["sent"]) == 2
    mail = next(m for m in outbox["sent"] if m["recipients"] == ["sita-e@example.com"])
    assert mail["subject"] == "Doors at six, Sita"
    assert "<strong>Sita</strong>" in mail["html"]
    assert 'href="https://maps.example/hall"' in mail["html"]
    assert "the map (https://maps.example/hall)" in mail["text"]

    empty = await client.post(f"/api/v1/events/{world['event'].id}/messages", json={"subject": "x", "body": "  ", "audience": {"who": "ALL"}}, headers=world["club"])
    assert empty.status_code == 422


async def test_the_link_in_the_email_opens_the_guests_ticket(client, world):
    url = links.file_url("ticket", world["a"].ticket_id)
    assert url.startswith("https://api.myotherapp.in/api/v1/files/tickets/")
    parsed = urlparse(url)
    good = await client.get(f"{parsed.path}?{parsed.query}")
    assert good.status_code == 200, good.text
    assert good.headers["content-type"].startswith("application/pdf")
    assert good.content[:5] == b"%PDF-"

    assert (await client.get(f"{parsed.path}?s=deadbeef")).status_code == 403
    assert (await client.get(parsed.path)).status_code == 403
    cert = urlparse(links.file_url("certificate", world["b"].ticket_id))
    assert (await client.get(f"{cert.path}?{cert.query}")).status_code == 410

    revoked = await client.post(f"/api/v1/events/{world['event'].id}/tickets/{world['a'].id}/revoke", json={}, headers=world["club"])
    assert revoked.status_code == 200
    assert (await client.get(f"{parsed.path}?{parsed.query}")).status_code == 410


async def test_save_to_library_then_a_new_event_starts_from_it(client, session, world, make_event):
    invite = _stage(await _journey(client, world), "INVITE")["template"]
    body = {k: v for k, v in invite.items() if k in ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")}
    body["subject"] = "Welcome to {{event_name}}"
    assert (await client.put(_url(world, f"/{invite['id']}"), json=body, headers=world["club"])).status_code == 200
    kept = await client.post(_url(world, f"/{invite['id']}/save-to-library"), headers=world["club"])
    assert kept.status_code == 200, kept.text
    assert kept.json()["eventId"] is None and kept.json()["subject"] == "Welcome to {{event_name}}"
    assert kept.json()["version"] == 2

    second = await make_event(club=world["club_row"], created_by=world["owner"], name="Demo Day", event_datetime=START + timedelta(days=30), duration=2.0)
    await session.commit()
    j = await client.get(f"/api/v1/events/{second.id}/emails", headers=world["club"])
    assert j.status_code == 200, j.text
    new_invite = _stage(j.json(), "INVITE")["template"]
    assert new_invite["subject"] == "Welcome to {{event_name}}"
    assert new_invite["sourceId"] == kept.json()["id"]

    # Editing the new event's copy, then resetting it, returns to the library's.
    body["subject"] = "Something else"
    assert (await client.put(f"/api/v1/events/{second.id}/emails/{new_invite['id']}", json=body, headers=world["club"])).status_code == 200
    back = await client.post(f"/api/v1/events/{second.id}/emails/{new_invite['id']}/reset", headers=world["club"])
    assert back.json()["subject"] == "Welcome to {{event_name}}"

    # A custom email: made, duplicated, deleted.
    custom = await client.post(_url(world), json={**body, "name": "Sponsors", "subject": "A word from our sponsors"}, headers=world["club"])
    assert custom.status_code == 200, custom.text
    assert custom.json()["stage"] == "CUSTOM"
    copy = await client.post(_url(world, f"/{custom.json()['id']}/duplicate"), headers=world["club"])
    assert copy.json()["name"] == "Copy of Sponsors"
    assert len((await _journey(client, world))["custom"]) == 2
    assert (await client.delete(_url(world, f"/{copy.json()['id']}"), headers=world["club"])).status_code == 200
    assert len((await _journey(client, world))["custom"]) == 1


async def test_a_guest_who_registers_gets_the_designed_invite_with_the_ticket(client, world, outbox, background):
    """The invite is the ticket email: registering sends it, through the same
    queue as Send tickets, without the club ever opening the emails page."""
    r = await client.post(
        f"/api/v1/events/registration/{world['event'].id}/register",
        json={"full_name": "Devika Menon", "email": "devika-e@example.com", "phone": "9999999999"},
    )
    assert r.status_code == 200, r.text
    assert outbox["plain"] == []
    (mail,) = outbox["sent"]
    assert mail["recipients"] == ["devika-e@example.com"]
    assert mail["subject"] == "Your ticket for Build Night"
    assert "Devika" in mail["html"]
    assert any(a["filename"].startswith("ticket-") for a in mail["attachments"])
    assert any(a.get("content_id") == "ticket-qr" for a in mail["attachments"])

    log = await client.get(f"/api/v1/events/{world['event'].id}/deliveries", params={"kind": "ticket"}, headers=world["club"])
    rows = {x["name"]: x for x in log.json()["items"]}
    assert rows["Devika Menon"]["status"] == "sent"
    # Registering forked the journey from the club's library on the way.
    journey = await _journey(client, world)
    assert _stage(journey, "INVITE")["template"]["sourceId"] is not None

    # Switched off, a guest who registers gets the plain ticket email instead.
    invite = _stage(journey, "INVITE")["template"]
    body = {k: v for k, v in invite.items() if k in ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")}
    body["enabled"] = False
    assert (await client.put(_url(world, f"/{invite['id']}"), json=body, headers=world["club"])).status_code == 200
    r = await client.post(
        f"/api/v1/events/registration/{world['event'].id}/register",
        json={"full_name": "Eshan Pillai", "email": "eshan-e@example.com", "phone": "9999999998"},
    )
    assert r.status_code == 200, r.text
    assert len(outbox["sent"]) == 1 and len(outbox["plain"]) == 1
    assert outbox["plain"][0]["recipients"] == ["eshan-e@example.com"]
    assert outbox["plain"][0]["attachment_filename"].startswith("ticket-")


async def test_the_invite_goes_out_with_the_tickets_and_nothing_else(client, world):
    invite = _stage(await _journey(client, world), "INVITE")["template"]
    body = {k: v for k, v in invite.items() if k in ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")}
    body["trigger"] = {"mode": "BEFORE_START", "minutes": 60}
    timed = await client.put(_url(world, f"/{invite['id']}"), json=body, headers=world["club"])
    assert timed.status_code == 422
    assert "goes out with the tickets" in timed.json()["message"]
    sent = await client.post(_url(world, f"/{invite['id']}/send"), headers=world["club"])
    assert sent.status_code == 422
    assert "Send tickets" in sent.json()["message"]


async def test_the_delivery_log_lists_messages_beside_tickets(client, world, outbox, background):
    assert (await client.post(f"/api/v1/events/{world['event'].id}/tickets/send", json={"only": "unsent"}, headers=world["club"])).status_code == 200
    body = {"subject": "Parking at {{venue}}", "body": "Behind the hall, {{first_name}}.", "audience": {"who": "ALL", "roles": []}}
    assert (await client.post(f"/api/v1/events/{world['event'].id}/messages", json=body, headers=world["club"])).status_code == 200

    everything = await client.get(f"/api/v1/events/{world['event'].id}/deliveries", headers=world["club"])
    assert everything.status_code == 200, everything.text
    kinds = {(x["name"], x["kind"]) for x in everything.json()["items"]}
    assert ("Ananya Iyer", "ticket") in kinds and ("Ananya Iyer", "message") in kinds
    # Four guests, two sends each: three reached and one skipped per send.
    assert everything.json()["total"] == 8
    assert everything.json()["tally"]["sent"] == 6 and everything.json()["tally"]["skipped"] == 2

    messages = await client.get(f"/api/v1/events/{world['event'].id}/deliveries", params={"kind": "message"}, headers=world["club"])
    rows = messages.json()["items"]
    # The subject as the club typed it: the same words the messages page shows.
    assert len(rows) == 4 and all(r["what"] == "Parking at {{venue}}" for r in rows)
    assert messages.json()["tally"]["sent"] == 3
    problems = await client.get(f"/api/v1/events/{world['event'].id}/deliveries", params={"problem": "true"}, headers=world["club"])
    assert problems.json()["items"] == [] and problems.json()["tally"]["skipped"] == 2


async def test_the_library_overview_knows_use_results_and_what_goes_out_next(client, session, world):
    h = world["club"]
    # The event's copies exist once its emails were looked at; the pre-event one is timed.
    journey = (await client.get(f"/api/v1/events/{world['event'].id}/emails", headers=h)).json()
    pre = next(s for s in journey["journey"] if s["stage"] == "PRE_EVENT")
    # Send the reminder by hand, so there is a message with recipients.
    inv = next(s for s in journey["journey"] if s["stage"] == "REMINDER")
    sent = await client.post(f"/api/v1/events/{world['event'].id}/emails/{inv['template']['id']}/send", headers=h)
    assert sent.status_code == 200, sent.text

    o = (await client.get("/api/v1/clubs/me/emails/overview", headers=h)).json()
    assert o["figures"]["messages"] == 1 and o["figures"]["recipients"] >= 1  # the reminder goes to guests not yet inside
    assert o["all_time"]["messages"] == 1
    by = {t["stage"]: t for t in o["templates"]}
    assert set(by) >= {"INVITE", "PRE_EVENT", "REMINDER", "POST_EVENT"}
    assert by["REMINDER"]["usage"]["events"] == 1 and by["REMINDER"]["usage"]["sends"] == 1 and by["REMINDER"]["usage"]["last_sent_at"]
    assert by["PRE_EVENT"]["usage"]["sends"] == 0 and by["INVITE"]["label"] == "Invite" and by["INVITE"]["usage"]["events"] == 1
    # The pre-event email is due a day before the start, and has not gone.
    q = next((x for x in o["queue"] if x["stage"] == "PRE_EVENT"), None)
    assert q is not None and q["event"]["name"] == "Build Night" and q["template_id"] == pre["template"]["id"]
    assert o["recent"][0]["stage"] == "REMINDER" and o["recent"][0]["recipients"] >= 1
    assert o["attention"]["disabled_stages"] == [] and o["delta"]["messages"] == 1
    assert (await client.get("/api/v1/clubs/me/emails/overview", headers=world["other"])).json()["templates"][0]["usage"]["events"] == 0
