"""A design's pictures are stored ready for print (app/core/storage/doc_art.py).

Artwork was kept as uploaded, and every PDF drawn on it carried it: a 1.36 MB
JPEG made each ticket 1.76 MB and a quarter of a second; a phone photo
printed on its side, its EXIF (and where it was taken) kept.
"""
import io
import re
import struct
import zlib
from datetime import datetime, timezone

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.api.certificate_templates.models import CertificateTemplates
from app.api.ticket_templates.models import TicketTemplates
from app.config import settings
from app.core.storage import doc_art, objects
from app.response import CustomHTTPException
from tests.conftest import auth_header

# ─── Pictures ────────────────────────────────────────────────────────────────


def _photo(w: int, h: int, seed: int = 7) -> Image.Image:
    """Detail at every scale, as a photo has (and so a JPEG that does not shrink to nothing)."""
    rng = np.random.default_rng(seed)
    out = np.zeros((h, w, 3), dtype=np.float32)
    for o in range(6):
        cells = 2 ** (o + 2)
        gw, gh = max(2, w * cells // max(w, h)), max(2, h * cells // max(w, h))
        for c in range(3):
            grid = Image.fromarray((rng.random((gh, gw)) * 255).astype(np.uint8), "L").resize((w, h), Image.Resampling.BICUBIC)
            out[:, :, c] += (np.asarray(grid, dtype=np.float32) - 128) / (1.6 ** o)
    out = (out - out.min()) / (out.max() - out.min()) * 255
    return Image.fromarray(out.astype(np.uint8), "RGB")


def _save(image: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    image.save(buf, fmt, **kw)
    return buf.getvalue()


def _sideways_jpeg() -> bytes:
    """40 x 20, red on the left, as a phone held sideways writes it (EXIF orientation 6)."""
    img = Image.new("RGB", (40, 20), "white")
    img.paste((255, 0, 0), (0, 0, 20, 20))
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x8825] = {2: (12.0, 58.0, 30.0)}  # a GPS latitude: where it was taken
    return _save(img, "JPEG", quality=95, exif=exif.tobytes())


def _soft_logo(w: int = 1200, h: int = 500) -> Image.Image:
    """Transparent around, soft (anti-aliased) edges: hundreds of colours."""
    big = Image.new("RGBA", (w * 2, h * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.ellipse((60, 60, h * 2 - 60, h * 2 - 60), fill=(29, 185, 160, 255))
    d.rounded_rectangle((h * 2 + 40, h * 2 * 3 // 10, w * 2 - 60, h * 2 * 7 // 10), radius=h // 5, fill=(20, 22, 20, 230))
    return big.resize((w, h), Image.Resampling.LANCZOS)


def _png_claiming(width: int, height: int) -> bytes:
    """A PNG whose header says width x height, and nothing else: tiny, and enormous once decoded."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 64)) + chunk(b"IEND", b"")


def _opened(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


# ─── The upload paths ────────────────────────────────────────────────────────
#
# First in the module, as they use the database: a module whose first test
# does not leaves its database tests on another event loop (tests/test_redis.py).


@pytest.fixture
def bucket(monkeypatch):
    """The bucket as a dict: key -> (bytes, content type)."""
    held: dict = {}
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: held.__setitem__(key, (data, content_type)))
    monkeypatch.setattr(objects, "presigned_url", lambda key, expires=3600: f"https://test-bucket/{key}?signed")
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: (held.get(key) or (None,))[0])
    return held


@pytest.fixture
def columns(monkeypatch):
    """What the template image columns PUT: key -> (bytes, content type)."""
    puts: dict = {}

    def upload_fileobj(fileobj, bucket, key, ExtraArgs=None, **kw):
        puts[key] = (fileobj.read(), (ExtraArgs or {}).get("ContentType"))

    for model in (TicketTemplates, CertificateTemplates):
        for column in model.__table__.columns:
            client = getattr(column.type, "s3_client", None)
            if client is not None:
                monkeypatch.setattr(client, "upload_fileobj", upload_fileobj)
                monkeypatch.setattr(client, "generate_presigned_url", lambda op, Params=None, **kw: f"https://test-bucket.s3.amazonaws.com/{(Params or {}).get('Key')}")
    return puts


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Yukthi", email="doc-art@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night",
                             event_datetime=datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc), duration=3.0)
    guest = await make_user(email="doc-art-guest@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Ananya Iyer", is_attended=True)
    await session.commit()
    return {"event": event, "club": club, "reg": reg, "h": auth_header(await mint_token(owner))}


async def test_the_designer_stores_an_upload_ready_for_print(client, world, bucket):
    raw = _save(_photo(3200, 2400), "JPEG", quality=96, exif=Image.Exif().tobytes())
    e = world["event"].id

    r = await client.post(f"/api/v1/events/{e}/designs/ticket/layout/images", params={"purpose": "template"},
                          files={"file": ("poster.jpg", raw, "image/jpeg")}, headers=world["h"])

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"key", "url", "width", "height"}  # the answer's shape, as before
    assert re.fullmatch(rf"{re.escape(settings.S3_BASE_PATH)}/events/{e}/layout/template-[0-9a-f]{{32}}\.jpg", body["key"])
    data, content_type = bucket[body["key"]]
    assert content_type == "image/jpeg" and len(data) < len(raw) / 3
    assert (body["width"], body["height"]) == _opened(data).size == (2400, 1800)


async def test_the_designer_turns_a_phone_photo_upright(client, world, bucket):
    r = await client.post(f"/api/v1/clubs/designs/badge/layout/images", params={"purpose": "image"},
                          files={"file": ("me.jpg", _sideways_jpeg(), "image/jpeg")}, headers=world["h"])

    assert r.status_code == 200, r.text
    assert (r.json()["width"], r.json()["height"]) == (20, 40)
    assert _opened(bucket[r.json()["key"]][0]).size == (20, 40)


async def test_the_designer_refuses_a_bomb_and_stores_nothing(client, world, bucket):
    r = await client.post(f"/api/v1/events/{world['event'].id}/designs/certificate/layout/images", params={"purpose": "template"},
                          files={"file": ("art.png", _png_claiming(9000, 9000), "image/png")}, headers=world["h"])

    assert r.status_code == 422 and "megapixels" in r.json()["message"]
    assert bucket == {}


async def test_a_certificates_artwork_and_signature_are_stored_ready_for_print(client, world, columns):
    background = _save(_photo(3508, 2480).convert("RGBA"), "PNG")  # an A4 export at 300 dpi, opaque
    signature = _save(_soft_logo(900, 300), "PNG")

    r = await client.put(
        f"/api/v1/events/{world['event'].id}/designs/certificate/artwork",
        files={"background_image": ("bg.png", background, "image/png"), "signature_1": ("sig.png", signature, "image/png")},
        headers=world["h"],
    )

    assert r.status_code == 200, r.text
    config = r.json()["config"]
    bg_key, sig_key = config["background_image_key"], config["signature_1_key"]
    assert re.search(r"/templates/certificates/backgrounds/v-[0-9a-f]{32}\.jpeg$", bg_key)
    assert re.search(r"/templates/certificates/signatures/v-[0-9a-f]{32}\.png$", sig_key)
    bg, bg_type = columns[bg_key]
    assert bg_type == "image/jpeg" and _opened(bg).size == (2400, 1697) and len(bg) < len(background) / 10
    sig, sig_type = columns[sig_key]
    assert sig_type == "image/png" and _opened(sig).convert("RGBA").getchannel("A").getextrema() == (0, 255)


async def test_a_ticket_templates_logo_is_stored_ready_for_print(client, world, columns):
    r = await client.post(
        "/api/v1/ticket-templates/create",
        data={"name": "Gala", "config": "{}"},
        files={"logo": ("logo.jpg", _sideways_jpeg(), "image/jpeg")},
        headers=world["h"],
    )

    assert r.status_code == 200, r.text
    data, _ = columns[r.json()["config"]["logo_key"]]
    assert _opened(data).size == (20, 40) and not _opened(data).getexif()


# ─── normalise ───────────────────────────────────────────────────────────────


def test_a_large_jpeg_is_sized_for_print_and_compressed():
    raw = _save(_photo(3600, 2700), "JPEG", quality=97)

    art = doc_art.normalise(raw, max_bytes=0)

    out = _opened(art.data)
    assert art.format == "JPEG" and out.format == "JPEG" and art.content_type == "image/jpeg"
    assert (art.width, art.height) == out.size == (2400, 1800)  # the longest side at APP_DOC_ART_MAX_PX, the shape kept
    assert len(art.data) < len(raw) / 3
    assert out.info.get("progressive") or out.info.get("progression")


def test_the_longest_side_follows_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "DOC_ART_MAX_PX", 1000)
    art = doc_art.normalise(_save(_photo(1200, 1600), "JPEG", quality=90), max_bytes=0)
    assert (art.width, art.height) == (750, 1000)


def test_a_long_thin_picture_keeps_a_usable_short_side():
    """The designer's canvas is never narrower than 300 px."""
    art = doc_art.normalise(_save(Image.new("RGB", (320, 6000), (40, 80, 120)), "PNG"), max_bytes=0)
    assert (art.width, art.height) == (300, 5625)


def test_a_png_with_transparency_stays_a_png_with_it():
    logo = _soft_logo()
    raw = _save(logo, "PNG")

    art = doc_art.normalise(raw, max_bytes=0)

    out = _opened(art.data).convert("RGBA")
    assert art.format == "PNG" and art.content_type == "image/png"
    assert out.size == logo.size
    assert out.getchannel("A").getextrema() == (0, 255)
    assert out.tobytes() == logo.tobytes()  # lossless: not one pixel moved


def test_a_flat_picture_with_transparency_goes_on_an_exact_palette():
    """Quantised only when converting back gives every pixel as it was."""
    sig = Image.new("RGBA", (1600, 500), (0, 0, 0, 0))
    ImageDraw.Draw(sig).line([(100 + i * 7, 250 + (i % 40) * 4) for i in range(200)], fill=(20, 20, 60, 255), width=9)
    raw = _save(sig, "PNG")

    art = doc_art.normalise(raw, max_bytes=0)

    out = _opened(art.data)
    assert art.format == "PNG" and out.mode == "P"
    assert out.convert("RGBA").tobytes() == sig.tobytes()
    assert len(art.data) <= len(raw)


def test_a_photo_without_transparency_becomes_a_jpeg_even_from_a_png():
    photo = _photo(2000, 1200)
    opaque = photo.convert("RGBA")  # an alpha channel that is opaque everywhere is none
    raw = _save(opaque, "PNG")

    art = doc_art.normalise(raw, max_bytes=0)

    assert art.format == "JPEG" and _opened(art.data).mode == "RGB"
    assert len(art.data) < len(raw) / 4


def test_the_cameras_orientation_is_applied_and_its_metadata_left_behind():
    art = doc_art.normalise(_sideways_jpeg(), max_bytes=0)

    out = _opened(art.data).convert("RGB")
    assert out.size == (20, 40)  # upright: turned a quarter clockwise
    assert out.getpixel((10, 5))[0] > 200 and out.getpixel((10, 5))[1] < 60  # what was the left is now the top
    assert out.getpixel((10, 35)) >= (200, 200, 200)
    assert not _opened(art.data).getexif()  # no orientation, no GPS, nothing


def test_a_colour_profile_and_text_chunks_are_not_kept():
    from PIL import ImageCms, PngImagePlugin

    srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "exported by someone's laptop")
    raw = _save(_soft_logo(400, 200), "PNG", icc_profile=srgb, pnginfo=info)

    out = _opened(doc_art.normalise(raw, max_bytes=0).data)
    assert "icc_profile" not in out.info and "Comment" not in out.info


def test_a_small_jpeg_is_never_made_larger():
    """Saved again at 85, a JPEG saved at 60 grows; its own bytes, without
    their metadata, are kept instead."""
    exif = Image.Exif()
    exif[0x010F] = "SomeCam"
    raw = _save(_photo(1600, 900), "JPEG", quality=60, exif=exif.tobytes())

    art = doc_art.normalise(raw, max_bytes=0)

    assert art.format == "JPEG" and len(art.data) < len(raw)
    assert not _opened(art.data).getexif()
    assert _opened(art.data).tobytes() == _opened(raw).tobytes()  # the very same pixels


@pytest.mark.parametrize("claim", [(8000, 6000), (100_000, 100_000)])
def test_a_decompression_bomb_is_refused_before_it_is_decoded(claim, monkeypatch):
    raw = _png_claiming(*claim)
    assert len(raw) < 200
    monkeypatch.setattr(Image.Image, "load", lambda self: pytest.fail("decoded a bomb"))

    with pytest.raises(CustomHTTPException) as refused:
        doc_art.normalise(raw)
    assert refused.value.status_code == 422 and "megapixels" in refused.value.message


def test_what_is_not_a_picture_is_refused():
    # A BMP was refused here too; it is converted now (tests/test_image_formats.py).
    for raw in (b"%PDF-1.4 not a picture", _save(Image.new("RGB", (40, 40)), "PCX")):
        with pytest.raises(CustomHTTPException) as refused:
            doc_art.normalise(raw)
        assert refused.value.status_code == 422


def test_too_many_bytes_is_refused_before_anything_opens_it(monkeypatch):
    monkeypatch.setattr(doc_art.Image, "open", lambda *a, **k: pytest.fail("opened an oversized upload"))
    with pytest.raises(CustomHTTPException) as refused:
        doc_art.normalise(b"x" * 2048, max_bytes=1024)
    assert refused.value.status_code == 413
