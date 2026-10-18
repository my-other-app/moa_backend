"""A design's pictures are kept as keys and signed when shown.

The ticket and certificate templates kept the presigned URL an upload made
-- good for an hour -- in their config, and the HTML templates printed from
it. An hour after a design was saved, its logo, artwork and signatures were
gone from every document and from the designer.
"""
import io
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import pytest
from PIL import Image
from sqlalchemy import select

from app.api.certificate_templates.models import CertificateTemplates
from app.api.templates_audience import TemplateAudience
from app.api.ticket_templates.models import TicketTemplates
from app.config import settings
from app.core.storage import objects
from tests.conftest import auth_header


def _png(color="red") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (6, 6), color).save(buf, format="PNG")
    return buf.getvalue()


def _key(folder: str, name: str = "0b6f2d6e-4a51-4b0e-9a2b-1f7c4b9d2e11.png") -> str:
    return f"{settings.S3_BASE_PATH}/templates/{folder}/{name}"


def _expired_link(key: str) -> str:
    """How a config saved last month names its image: a link long dead."""
    return (
        f"https://{settings.S3_BUCKET}.s3.amazonaws.com/{quote(key)}"
        "?AWSAccessKeyId=AKIAOLD&Signature=abc%3D&Expires=1700000000"
    )


@pytest.fixture
async def designing(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="keys@example.com")
    event = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=datetime.now(timezone.utc) + timedelta(days=9))
    guest = await make_user(email="keys-guest@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Ananya Iyer", is_attended=True)
    await session.commit()
    return {"club": club, "event": event, "reg": reg, "h": auth_header(await mint_token(owner)), "guest": auth_header(await mint_token(guest))}


async def test_a_saved_template_keeps_its_images_as_keys(client, session, designing, fake_s3):
    r = await client.post(
        "/api/v1/ticket-templates/create",
        data={"name": "Gala", "config": "{}"},
        files={"logo": ("logo.png", _png(), "image/png")},
        headers=designing["h"],
    )

    assert r.status_code == 200, r.text
    shown = r.json()["config"]
    assert "/templates/tickets/logos/" in shown["logo_key"]
    # Shown with a link signed now, for the dashboard to draw.
    assert shown["logo_url"] and "Signature=" in shown["logo_url"] or "X-Amz-Signature=" in shown["logo_url"]
    stored = (await session.execute(select(TicketTemplates.config).where(TicketTemplates.id == r.json()["id"]))).scalar_one()
    assert stored["logo_key"] == shown["logo_key"]
    assert "logo_url" not in stored  # nothing that expires is kept


async def test_a_config_saved_with_a_dead_link_reads_its_key_and_a_fresh_one(session, designing):
    from app.api.certificate_templates.service import get_template_for_event

    key = _key("certificates/signatures")
    session.add(CertificateTemplates(
        event_id=designing["event"].id, audience=TemplateAudience.event_default, name="Old",
        config={"signature_1_url": _expired_link(key), "signature_1_name": "Dean"},
    ))
    await session.commit()

    style = await get_template_for_event(session, designing["event"].id)

    assert style["signature_1_key"] == key
    assert style["signature_1_url"] != _expired_link(key) and "1700000000" not in style["signature_1_url"]


async def test_an_html_ticket_carries_its_images_inside_it(client, session, designing, monkeypatch):
    """wkhtmltopdf gets the bytes as data: URIs, so nothing it prints can
    expire or hang on a remote fetch."""
    from app.api.events import tickets

    logo, background = _key("tickets/logos"), _key("tickets/backgrounds", "9c1d2e3f-0a1b-4c2d-8e3f-4a5b6c7d8e9f.png")
    session.add(TicketTemplates(
        event_id=designing["event"].id, audience=TemplateAudience.event_default, name="T",
        config={"logo_key": logo, "background_image_url": _expired_link(background)},
    ))
    await session.commit()
    held = {logo: _png("blue"), background: _png("green")}
    monkeypatch.setattr(objects, "get_bytes", lambda key: held.get(key))
    seen = {}

    def capture(template_path, context, options=None, template_dir=None):
        seen.update(context)
        return io.BytesIO(b"%PDF-1.4 stub")

    monkeypatch.setattr(tickets, "generate_pdf_bytes", capture)
    r = await client.get(f"/api/v1/events/tickets/{designing['reg'].ticket_id}/pdf", headers=designing["guest"])

    assert r.status_code == 200, r.text
    assert seen["style"]["logo_url"].startswith("data:image/png;base64,")
    assert seen["style"]["background_image_url"].startswith("data:image/png;base64,")


async def test_a_key_outside_the_template_folders_is_never_signed(client, designing):
    """The config comes from the dashboard. Signing whatever key it names
    would hand out a link to any object in the bucket."""
    e = designing["event"].id
    r = await client.put(
        f"/api/v1/events/{e}/designs/ticket",
        json={"config": {"logo_key": "clubs/closeout/report-123.pdf", "background_image_url": "https://evil.example/x.png"}},
        headers=designing["h"],
    )

    assert r.status_code == 200, r.text
    config = r.json()["config"]
    assert config["logo_key"] is None and config["logo_url"] is None
    assert config["background_image_key"] is None and config["background_image_url"] is None


async def test_forking_a_template_with_a_logo_copies_the_logo(client, session, designing, fake_s3):
    """The copy took the source's image column as read -- an S3Image, a dict
    without "bytes" -- and the save failed with "Invalid input format"."""
    key = _key("tickets/logos")
    club_default = TicketTemplates(club_id=designing["club"].id, is_default=True, name="Club", config={"logo_key": key}, logo=key)
    session.add(club_default)
    await session.commit()
    session.expunge(club_default)  # read back from the database, as a request would

    r = await client.put(f"/api/v1/events/{designing['event'].id}/designs/ticket", json={"config": {"bg_color": "#101014"}}, headers=designing["h"])

    assert r.status_code == 200, r.text
    assert r.json()["is_own"] is True and r.json()["config"]["logo_key"] == key
    fork = (await session.execute(select(TicketTemplates).where(TicketTemplates.event_id == designing["event"].id))).scalar_one()
    assert fork.logo.variations["original"] == key


async def test_an_artwork_upload_keeps_the_signatures_the_fork_has_no_column_for(client, session, designing, fake_s3):
    """A fork copies the logo and background columns only; its signatures
    live in the config. Syncing the columns into the config after an upload
    wiped them."""
    signature = _key("certificates/signatures")
    session.add(CertificateTemplates(
        club_id=designing["club"].id, is_default=True, name="Club",
        config={"signature_1_url": _expired_link(signature), "signature_1_name": "Dean"},
    ))
    await session.commit()

    r = await client.put(
        f"/api/v1/events/{designing['event'].id}/designs/certificate/artwork",
        files={"background_image": ("bg.png", _png(), "image/png")},
        headers=designing["h"],
    )

    assert r.status_code == 200, r.text
    config = r.json()["config"]
    assert config["signature_1_key"] == signature and config["signature_1_url"]
    assert "/templates/certificates/backgrounds/" in config["background_image_key"]
