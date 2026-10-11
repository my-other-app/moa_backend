"""Pictures in a layered design: uploaded under the event, referred to by key.

A template is the ticket's artwork; an image layer is a logo or a photo on
it. Both arrive before anything is saved and are read back by the renderer,
so what matters is that the key the upload answers with is the key the
layout may hold, that the editor gets a URL for each one, and that a layout
cannot point at an object that is not this event's.
"""
import io
from datetime import datetime, timezone

import pytest
from PIL import Image

from app.config import settings
from app.core.layout.model import TicketLayoutFields, dump
from app.core.layout.starters import ticket_starter
from app.core.storage import objects
from tests.conftest import auth_header


@pytest.fixture
def bucket(monkeypatch):
    """The three calls the designer makes on the bucket, kept in memory."""
    store: dict[str, bytes] = {}
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: store.__setitem__(key, data))
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://test-bucket/{key}?signed")
    monkeypatch.setattr(objects, "get_bytes", lambda key: store.get(key))
    return store


@pytest.fixture
async def world(session, make_club, make_event, mint_token):
    owner, club = await make_club(name="Yukthi", email="images@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Build Night",
        event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0,
        location_name="Innovation Hall",
    )
    other = await make_event(
        club=club, created_by=owner, name="Other Night",
        event_datetime=datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc), duration=2.0,
    )
    return {"event": event, "other": other, "club": auth_header(await mint_token(owner))}


def _png(width: int, height: int, color=(200, 120, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def _layout_with(template_key=None, image_key=None) -> dict:
    s = ticket_starter("classic")
    body = dump(TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"]))
    if template_key:
        body["canvas"]["templateKey"] = template_key
    if image_key:
        body["layers"].append({
            "kind": "image", "id": "logo-pic", "name": "Logo", "x": 40, "y": 40, "width": 200, "height": 100,
            "source": "upload", "imageKey": image_key,
        })
    return body


async def _upload(client, world, data: bytes, purpose="template", name="art.png", mime="image/png"):
    return await client.post(
        f"/api/v1/events/{world['event'].id}/designs/ticket/layout/images",
        params={"purpose": purpose},
        files={"file": (name, data, mime)},
        headers=world["club"],
    )


async def test_an_upload_lands_under_the_event_and_answers_with_its_key_and_size(client, world, bucket):
    r = await _upload(client, world, _png(1560, 690))

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["key"].startswith(f"{settings.S3_BASE_PATH}/events/{world['event'].id}/layout/template-")
    assert body["key"].endswith(".png")
    assert body["url"] == f"https://test-bucket/{body['key']}?signed"
    assert (body["width"], body["height"]) == (1560, 690)
    assert body["key"] in bucket


async def test_a_wide_export_is_stored_at_2400_px_so_files_stay_light(client, world, bucket):
    r = await _upload(client, world, _png(4800, 2000))

    assert r.status_code == 200, r.text
    assert (r.json()["width"], r.json()["height"]) == (2400, 1000)
    stored = Image.open(io.BytesIO(bucket[r.json()["key"]]))
    assert stored.size == (2400, 1000)


async def test_something_that_is_not_an_image_is_refused(client, world, bucket):
    r = await _upload(client, world, b"%PDF-1.4 not a picture", name="art.pdf", mime="application/pdf")

    assert r.status_code == 422, r.text
    assert not bucket


async def test_the_editor_gets_a_url_for_every_key_the_layout_holds(client, world, bucket):
    e = world["event"].id
    template = (await _upload(client, world, _png(1560, 690))).json()["key"]
    logo = (await _upload(client, world, _png(200, 100), purpose="image")).json()["key"]

    saved = await client.put(f"/api/v1/events/{e}/designs/ticket/layout", json=_layout_with(template, logo), headers=world["club"])
    assert saved.status_code == 200, saved.text
    r = await client.get(f"/api/v1/events/{e}/designs/ticket/layout", headers=world["club"])

    body = r.json()
    assert body["canvas"]["templateKey"] == template
    assert body["templateUrl"] == f"https://test-bucket/{template}?signed"
    assert body["imageUrls"] == {template: f"https://test-bucket/{template}?signed", logo: f"https://test-bucket/{logo}?signed"}
    # The canvas fills tokens with what the renderer would print.
    assert body["sampleValues"]["event_name"] == "Build Night"
    assert body["sampleValues"]["org_name"] == "Yukthi"


async def test_a_layout_cannot_point_at_another_events_upload(client, world, bucket):
    e, other = world["event"].id, world["other"].id
    foreign = f"{settings.S3_BASE_PATH}/events/{other}/layout/template-abc.png"

    r = await client.put(f"/api/v1/events/{e}/designs/ticket/layout", json=_layout_with(foreign), headers=world["club"])

    assert r.status_code == 422, r.text
    assert "canvas.templateKey" in (r.json().get("errors") or {}).get("fields", {})


async def test_the_preview_draws_the_uploaded_template_and_picture(client, world, bucket):
    e = world["event"].id
    template = (await _upload(client, world, _png(1560, 690, (10, 10, 10)))).json()["key"]
    logo = (await _upload(client, world, _png(200, 100, (250, 250, 250)), purpose="image")).json()["key"]

    r = await client.post(f"/api/v1/events/{e}/designs/ticket/layout/preview", json=_layout_with(template, logo), headers=world["club"])

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    png = Image.open(io.BytesIO(r.content)).convert("RGB")
    # The near-black template shows through where nothing is drawn on it: just
    # right of the classic starter's brand stripe, above its first line of text.
    assert png.getpixel((40, 20))[0] < 40
