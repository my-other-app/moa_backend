"""Every render is drawn on the render pool, never on the event loop.

Production is one process with one loop. A certificate, an ID card, a
preview or a wallet pass drawn on it held every other request in the
process -- a check-in scan, a payment webhook -- behind the drawing. These
tests stub the drawing at its lowest level, record the thread it ran on,
and say it was one of the pool's (app.core.layout.pool), not the loop's.
"""
import asyncio
import io
import os
import stat
import threading
import time
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.core.layout import pool
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import badge_starter, certificate_starter, ticket_starter
from tests.conftest import auth_header

START = datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None)


@pytest.fixture
def threads(monkeypatch):
    """Where each layered render, each HTML render and each PNG was drawn."""
    from app.api.event_designs import router as designs_router
    from app.api.events import certificates, router as events_router, tickets
    from app.core.layout import renderer

    seen: list[tuple[str, str]] = []
    real_pages = renderer.render_pdf_pages
    real_png = renderer.pdf_to_png

    def pages(inputs, **kwargs):
        seen.append(("layout", threading.current_thread().name))
        return real_pages(inputs, **kwargs)

    def png(pdf, **kwargs):
        seen.append(("png", threading.current_thread().name))
        return real_png(pdf, **kwargs)

    def html_pdf(template_path, context, options=None, template_dir=None):
        seen.append(("html-pdf", threading.current_thread().name))
        return io.BytesIO(b"%PDF-1.4 stub")

    def html_png(template_path, context, width=1400, template_dir=None):
        seen.append(("html-png", threading.current_thread().name))
        return io.BytesIO(b"\x89PNG stub")

    monkeypatch.setattr(renderer, "render_pdf_pages", pages)
    import app.api.events.documents as documents

    monkeypatch.setattr(documents, "render_pdf_pages", pages)
    monkeypatch.setattr(documents, "pdf_to_png", png)
    for module in (tickets, certificates, events_router):
        monkeypatch.setattr(module, "generate_pdf_bytes", html_pdf)
    for module in (events_router, designs_router):
        monkeypatch.setattr(module, "generate_image_bytes", html_png)
    return seen


def _on_pool(seen) -> bool:
    return bool(seen) and all(name.startswith(pool.THREAD_PREFIX) for _, name in seen)


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="pool@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0, location_name="Hall")
    user = await make_user(email="pool-guest@example.com")
    # Issued as well as checked in: a check-in is not a certificate
    # (events.certificates.availability), and the routes below draw one.
    reg = await make_registration(
        event=event, user=user, full_name="Ananya Iyer", is_attended=True,
        certificate_issued_at=datetime.now(timezone.utc),
    )
    await session.commit()
    return {"event": event, "reg": reg, "club": auth_header(await mint_token(owner)), "guest": auth_header(await mint_token(user))}


async def _loaded(session, world):
    """The registration with its event and club, as the endpoints load it."""
    from app.api.events import service

    return await service.get_ticket_details(session, ticket_id=world["reg"].ticket_id)


def _body(starter) -> dict:
    return dump(TicketLayoutFields(canvas=starter["canvas"], layers=starter["layers"], page=starter["page"]))


async def _layered(client, world, kind: str):
    starter = {"ticket": ticket_starter, "certificate": certificate_starter}[kind]("classic") if kind != "badge" else badge_starter("classic", page=None)
    r = await client.put(f"/api/v1/events/{world['event'].id}/designs/{kind}/layout", json=_body(starter), headers=world["club"])
    assert r.status_code == 200, r.text


# ─── documents ────────────────────────────────────────────────────────────


async def test_a_certificate_is_drawn_on_the_render_pool(session, world, threads):
    """certificate_document drew on the loop: sends, the post-event email's
    attachment, the bundle and the public routes all came through it."""
    from app.api.events.certificates import certificate_document

    doc = await certificate_document(session, await _loaded(session, world))

    assert doc.content.startswith(b"%PDF") and _on_pool(threads)


async def test_a_layered_certificate_and_its_png_are_drawn_on_the_pool(client, world, threads):
    await _layered(client, world, "certificate")
    r = await client.get(f"/api/v1/events/certificates/{world['reg'].ticket_id}/image")

    assert r.status_code == 200, r.text
    assert {kind for kind, _ in threads} == {"layout", "png"} and _on_pool(threads)


async def test_the_ticket_and_certificate_images_are_drawn_on_the_pool(client, world, threads):
    t = await client.get(f"/api/v1/events/tickets/{world['reg'].ticket_id}/image", headers=world["guest"])
    c = await client.get(f"/api/v1/events/certificates/{world['reg'].ticket_id}/image")
    p = await client.get(f"/api/v1/events/certificates/{world['reg'].ticket_id}/pdf")

    assert (t.status_code, c.status_code, p.status_code) == (200, 200, 200)
    assert [kind for kind, _ in threads] == ["html-png", "html-png", "html-pdf"] and _on_pool(threads)


async def test_the_designers_previews_are_drawn_on_the_pool(client, world, threads):
    e = world["event"].id
    html = await client.post(f"/api/v1/events/{e}/designs/ticket/preview", json={"config": {}}, headers=world["club"])
    layered = await client.post(f"/api/v1/events/{e}/designs/ticket/layout/preview", json=_body(ticket_starter("classic")), headers=world["club"])
    club = await client.post("/api/v1/clubs/designs/ticket/layout/preview", json=_body(ticket_starter("classic")), headers=world["club"])

    assert (html.status_code, layered.status_code, club.status_code) == (200, 200, 200)
    assert _on_pool(threads)


async def test_id_cards_one_and_a_batch_are_drawn_on_the_pool(client, world, threads):
    url = f"/api/v1/events/{world['event'].id}/idcards"
    one = await client.get(f"{url}/{world['reg'].id}.pdf", params={"record": "false"}, headers=world["club"])
    batch = await client.get(f"{url}/batch.pdf", params={"scope": "all"}, headers=world["club"])

    assert one.status_code == 200 and batch.status_code == 200, (one.text, batch.text)
    assert len(threads) == 2 and _on_pool(threads)


async def test_a_wallet_pass_is_signed_on_the_pool(client, world, monkeypatch):
    """`subprocess.run(openssl)` and a `requests.get` of the poster both ran
    on the loop."""
    from app.api.events import wallet_service

    where = {}

    def sign(manifest):
        where["sign"] = threading.current_thread().name
        return b"signature"

    monkeypatch.setattr(wallet_service, "sign_manifest", sign)
    r = await client.get(f"/api/v1/events/tickets/{world['reg'].ticket_id}/wallet.pkpass", headers=world["guest"])

    assert r.status_code == 200, r.text
    assert where["sign"].startswith(pool.THREAD_PREFIX)


# ─── the pool ─────────────────────────────────────────────────────────────


async def test_the_loop_keeps_serving_while_a_certificate_draws(session, world, monkeypatch):
    """The point of it: a slow render, and the loop ticking meanwhile."""
    from app.api.events import certificates

    def slow(template_path, context, options=None, template_dir=None):
        time.sleep(0.4)
        return io.BytesIO(b"%PDF-1.4 slow")

    monkeypatch.setattr(certificates, "generate_pdf_bytes", slow)
    reg = await _loaded(session, world)
    ticks = 0

    async def tick():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(tick())
    try:
        await certificates.certificate_document(session, reg)
    finally:
        ticker.cancel()
    assert ticks >= 15


async def test_the_pool_is_bounded_by_its_setting(monkeypatch):
    monkeypatch.setattr(settings, "RENDER_THREADS", 2)
    pool.shutdown()
    busy, most = 0, 0
    lock = threading.Lock()

    def work():
        nonlocal busy, most
        with lock:
            busy += 1
            most = max(most, busy)
        time.sleep(0.05)
        with lock:
            busy -= 1
        return threading.current_thread().name

    try:
        names = await asyncio.gather(*[pool.run(work) for _ in range(8)])
    finally:
        pool.shutdown()
    assert most == 2 and len(set(names)) == 2
    assert all(n.startswith(pool.THREAD_PREFIX) for n in names)


def test_every_pdfium_call_holds_the_process_lock(monkeypatch):
    """pypdfium2 is not thread-safe; two renders' PNGs at once could crash."""
    import pypdfium2

    from app.core.layout.renderer import blank_page_pdf, pdf_text, pdf_to_png

    real = pypdfium2.PdfDocument
    held = []

    def guarded(*args, **kwargs):
        held.append(pool.PDFIUM.held())
        return real(*args, **kwargs)

    monkeypatch.setattr(pypdfium2, "PdfDocument", guarded)
    pdf = blank_page_pdf("hello")
    assert pdf_to_png(pdf, width_px=100).startswith(b"\x89PNG")
    assert "hello" in pdf_text(pdf)
    assert held == [True, True]


def test_wkhtmltopdf_is_stopped_when_it_overruns(monkeypatch, tmp_path):
    """pdfkit waited on wkhtmltopdf with no limit at all."""
    from app.core.utils import pdf

    (tmp_path / "page.html").write_text("<p>A page that never finishes.</p>")
    fake = tmp_path / "wkhtmltopdf"
    fake.write_text("#!/bin/sh\nsleep 5\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(settings, "RENDER_TIMEOUT", 0.5)
    if hasattr(pdf, "_wkhtmltopdf"):
        pdf._wkhtmltopdf.cache_clear()

    began = time.monotonic()
    try:
        with pytest.raises(Exception) as caught:
            pdf.generate_pdf_bytes("page.html", {}, template_dir=f"{tmp_path}/")
    finally:
        if hasattr(pdf, "_wkhtmltopdf"):
            pdf._wkhtmltopdf.cache_clear()
    assert time.monotonic() - began < 3
    assert "longer than" in str(caught.value)
