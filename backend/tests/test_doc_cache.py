"""Rendered documents are kept, bundles stream, a batch is one document.

A ticket was drawn again for every download, every resend and every bundle;
a bundle was built whole in memory before a byte left; a batch of ID cards
was a PDF per card, merged by pdfium.
"""
import io
import re
import zipfile
from datetime import datetime, timezone

import pytest

from app.api.events import doc_cache, tickets
from app.api.templates_audience import TemplateAudience
from app.api.ticket_templates.models import TicketTemplates
from app.config import settings
from app.core.storage import objects
from tests.conftest import auth_header


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="doc-cache@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0)
    a = await make_user(email="dc-a@example.com")
    b = await make_user(email="dc-b@example.com")
    r_a = await make_registration(event=event, user=a, full_name="Ananya Iyer", is_attended=True)
    r_b = await make_registration(event=event, user=b, full_name="Bala Krishnan", is_attended=True, role="speaker")
    await session.commit()
    return {"event": event, "a": r_a, "b": r_b, "club": auth_header(await mint_token(owner)), "guest": auth_header(await mint_token(a))}


@pytest.fixture
def bucket(monkeypatch):
    """The bucket as a dict, and the cache switched on."""
    held: dict = {}
    monkeypatch.setattr(settings, "RENDER_DOC_CACHE", True)
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: held.get(key))
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, data))
    return held


@pytest.fixture
def drawn(monkeypatch):
    calls = []

    def pdf(template_path, context, options=None, template_dir=None):
        calls.append(context["email_payload"]["participant_name"])
        return io.BytesIO(b"%PDF-1.4 " + context["email_payload"]["participant_name"].encode())

    from app.api.events import certificates

    monkeypatch.setattr(tickets, "generate_pdf_bytes", pdf)
    monkeypatch.setattr(certificates, "generate_pdf_bytes", pdf)
    return calls


async def _reg(session, world, who="a"):
    from app.api.events import service

    return await service.get_ticket_details(session, ticket_id=world[who].ticket_id)


async def test_a_ticket_is_drawn_once_and_then_read(session, world, bucket, drawn):
    reg = await _reg(session, world)
    first = await tickets.ticket_document(session, reg)
    second = await tickets.ticket_document(session, reg)

    assert first.content == second.content and drawn == ["Ananya Iyer"]
    (key,) = bucket
    assert re.fullmatch(rf"{re.escape(settings.S3_BASE_PATH)}/docs/ticket/{reg.id}/0-[0-9a-f]{{24}}\.pdf", key)


async def test_anything_printed_changing_draws_it_again(session, world, bucket, drawn):
    reg = await _reg(session, world)
    await tickets.ticket_document(session, reg)
    reg.full_name = "Ananya R. Iyer"
    await session.commit()
    again = await tickets.ticket_document(session, await _reg(session, world))

    assert drawn == ["Ananya Iyer", "Ananya R. Iyer"] and b"Ananya R. Iyer" in again.content
    assert len(bucket) == 2


async def test_a_render_missing_an_image_is_not_kept(session, world, bucket, drawn, monkeypatch):
    """A ticket without its logo still goes out; it is not what the next
    download gets."""
    logo = f"{settings.S3_BASE_PATH}/templates/tickets/logos/0b6f2d6e-4a51-4b0e-9a2b-1f7c4b9d2e11.png"
    session.add(TicketTemplates(event_id=world["event"].id, audience=TemplateAudience.event_default, name="T", config={"logo_key": logo}))
    await session.commit()
    monkeypatch.setattr(objects, "get_bytes", lambda key: None)  # the bucket did not answer

    reg = await _reg(session, world)
    await tickets.ticket_document(session, reg)
    await tickets.ticket_document(session, reg)

    assert drawn == ["Ananya Iyer", "Ananya Iyer"] and bucket == {}


async def test_the_download_and_a_resend_share_the_kept_certificate(client, session, world, bucket, drawn):
    from app.api.events.certificates import certificate_document

    r = await client.get(f"/api/v1/events/certificates/{world['a'].ticket_id}/pdf")
    assert r.status_code == 200
    doc = await certificate_document(session, await _reg(session, world))

    assert doc.content == r.content and drawn == ["Ananya Iyer"]


async def test_the_bundle_streams_one_certificate_at_a_time(world, monkeypatch):
    from app.api.certificates import service

    log = []

    def job(name):
        def draw():
            log.append(f"draw {name}")
            return b"%PDF-1.4 " + name.encode(), True
        return (f"{name}.pdf", None, draw)

    chunks = []
    async for chunk in service.bundle_chunks([job("one"), job("two")]):
        log.append("sent")
        chunks.append(chunk)

    # The first certificate left before the second was drawn.
    assert log[:3] == ["draw one", "sent", "draw two"]
    z = zipfile.ZipFile(io.BytesIO(b"".join(chunks)))
    assert z.namelist() == ["one.pdf", "two.pdf"] and z.read("two.pdf") == b"%PDF-1.4 two"
    assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())


async def test_the_bundle_endpoint_streams_a_valid_archive(client, world, drawn):
    base = f"/api/v1/events/{world['event'].id}/certificates"
    await client.post(f"{base}/issue", json={"audience": "checked_in"}, headers=world["club"])
    r = await client.get(f"{base}/bundle.zip", headers=world["club"])

    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert len(names) == 2 and sorted(drawn) == ["Ananya Iyer", "Bala Krishnan"]


async def test_a_batch_of_id_cards_is_one_document_not_a_merge(client, world, monkeypatch):
    import pypdfium2

    real = pypdfium2.PdfDocument
    monkeypatch.setattr(pypdfium2, "PdfDocument", lambda *a, **k: pytest.fail("merged with pdfium"))
    r = await client.get(f"/api/v1/events/{world['event'].id}/idcards/batch.pdf", params={"scope": "all"}, headers=world["club"])
    monkeypatch.setattr(pypdfium2, "PdfDocument", real)

    assert r.status_code == 200, r.text
    assert r.headers["x-cards"] == "2" and len(real(r.content)) == 2


def test_the_key_names_the_kind_the_registration_and_the_design():
    key = doc_cache.key_for("certificate", "3f1c", 7, "abc")
    assert key == f"{settings.S3_BASE_PATH}/docs/certificate/3f1c/7-abc.pdf"
    # Links that expire are not part of what is printed.
    assert doc_cache.fingerprint({"logo_key": "k", "logo_url": "https://x?sig=1"}) == doc_cache.fingerprint({"logo_key": "k", "logo_url": "https://x?sig=2"})
