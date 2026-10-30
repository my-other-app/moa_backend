"""The public certificate: cached minutes not a day, revalidated by ETag,
limited per address -- and issued per role in the database.

The PNG was served `public, max-age=86400`: a revoked certificate kept
showing, and a redesign took a day to appear. And `issue()` loaded every
registration as an object and stamped them all with the event default's
design version, so a speaker issued their own design read as out of date.
"""
import io
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.api.events.models import EventRegistrationsLink
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import certificate_starter
from tests.conftest import auth_header


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="cert-cache@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0)
    a = await make_user(email="cc-a@example.com")
    s = await make_user(email="cc-s@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", is_attended=True)
    r_s = await make_registration(event=event, user=s, full_name="Sita Raman", is_attended=True, role="speaker")
    await session.commit()
    return {"event": event, "a": r_a, "s": r_s, "club": auth_header(await mint_token(owner))}


@pytest.fixture
def drawn(monkeypatch):
    """Count the certificate renders (HTML designs, wkhtmltopdf stubbed)."""
    from app.api.events import certificates, router as events_router

    calls = []

    def pdf(template_path, context, options=None, template_dir=None):
        calls.append("pdf")
        return io.BytesIO(b"%PDF-1.4 stub")

    def png(template_path, context, width=1400, template_dir=None):
        calls.append("png")
        return io.BytesIO(b"\x89PNG stub")

    monkeypatch.setattr(certificates, "generate_pdf_bytes", pdf)
    monkeypatch.setattr(events_router, "generate_image_bytes", png)
    return calls


async def _design(client, world, audience="event_default"):
    st = certificate_starter("classic")
    r = await client.put(
        f"/api/v1/events/{world['event'].id}/designs/certificate/layout",
        params={"audience": audience},
        json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])),
        headers=world["club"],
    )
    assert r.status_code == 200, r.text
    return r.json()["version"]


async def _issue(client, world) -> None:
    """The club issues to whoever checked in. Without it there is nothing to
    cache, draw or revoke: a check-in is not a certificate
    (events.certificates.availability)."""
    r = await client.post(
        f"/api/v1/events/{world['event'].id}/certificates/issue",
        json={"audience": "checked_in"}, headers=world["club"],
    )
    assert r.status_code == 200, r.text


async def test_the_public_certificate_is_cached_minutes_and_revalidated(client, world, drawn):
    await _issue(client, world)
    url = f"/api/v1/events/certificates/{world['a'].ticket_id}"
    png = await client.get(f"{url}/image")
    pdf = await client.get(f"{url}/pdf")

    for r in (png, pdf):
        assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=300"
        assert r.headers["etag"].startswith('"')
    assert drawn == ["png", "pdf"]

    # Asked again with the ETag: a 304, and nothing is drawn.
    again = await client.get(f"{url}/image", headers={"If-None-Match": png.headers["etag"]})
    assert again.status_code == 304 and drawn == ["png", "pdf"]


async def test_a_revocation_or_a_redesign_changes_the_etag(client, world, drawn):
    await _issue(client, world)
    url = f"/api/v1/events/certificates/{world['a'].ticket_id}/image"
    first = (await client.get(url)).headers["etag"]

    await _design(client, world)
    redesigned = await client.get(url, headers={"If-None-Match": first})
    assert redesigned.status_code == 200 and redesigned.headers["etag"] != first

    base = f"/api/v1/events/{world['event'].id}/certificates"
    await client.post(f"{base}/{world['a'].id}/revoke", headers=world["club"])
    assert (await client.get(url)).status_code == 403


async def test_the_public_routes_are_limited_per_address(client, world, drawn):
    await _issue(client, world)
    code = world["a"].certificate_no
    answers = [(await client.get(f"/api/v1/verify/certificates/{code}")).status_code for _ in range(21)]
    assert answers[:20] == [200] * 20 and answers[20] == 429

    url = f"/api/v1/events/certificates/{world['a'].ticket_id}/image"
    statuses = [(await client.get(url)).status_code for _ in range(121)]
    assert statuses[-1] == 429 and statuses[:120] == [200] * 120


async def test_each_role_is_issued_its_own_designs_version(client, session, world):
    await _design(client, world)
    default_version = await _design(client, world)  # the default, saved twice
    # The speakers' own design, forked from the default and saved: its own version.
    speaker_version = await _design(client, world, audience="speaker")
    assert (default_version, speaker_version) == (2, 3)

    r = await client.post(f"/api/v1/events/{world['event'].id}/certificates/issue", json={"audience": "checked_in"}, headers=world["club"])
    assert r.json() == {"issued": 2, "refreshed": 0, "already_issued": 0}

    rows = dict((await session.execute(
        select(EventRegistrationsLink.full_name, EventRegistrationsLink.certificate_design_version)
        .where(EventRegistrationsLink.event_id == world["event"].id)
    )).all())
    assert rows == {"Ananya Iyer": 2, "Sita Raman": 3}
    # The speaker's certificate is current: nothing outdated, nothing to refresh.
    summary = (await client.get(f"/api/v1/events/{world['event'].id}/certificates/summary", headers=world["club"])).json()
    assert summary["outdated"] == 0
    again = await client.post(f"/api/v1/events/{world['event'].id}/certificates/issue", json={"audience": "checked_in"}, headers=world["club"])
    assert again.json() == {"issued": 0, "refreshed": 0, "already_issued": 2}

    # A new speaker design: the speaker's certificate is refreshed, the attendee's is not.
    await _design(client, world, audience="speaker")
    third = await client.post(f"/api/v1/events/{world['event'].id}/certificates/issue", json={"audience": "everyone"}, headers=world["club"])
    assert third.json() == {"issued": 0, "refreshed": 1, "already_issued": 1}


async def test_issuing_does_not_load_every_registration(session, world):
    from sqlalchemy import event as orm_event

    from app.api.certificates import service

    loaded = []

    def on_load(target, context):
        loaded.append(target)

    event_id = world["event"].id
    session.expunge_all()  # nothing already in memory
    event = await session.get(type(world["event"]), event_id)
    orm_event.listen(EventRegistrationsLink, "load", on_load)
    try:
        result = await service.issue(session, event, "everyone", [])
    finally:
        orm_event.remove(EventRegistrationsLink, "load", on_load)

    assert result["issued"] == 2 and loaded == []
