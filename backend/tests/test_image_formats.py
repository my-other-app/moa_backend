"""Pictures in the formats clubs actually have.

Only JPEG, PNG, WebP and GIF were taken. An iPhone photo picked on a Mac or
in Chrome is a HEIC, many downloads are AVIF, scanners and old exports make
BMP and TIFF: each was refused with "Use PNG, JPEG or WebP", and a club
was left to find out how to re-save a logo or a poster. They are converted
now on every path that takes a picture: the image columns (logo, cover,
poster, team, partners, posts), print art, email pictures, product photos
and the AI draft's poster. SVG and PDF are still refused (the server never
draws an SVG it was sent), saying what to do instead.

Every file here is made by the test, in the format it is named for.
"""
import io
import time

import pytest
from PIL import Image
from pillow_heif.as_plugin import HeifImageFile

from app.api.clubs.models import Clubs
from app.api.merch import service as merch
from app.core.storage import doc_art, fields, formats, images
from app.core.storage.fields import prepare_image
from app.response import CustomHTTPException
from tests.conftest import auth_header

LOGO = Clubs.logo.property.columns[0].type
ALLOWED = LOGO.allowed_extensions
WORKS = "JPG, PNG, WebP, GIF, HEIC, AVIF, BMP or TIFF"
RED, WHITE = (255, 0, 0), (255, 255, 255)

SVG = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="20" height="20" fill="red"/></svg>'
)


def _picture(fmt: str, size=(40, 20), *, alpha=False, orientation=None) -> bytes:
    """A picture whose left half is red: white on the right, or see-through
    with `alpha`. `orientation` is the camera's EXIF tag."""
    if alpha:
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        img.paste((*RED, 255), (0, 0, size[0] // 2, size[1]))
    else:
        img = Image.new("RGB", size, WHITE)
        img.paste(RED, (0, 0, size[0] // 2, size[1]))
    buf = io.BytesIO()
    extra = {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        extra["exif"] = exif.tobytes()
    img.save(buf, fmt, **extra)
    raw = buf.getvalue()
    assert Image.open(io.BytesIO(raw)).format == fmt  # really the format it says
    return raw


def _photo(fmt: str, size=(400, 300)) -> bytes:
    """A photo-like picture (a gradient: thousands of colours, nothing flat)."""
    img = Image.linear_gradient("L").resize(size).convert("RGB")
    img = Image.merge("RGB", (img.getchannel(0), img.getchannel(0).transpose(Image.Transpose.FLIP_LEFT_RIGHT), img.getchannel(0).rotate(90)))
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _near(pixel, colour, slack=12) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(pixel, colour))


def _prepare(raw: bytes):
    return prepare_image(raw, max_size=LOGO.max_size, allowed=ALLOWED, variations=LOGO.variations)


def _refusal(call) -> CustomHTTPException:
    with pytest.raises(CustomHTTPException) as refused:
        call()
    return refused.value


# ─── Through the routes ───────────────────────────────────────────────────
#
# First in the file: the suite's shared event loop needs an async test to
# run before any sync one when this file runs on its own (tests/conftest.py).


@pytest.fixture
def bucket(monkeypatch):
    """Every PUT and DELETE on the bucket client the image columns share."""
    seen = {"put": {}, "deleted": []}

    def upload_fileobj(fileobj, bucket, key, **kwargs):
        seen["put"][key] = (fileobj.read(), (kwargs.get("ExtraArgs") or {}).get("ContentType"))

    def delete_object(Bucket=None, Key=None, **kwargs):
        seen["deleted"].append(Key)
        return {}

    monkeypatch.setattr(LOGO.s3_client, "upload_fileobj", upload_fileobj)
    monkeypatch.setattr(LOGO.s3_client, "delete_object", delete_object)
    return seen


@pytest.fixture
async def owner(session, make_club, mint_token):
    user, club = await make_club(name="Formats Club", email="formats@example.com")
    await session.commit()
    return {"club": club, "user": user, "h": auth_header(await mint_token(user))}


def _originals(seen) -> list[str]:
    """The originals stored, in order: `v-<hex>.<ext>`, not `v-<hex>.<size>.<ext>`."""
    return [k for k in seen["put"] if len(k.rsplit("/", 1)[-1].split(".")) == 2]


async def _logo_url(client, h) -> str:
    me = (await client.get("/api/v1/clubs/info", headers=h)).json()
    return me["logo"]["original"]


def _name(key: str) -> str:
    """The stored file's name (`v-<hex>.<ext>`), which its link carries."""
    return key.rsplit("/", 1)[-1]


async def test_an_iphone_logo_uploads_even_when_the_browser_gives_it_no_type(client, owner, bucket):
    """Chrome on Windows sends a HEIC as application/octet-stream; the route
    refused anything not image/* (with a 500, for the logo)."""
    r = await client.put(
        "/api/v1/clubs/logo", files={"logo": ("IMG_0042.HEIC", _picture("HEIF", orientation=6), "application/octet-stream")}, headers=owner["h"],
    )
    assert r.status_code == 200, r.text

    (key,) = _originals(bucket)
    data, content_type = bucket["put"][key]
    assert key.endswith(".jpeg") and content_type == "image/jpeg"
    assert Image.open(io.BytesIO(data)).size == (20, 40)
    assert _name(key) in await _logo_url(client, owner["h"])


async def test_an_avif_cover_and_a_bmp_team_photo_upload(client, owner, bucket):
    r = await client.put("/api/v1/clubs/cover", files={"cover": ("banner.avif", _picture("AVIF", size=(120, 40), alpha=True), "image/avif")}, headers=owner["h"])
    assert r.status_code == 200, r.text
    (cover,) = _originals(bucket)
    assert cover.endswith(".png") and Image.open(io.BytesIO(bucket["put"][cover][0])).mode == "RGBA"

    member = await client.post("/api/v1/clubs/team", json={"name": "Asha", "role": "Lead"}, headers=owner["h"])
    assert member.status_code == 201, member.text
    r = await client.put(f"/api/v1/clubs/team/{member.json()['id']}/photo", files={"photo": ("asha.bmp", _picture("BMP"), "image/bmp")}, headers=owner["h"])
    assert r.status_code == 200, r.text
    assert _originals(bucket)[-1].endswith(".jpeg")


async def test_a_refused_logo_keeps_the_logo_the_club_has(client, session, owner, bucket):
    """The old logo was deleted before the new one was checked: a refused
    upload left the club naming a logo no longer in the bucket."""
    first = await client.put("/api/v1/clubs/logo", files={"logo": ("logo.png", _picture("PNG"), "image/png")}, headers=owner["h"])
    assert first.status_code == 200, first.text
    (kept,) = _originals(bucket)

    svg = await client.put("/api/v1/clubs/logo", files={"logo": ("logo.svg", SVG, "image/svg+xml")}, headers=owner["h"])
    assert svg.status_code == 422 and "Export it as a PNG" in svg.json()["message"]
    await session.rollback()  # the request's failed save, as production's session closing would
    assert bucket["deleted"] == [] and _name(kept) in await _logo_url(client, owner["h"])

    again = await client.put("/api/v1/clubs/logo", files={"logo": ("logo.heic", _picture("HEIF"), "image/heic")}, headers=owner["h"])
    assert again.status_code == 200, again.text
    # Replaced now, so the old one goes (on a worker thread).
    for _ in range(100):
        if kept in bucket["deleted"]:
            break
        time.sleep(0.01)
    assert kept in bucket["deleted"]


async def test_an_event_poster_in_avif_is_stored_as_a_jpeg(session, owner, make_event, bucket):
    event = await make_event(club=owner["club"], created_by=owner["user"], name="AVIF Night")
    event.poster = {"bytes": io.BytesIO(_picture("AVIF", size=(80, 100))), "filename": "poster.avif"}
    await session.commit()

    (key,) = _originals(bucket)
    assert key.endswith(".jpeg") and event.poster.variations["original"] == key


# ─── The image columns ────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["HEIF", "AVIF", "BMP", "TIFF"])
def test_an_opaque_picture_is_stored_as_a_jpeg(fmt):
    prepared = _prepare(_picture(fmt))

    assert prepared.ext == "jpeg" and prepared.content_type == "image/jpeg"
    assert set(prepared.variants) == set(LOGO.variations)
    for data in (prepared.original, *prepared.variants.values()):
        assert Image.open(io.BytesIO(data)).format == "JPEG"
    original = Image.open(io.BytesIO(prepared.original)).convert("RGB")
    assert original.size == (40, 20)
    assert _near(original.getpixel((3, 10)), RED) and _near(original.getpixel((36, 10)), WHITE)


@pytest.mark.parametrize("fmt", ["HEIF", "AVIF", "TIFF"])
def test_a_see_through_picture_is_stored_as_a_png_that_stays_see_through(fmt):
    prepared = _prepare(_picture(fmt, alpha=True))

    assert prepared.ext == "png" and prepared.content_type == "image/png"
    for data in (prepared.original, *prepared.variants.values()):
        stored = Image.open(io.BytesIO(data))
        assert stored.format == "PNG" and stored.mode == "RGBA"
    original = Image.open(io.BytesIO(prepared.original))
    assert original.getpixel((36, 10))[3] == 0  # the right half: still see-through
    assert original.getpixel((3, 10))[3] == 255 and _near(original.getpixel((3, 10))[:3], RED)


@pytest.mark.parametrize("fmt", ["HEIF", "AVIF"])
def test_a_photo_the_camera_turned_is_stored_upright(fmt):
    """Orientation 6: the phone was held sideways. The 40 x 20 picture is
    shown 20 x 40 with its red half on top, in the original and the variants."""
    prepared = _prepare(_picture(fmt, orientation=6))

    for data in (prepared.original, *prepared.variants.values()):
        stored = Image.open(io.BytesIO(data))
        assert stored.size == (20, 40) and stored.getexif().get(0x0112, 1) == 1
        stored = stored.convert("RGB")
        assert _near(stored.getpixel((10, 3)), RED) and _near(stored.getpixel((10, 36)), WHITE)


def test_a_heic_is_refused_for_its_pixels_from_its_header_before_it_is_decoded(monkeypatch):
    monkeypatch.setattr(fields, "MAX_IMAGE_PIXELS", 100)  # the 40 x 20 picture is 800
    monkeypatch.setattr(HeifImageFile, "load", lambda self: pytest.fail("decoded a picture with too many pixels"))

    refused = _refusal(lambda: _prepare(_picture("HEIF")))
    assert refused.status_code == 422 and "40 x 20 pixels" in refused.message


def test_a_heic_over_the_byte_limit_is_refused_before_anything_opens_it(monkeypatch):
    raw = _picture("HEIF")
    monkeypatch.setattr(fields.Image, "open", lambda *a, **k: pytest.fail("opened an oversized upload"))

    refused = _refusal(lambda: prepare_image(raw, max_size=len(raw) - 1, allowed=ALLOWED, variations={}))
    assert refused.status_code == 413


def test_what_is_refused_says_what_to_do():
    svg = _refusal(lambda: _prepare(SVG))
    assert svg.status_code == 422
    assert svg.message == "That's an SVG drawing, which can't be uploaded as it is. Export it as a PNG and upload that."

    pdf = _refusal(lambda: _prepare(b"%PDF-1.7\n%poster\n"))
    assert pdf.message == "That's a PDF, not a picture. Export the page as a PNG or JPG and upload that."

    pcx = _refusal(lambda: _prepare(_picture("PCX")))  # a picture Pillow reads, but nobody shows
    assert pcx.message == f"PCX pictures can't be used here. Upload a {WORKS} picture."

    junk = _refusal(lambda: _prepare(b"hello, not a picture"))
    assert junk.message == f"That file isn't a picture we can read. Upload a {WORKS} picture."


def test_a_part_with_no_type_may_still_be_a_picture():
    for kind in ("image/heic", "image/avif", "image/png; charset=binary", "application/octet-stream", "", None):
        assert formats.may_be_picture(kind), kind
    for kind in ("text/plain", "application/pdf", "video/mp4"):
        assert not formats.may_be_picture(kind), kind


# ─── Print art, email pictures, product photos ───────────────────────────


def test_print_art_takes_a_heic_photo_and_a_see_through_avif():
    photo = doc_art.normalise(_photo("HEIF"))
    assert photo.format == "JPEG" and Image.open(io.BytesIO(photo.data)).size == (400, 300)

    logo = doc_art.normalise(_picture("AVIF", size=(400, 300), alpha=True))
    assert logo.format == "PNG" and Image.open(io.BytesIO(logo.data)).getpixel((390, 150))[3] == 0

    svg = _refusal(lambda: doc_art.normalise(SVG))
    assert svg.status_code == 422 and "Export it as a PNG" in svg.message


@pytest.fixture
def puts(monkeypatch):
    """What the storage helpers PUT: key -> (bytes, content type)."""
    seen = {}

    def put_bytes(key, data, content_type):
        seen[key] = (data, content_type)

    from app.core.storage import objects

    monkeypatch.setattr(objects, "put_bytes", put_bytes)
    monkeypatch.setattr(objects, "presigned_url", lambda key, *a, **k: f"https://bucket.test/{key}")
    monkeypatch.setattr(objects, "photo_url", lambda key, *a, **k: f"https://bucket.test/{key}")
    return seen


def test_an_email_picture_in_heic_is_stored_as_a_jpeg_and_a_gif_as_it_came(puts):
    out = images.store_image(_picture("HEIF", size=(900, 300)), "emails/1/", max_width=600, name="email")
    assert out["key"].endswith(".jpg") and (out["width"], out["height"]) == (600, 200)
    data, content_type = puts[out["key"]]
    assert content_type == "image/jpeg" and Image.open(io.BytesIO(data)).format == "JPEG"

    gif = _picture("GIF")
    out = images.store_image(gif, "emails/1/", max_width=600, name="email")
    assert out["key"].endswith(".gif") and puts[out["key"]] == (gif, "image/gif")


def test_a_product_photo_in_avif_or_tiff_is_stored(puts):
    out = merch.product_photo("events/1/merch", _picture("AVIF", size=(1600, 1200)))
    data, content_type = puts[out["key"]]
    assert out["key"].endswith(".jpg") and content_type == "image/jpeg"
    assert Image.open(io.BytesIO(data)).size == (1200, 900)

    out = merch.product_photo("events/1/merch", _picture("TIFF", alpha=True))
    assert out["key"].endswith(".png") and Image.open(io.BytesIO(puts[out["key"]][0])).mode == "RGBA"

    pdf = _refusal(lambda: merch.product_photo("events/1/merch", b"%PDF-1.4\n"))
    assert pdf.status_code == 422 and "PDF" in pdf.message
