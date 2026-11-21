"""Renders hold no database: a ticket, certificate or ID card is drawn with
no transaction open (so no pooled connection, and no guest's row, waits on
the drawing), an ID card's print claim is let go when its render fails, and
an on-screen picture that cannot be drawn in time is a 503 the app and the
website answer with their own card."""
import asyncio
import io
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.api.events.models import EventRegistrationsLink
from app.config import settings
from app.core.layout import pool
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import badge_starter
from app.response import CustomHTTPException
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)
#: A card counts as printed (and is claimed) only when its link says record=true.
RECORD = {"record": "true"}


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    from app.api.certificate_templates.models import CertificateTemplates
    from app.api.templates_audience import TemplateAudience
    from app.api.ticket_templates.models import TicketTemplates

    owner, club = await make_club(name="Yukthi", email="renders-db@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0, location_name="Hall")
    user = await make_user(email="renders-db-guest@example.com")
    other = await make_user(email="renders-db-other@example.com")
    reg = await make_registration(event=event, user=user, full_name="Ananya Iyer", is_attended=True, certificate_issued_at=datetime.now(timezone.utc))
    second = await make_registration(event=event, user=other, full_name="Bala Krishnan")
    # The event's own HTML designs: drawn here, on the render pool.
    for model in (TicketTemplates, CertificateTemplates):
        session.add(model(club_id=None, event_id=event.id, audience=TemplateAudience.event_default, name="HTML", is_default=False, config={}))
    await session.commit()
    club_h = auth_header(await mint_token(owner))
    st = badge_starter("card_cr80", page=None)
    card = await client.put(f"/api/v1/events/{event.id}/designs/badge/layout", json=dump(TicketLayoutFields(canvas=st["canvas"], layers=st["layers"], page=st["page"])), headers=club_h)
    assert card.status_code == 200, card.text
    return {"event": event, "reg": reg, "second": second, "club": club_h, "guest": auth_header(await mint_token(user))}


@pytest.fixture
def drawing(session, monkeypatch):
    """Whether the request's session had a transaction open each time
    something was handed to the render pool, and the HTML renders stubbed."""
    from app.api.events import certificates, router as events_router, tickets

    seen: list[bool] = []
    real_run = pool.run

    async def run(fn, /, *args, **kwargs):
        seen.append(session.in_transaction())
        return await real_run(fn, *args, **kwargs)

    def html_pdf(template_path, context, options=None, template_dir=None):
        return io.BytesIO(b"%PDF-1.4 stub")

    def html_png(template_path, context, width=1400, template_dir=None):
        return io.BytesIO(b"\x89PNG stub")

    monkeypatch.setattr(pool, "run", run)
    for module in (tickets, certificates, events_router):
        monkeypatch.setattr(module, "generate_pdf_bytes", html_pdf)
    monkeypatch.setattr(events_router, "generate_image_bytes", html_png)
    return seen


async def test_tickets_and_certificates_are_drawn_with_no_transaction_open(client, world, drawing):
    code = world["reg"].ticket_id
    for path in (f"tickets/{code}/image", f"tickets/{code}/pdf", f"certificates/{code}/image", f"certificates/{code}/pdf"):
        r = await client.get(f"/api/v1/events/{path}", headers=world["guest"])
        assert r.status_code == 200, (path, r.text)
    assert len(drawing) == 4 and not any(drawing)


async def test_id_cards_are_drawn_with_no_transaction_open_and_recorded_after(client, session, world, drawing):
    url = f"/api/v1/events/{world['event'].id}/idcards"
    one = await client.get(f"{url}/{world['reg'].id}.pdf", params=RECORD, headers=world["club"])
    assert one.status_code == 200, one.text
    batch = await client.get(f"{url}/batch.pdf", params={"scope": "not_printed", "record": "true"}, headers=world["club"])
    assert batch.status_code == 200 and batch.headers["x-cards"] == "1", batch.text
    assert drawing and not any(drawing)
    printed = dict((await session.execute(
        select(EventRegistrationsLink.id, EventRegistrationsLink.badge_printed_at).where(EventRegistrationsLink.event_id == world["event"].id)
    )).all())
    assert printed[world["reg"].id] is not None and printed[world["second"].id] is not None
    summary = await client.get(f"{url}/summary", headers=world["club"])
    assert summary.json()["printed"] == 2 and summary.json()["jobs"] == 2


async def test_a_card_claim_is_let_go_when_its_render_fails(client, session, world, monkeypatch):
    from app.api.idcards import service as idcards

    url = f"/api/v1/events/{world['event'].id}/idcards"
    assert (await client.patch(f"{url}/settings", json={"reprint_policy": "never"}, headers=world["club"])).status_code == 200
    during = []

    async def broken(session_, event, reg, style, **kw):
        # The claim is kept before the drawing starts, with no transaction open.
        during.append((session.in_transaction(), reg.badge_printed_at is not None))
        raise CustomHTTPException(503, message="The render service is down.", error_code="RENDER_UNAVAILABLE")

    real = idcards._draw_card
    monkeypatch.setattr(idcards, "_draw_card", broken)
    failed = await client.get(f"{url}/{world['reg'].id}.pdf", params=RECORD, headers=world["club"])
    assert failed.status_code == 503 and during == [(False, True)]
    row = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.id == world["reg"].id).execution_options(populate_existing=True))
    assert row.badge_printed_at is None
    # Their one card was not used up: it prints once the render works again.
    monkeypatch.setattr(idcards, "_draw_card", real)
    assert (await client.get(f"{url}/{world['reg'].id}.pdf", params=RECORD, headers=world["club"])).status_code == 200
    assert (await client.get(f"{url}/{world['reg'].id}.pdf", params=RECORD, headers=world["club"])).status_code == 403


async def test_a_ticket_picture_that_cannot_be_drawn_in_time_is_a_503(client, world, monkeypatch):
    from app.api.events import router as events_router

    monkeypatch.setattr(settings, "RENDER_PICTURE_WAIT", 0.1)

    def slow(template_path, context, width=1400, template_dir=None):
        import time

        time.sleep(0.4)
        return io.BytesIO(b"\x89PNG stub")

    monkeypatch.setattr(events_router, "generate_image_bytes", slow)
    r = await client.get(f"/api/v1/events/tickets/{world['reg'].ticket_id}/image", headers=world["guest"])
    assert r.status_code == 503 and r.json()["error_code"] == "RENDER_BUSY"
    await asyncio.sleep(0.4)  # the thread finishes its drawing before the next test


async def test_pictures_beyond_the_queue_wait_for_a_slot_within_the_same_bound(monkeypatch):
    import weakref

    monkeypatch.setattr(settings, "RENDER_THREADS", 1)
    monkeypatch.setattr(settings, "RENDER_PICTURE_WAIT", 0.45)
    monkeypatch.setattr(pool, "_picture_slots", weakref.WeakKeyDictionary())

    async def make():
        await asyncio.sleep(0.3)
        return "png"

    results = await asyncio.gather(*(pool.picture(make) for _ in range(pool.PICTURE_QUEUE_PER_THREAD + 1)), return_exceptions=True)
    assert results[: pool.PICTURE_QUEUE_PER_THREAD] == ["png"] * pool.PICTURE_QUEUE_PER_THREAD
    assert isinstance(results[-1], pool.RenderBusy)
