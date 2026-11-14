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


# ─── A design's uploads: only the designers' folders are read or signed ──

HEX = "0123456789abcdef0123456789abcdef"


def _at(rest: str) -> str:
    return f"{settings.S3_BASE_PATH}/{rest}"


def _v1_layout(template_key=None, image_keys=()) -> dict:
    from app.core.layout.model import TicketLayoutFields, dump
    from tests.legacy_layouts import ticket_starter

    s = ticket_starter("classic")
    body = dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"]))
    if template_key:
        body["canvas"]["templateKey"] = template_key
    for i, key in enumerate(image_keys):
        body["layers"].append({
            "kind": "image", "id": f"pic-{i}", "name": "Picture", "x": 40, "y": 40, "width": 200, "height": 100,
            "source": "upload", "imageKey": key,
        })
    return body


def test_the_folders_a_design_and_a_product_may_name():
    from app.core.storage import bucket

    assert bucket.is_design_upload(_at(f"events/7/layout/image-{HEX}.png"))
    # A key kept under an earlier base path still reads.
    assert bucket.is_design_upload(f"older/events/7/layout/image-{HEX}.png")
    assert bucket.is_design_picture(f"older/clubs/logos/v-{HEX}.png")
    assert bucket.is_design_upload(_at(f"clubs/3/layout/template-{HEX}.jpg"))
    assert bucket.is_product_photo(_at(f"events/7/merch/v-{HEX}.png")) and bucket.is_product_photo(_at(f"clubs/3/merch/v-{HEX}.png"))
    for key in (_at("docs/ticket/1/0-abc.pdf"), _at("events/files/report.pdf"), _at(f"events/7/closeout/{HEX}.pdf"), _at("docs/ticket/x/1-a.pdf"),
                _at("events/7/layout/../../8/layout/x.png"), _at("events/7/layout/nested/x.png"), _at("events/seven/layout/x.png"),
                "docs/ticket/1/events", None, ""):
        assert not bucket.is_design_upload(key), key
        assert not bucket.is_product_photo(key), key
        assert not bucket.is_design_picture(key), key
    # What a design may draw by its address: a public picture, a template's image, an upload.
    assert bucket.is_design_picture(_at(f"clubs/logos/v-{HEX}.png"))
    assert bucket.is_design_picture(_key("tickets/logos"))
    assert bucket.is_design_picture(_at(f"events/7/layout/image-{HEX}.png"))


def test_a_product_photo_link_is_for_a_product_photo_alone(monkeypatch):
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://files.test/{key}")
    photo = _at(f"events/7/merch/v-{HEX}.png")
    assert objects.photo_url(photo)
    for key in (_at("docs/ticket/1/0-abc.pdf"), _at(f"events/7/layout/image-{HEX}.png"), _at("events/7/merch/../closeout/x.pdf")):
        assert objects.photo_url(key) is None, key


async def test_a_layout_kept_in_the_design_signs_only_the_designers_uploads(client, designing, monkeypatch):
    e = designing["event"].id
    own = _at(f"events/{e}/layout/image-{HEX}.png")
    foreign = [_at("docs/ticket/1/0-abc.pdf"), _at(f"events/{e}/closeout/{HEX}.pdf")]
    signed = []
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: signed.append(key) or f"https://files.test/{key}")
    r = await client.put(
        f"/api/v1/events/{e}/designs/ticket", json={"config": {"layout": _v1_layout(foreign[0], [own, foreign[1]])}}, headers=designing["h"],
    )
    assert r.status_code == 200, r.text
    got = await client.get(f"/api/v1/events/{e}/designs/ticket/layout", headers=designing["h"])
    assert got.status_code == 200, got.text
    assert got.json()["imageUrls"] == {own: f"https://files.test/{own}"} and got.json()["templateUrl"] is None
    assert not set(foreign) & set(signed)


def test_a_layout_draws_only_the_designers_uploads(monkeypatch):
    from app.api.events import documents

    own = _at(f"events/7/layout/image-{HEX}.png")
    foreign = [_at("docs/ticket/1/0-abc.pdf"), _at(f"events/7/closeout/{HEX}.pdf")]
    read = []
    monkeypatch.setattr(objects, "get_bytes", lambda key: read.append(key) or _png())
    style = {"layout": _v1_layout(foreign[0], [own, foreign[1]])}
    inp, _ = documents.layout_input(style, {}, "QR", title="Ticket")
    assert read == [own] and set(inp.images) == {own}
    # By address too: only a picture a design may draw is read.
    read.clear()
    assert documents.fetch_bytes(_expired_link(foreign[0])) is None
    assert documents.fetch_bytes(_expired_link(own)) == _png()
    assert read == [own]


def test_the_v2_editor_signs_only_the_designers_uploads(monkeypatch):
    from types import SimpleNamespace

    from app.api.doc_presets import layouts

    own, foreign = _at(f"clubs/3/layout/image-{HEX}.png"), _at("events/files/report.pdf")
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://files.test/{key}")
    layout = {"layers": [{"kind": "image", "source": "upload", "imageKey": k} for k in (own, foreign)]}
    links = layouts.picture_links(layout, {}, SimpleNamespace(club=None, poster=None))
    assert links["uploads"] == {own: f"https://files.test/{own}"}
