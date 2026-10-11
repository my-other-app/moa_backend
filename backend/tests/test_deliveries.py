"""Sending tickets, and knowing who got one.

A ticket emailed and never recorded is a ticket nobody can say was sent.
These tests send through a fake SES and read back what the log and the
attendee list say about each guest.
"""
import io
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from app.api.deliveries import service
from app.core.email import email as email_mod
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from tests.conftest import auth_header


@pytest.fixture
def outbox(monkeypatch):
    """Every mail the worker hands to SES, and a switch to make SES refuse."""
    sent: list[dict] = []
    state = {"refuse": None}

    def send(**kwargs):
        if state["refuse"]:
            raise RuntimeError(state["refuse"])
        sent.append(kwargs)
        return {"MessageId": f"ses-{len(sent)}"}

    # Tickets go out in the event's designed invite (`send_email`), or in the
    # plain words (`send_email_with_attachment`) when the club switches the
    # invite off. Both land here, with their own argument shapes.
    monkeypatch.setattr(email_mod, "send_email_with_attachment", send)
    monkeypatch.setattr(email_mod, "send_email", send)
    return {"sent": sent, "state": state}


@pytest.fixture
def background(monkeypatch, session):
    """The worker runs after the response on its own session; here it is the
    test's, so what it writes is what the test can read."""

    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(service, "session_scope", scope)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="deliveries@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0,
        location_name="Innovation Hall",
    )
    a = await make_user(email="ananya@example.com")
    b = await make_user(email="bala@example.com")
    c = await make_user(email="chitra@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer")
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan")
    # Registered at the desk with no address: a fact about the guest, not a failure.
    r_c = await make_registration(event=event, user=c, full_name="Chitra Nair", email="")
    other_owner, _ = await make_club(name="Other", email="other@example.com")
    await session.commit()
    headers = auth_header(await mint_token(owner))
    # A layered design, so tickets render with reportlab rather than a browser.
    s = ticket_starter("classic")
    r = await client.put(
        f"/api/v1/events/{event.id}/designs/ticket/layout",
        json=dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"])),
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return {
        "event": event, "a": r_a, "b": r_b, "c": r_c, "club": headers,
        "other": auth_header(await mint_token(other_owner)),
    }


def _send(client, world, **body):
    return client.post(f"/api/v1/events/{world['event'].id}/tickets/send", json=body, headers=world["club"])


async def test_send_emails_everyone_with_an_address_and_records_each_one(client, world, outbox, background):
    r = await _send(client, world)

    assert r.status_code == 200, r.text
    assert r.json() == {"queued": 3, "already_sent": 0, "revoked": 0, "channels": ["email"]}
    assert [m["recipients"] for m in outbox["sent"]] == [["ananya@example.com"], ["bala@example.com"]]
    mail = outbox["sent"][0]
    assert mail["subject"] == "Your ticket for Build Night"
    # The designed invite carries the ticket PDF and the guest's QR inline.
    ticket = next(a for a in mail["attachments"] if a["filename"] == f"ticket-{world['a'].ticket_id}.pdf")
    assert ticket["content"][:4] == b"%PDF"
    assert any(a.get("content_id") == "ticket-qr" for a in mail["attachments"])
    assert "Innovation Hall" in mail["text"]

    summary = (await client.get(f"/api/v1/events/{world['event'].id}/deliveries/summary", headers=world["club"])).json()
    assert summary["registrations"] == 3
    email = next(c for c in summary["channels"] if c["channel"] == "email")
    assert (email["sent"], email["skipped"], email["failed"], email["pending"], email["unsent"]) == (2, 1, 0, 0, 0)

    # The attendee list says who was reached, in the same words.
    rows = (await client.get(f"/api/v1/events/registration/{world['event'].id}/list", headers=world["club"])).json()["items"]
    by_name = {row["full_name"]: row["ticket_deliveries"] for row in rows}
    assert by_name["Ananya Iyer"][0]["status"] == "sent"
    assert by_name["Chitra Nair"][0] == {**by_name["Chitra Nair"][0], "status": "skipped", "error": "no email address"}


async def test_sending_again_does_not_send_twice_unless_asked(client, world, outbox, background):
    await _send(client, world)
    again = await _send(client, world)
    # The two delivered are left alone; the guest with no address is tried
    # again, in case one was added -- and skipped again, since none was.
    assert again.json()["queued"] == 1
    assert again.json()["already_sent"] == 2
    assert len(outbox["sent"]) == 2

    everyone = await _send(client, world, only="all")
    assert everyone.json()["queued"] == 3
    assert len(outbox["sent"]) == 4

    one = await client.post(f"/api/v1/events/{world['event'].id}/tickets/{world['a'].id}/send", headers=world["club"])
    assert one.json()["queued"] == 1
    assert outbox["sent"][-1]["recipients"] == ["ananya@example.com"]


async def test_a_refusal_is_recorded_and_retried_on_request(client, world, outbox, background):
    outbox["state"]["refuse"] = "MessageRejected: address is on the suppression list"
    await _send(client, world)

    log = (await client.get(f"/api/v1/events/{world['event'].id}/deliveries", params={"problem": "true"}, headers=world["club"])).json()
    assert log["total"] == 2
    assert all(row["status"] == "failed" for row in log["items"])
    assert "suppression list" in log["items"][0]["reason"]
    assert log["tally"]["failed"] == 2 and log["tally"]["skipped"] == 1

    outbox["state"]["refuse"] = None
    retry = await _send(client, world, only="failed")
    # The two refusals and the guest with no address are all tried again.
    assert retry.json()["queued"] == 3
    assert len(outbox["sent"]) == 2
    log = (await client.get(f"/api/v1/events/{world['event'].id}/deliveries", params={"search": "Ananya"}, headers=world["club"])).json()
    assert log["total"] == 1
    assert log["items"][0]["status"] == "sent"
    assert log["items"][0]["attempts"] == 2


async def test_a_revoked_ticket_is_refused_at_the_door_and_not_sent(client, world, outbox, background):
    e, a = world["event"].id, world["a"]
    r = await client.post(f"/api/v1/events/{e}/tickets/{a.id}/revoke", json={"reason": "transferred"}, headers=world["club"])
    assert r.status_code == 200, r.text
    assert r.json()["revoked_at"] is not None

    checkin = await client.post(f"/api/v1/events/registration/{e}/attendance/{a.id}", json={"is_attended": True}, headers=world["club"])
    assert checkin.status_code == 409, checkin.text

    sent = await _send(client, world)
    assert sent.json()["revoked"] == 1
    assert [m["recipients"] for m in outbox["sent"]] == [["bala@example.com"]]

    r = await client.post(f"/api/v1/events/{e}/tickets/{a.id}/reinstate", headers=world["club"])
    assert r.json()["revoked_at"] is None
    checkin = await client.post(f"/api/v1/events/registration/{e}/attendance/{a.id}", json={"is_attended": True}, headers=world["club"])
    assert checkin.status_code == 200, checkin.text


async def test_the_bundle_is_one_pdf_per_guest_named_by_ticket(client, world):
    r = await client.get(f"/api/v1/events/{world['event'].id}/tickets/bundle.zip", headers=world["club"])

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    names = sorted(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert names == sorted(
        f"{reg.ticket_id} {name}.pdf"
        for reg, name in ((world["a"], "Ananya Iyer"), (world["b"], "Bala Krishnan"), (world["c"], "Chitra Nair"))
    )


async def test_another_club_cannot_send_or_read(client, world):
    e = world["event"].id
    assert (await _send(client, {"event": world["event"], "club": world["other"]})).status_code == 403
    assert (await client.get(f"/api/v1/events/{e}/deliveries", headers=world["other"])).status_code == 403
