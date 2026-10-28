"""Uploads kept as they came no longer carry their metadata.

A JPEG, PNG or WebP that needed no turning or shrinking was stored byte for
byte, EXIF and all: a phone photo used as a club's logo, an event's poster
or a team photo published where it was taken (EXIF's GPS block), on a link
anyone may open. The metadata is taken out losslessly now (app/core/storage
/metadata.py): the stored file decodes to exactly the same pixels and keeps
its colour profile. A picture the camera turned is turned and encoded anew,
which carries no metadata either.

Every file here is made by the test: a real EXIF block with a GPS IFD (as a
phone writes it), XMP, a comment, IPTC, text chunks and an ICC profile.
"""
import io
import struct
import threading

import pytest
from PIL import ExifTags, Image, ImageCms
from PIL.PngImagePlugin import PngInfo

from app.api.ai import draft
from app.api.clubs.models import Clubs
from app.api.merch import service as merch
from app.core.storage import images
from app.core.storage.fields import prepare_image
from app.core.storage.metadata import without_metadata
from tests.conftest import auth_header

LOGO = Clubs.logo.property.columns[0].type
ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
XMP = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description photoshop:City="Kochi"/></rdf:RDF></x:xmpmeta>'
GPS = {1: "N", 2: (9.0, 58.0, 54.0), 3: "E", 4: (76.0, 16.0, 3.0)}


def _exif(orientation=None) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "Apple"
    exif[0x0110] = "iPhone 15 Pro"
    exif[ExifTags.IFD.GPSInfo] = GPS
    if orientation:
        exif[0x0112] = orientation
    return exif.tobytes()


def _photo(size=(96, 64)) -> Image.Image:
    """Thousands of colours, so a lossy re-encode would show."""
    grey = Image.linear_gradient("L").resize(size)
    return Image.merge("RGB", (grey, grey.transpose(Image.Transpose.FLIP_LEFT_RIGHT), grey.rotate(90)))


def _jpeg(size=(96, 64), orientation=None, progressive=False) -> bytes:
    buf = io.BytesIO()
    _photo(size).save(buf, "JPEG", quality=90, exif=_exif(orientation), icc_profile=ICC, xmp=XMP, comment=b"taken at home", progressive=progressive)
    raw = buf.getvalue()
    # IPTC (Photoshop's APP13), as an editor writes it, right after the start of image.
    iptc = b"Photoshop 3.0\x00" + b"8BIM\x04\x04\x00\x00\x00\x00\x00\x10" + b"\x1c\x02\x5a\x00\x05Kochi" + b"\x00" * 6
    return raw[:2] + b"\xff\xed" + struct.pack(">H", len(iptc) + 2) + iptc + raw[2:]


def _png() -> bytes:
    info = PngInfo()
    info.add_text("Comment", "taken at home")
    info.add_itxt("XML:com.adobe.xmp", XMP.decode())
    buf = io.BytesIO()
    _photo().convert("RGBA").save(buf, "PNG", pnginfo=info, exif=_exif(), icc_profile=ICC)
    return buf.getvalue()


def _webp(lossless=False) -> bytes:
    buf = io.BytesIO()
    _photo().save(buf, "WEBP", exif=_exif(), xmp=XMP, icc_profile=ICC, lossless=lossless, quality=90)
    return buf.getvalue()


def _mpo() -> bytes:
    """A camera's JPEG with a second frame, each with its own EXIF."""
    buf = io.BytesIO()
    _photo().save(buf, "MPO", save_all=True, append_images=[_photo().rotate(180)], exif=_exif(), icc_profile=ICC)
    return buf.getvalue()


def _clean(data: bytes, *, icc: bool = True) -> Image.Image:
    """The stored file, checked to carry no GPS, EXIF, XMP, IPTC or text."""
    img = Image.open(io.BytesIO(data))
    assert not img.getexif(), "EXIF left in"
    assert not img.getexif().get_ifd(ExifTags.IFD.GPSInfo)
    assert b"Exif\x00\x00" not in data and b"eXIf" not in data and b"EXIF" not in data
    assert b"xmpmeta" not in data and b"Kochi" not in data and b"taken at home" not in data and b"Photoshop 3.0" not in data
    assert b"iPhone" not in data
    if icc:
        assert img.info.get("icc_profile") == ICC
    return img


def _same_pixels(a: bytes, b: bytes) -> bool:
    x, y = Image.open(io.BytesIO(a)), Image.open(io.BytesIO(b))
    return x.size == y.size and x.convert("RGBA").tobytes() == y.convert("RGBA").tobytes()


# ─── Through a route ──────────────────────────────────────────────────────
#
# First in the file: the suite's shared event loop needs an async test to
# run before any sync one when this file runs on its own (tests/conftest.py).


@pytest.fixture
def bucket(monkeypatch):
    seen: dict = {}

    def upload_fileobj(fileobj, bucket, key, **kwargs):
        seen[key] = (fileobj.read(), threading.current_thread())

    monkeypatch.setattr(LOGO.s3_client, "upload_fileobj", upload_fileobj)
    monkeypatch.setattr(LOGO.s3_client, "delete_object", lambda **kw: {})
    return seen


async def test_a_logo_uploaded_from_a_phone_is_published_without_where_it_was_taken(client, session, make_club, mint_token, bucket):
    user, _ = await make_club(name="GPS Club", email="gps@example.com")
    await session.commit()
    raw = _jpeg()
    r = await client.put("/api/v1/clubs/logo", files={"logo": ("IMG_0042.jpg", raw, "image/jpeg")}, headers=auth_header(await mint_token(user)))
    assert r.status_code == 200, r.text

    (key,) = [k for k in bucket if len(k.rsplit("/", 1)[-1].split(".")) == 2]
    stored = bucket[key][0]
    _clean(stored)
    assert _same_pixels(stored, raw)


# ─── The image columns: logos, covers, posters, team and profile photos ──


@pytest.mark.parametrize("make", [_jpeg, lambda: _jpeg(progressive=True), _png, _webp, lambda: _webp(lossless=True)], ids=["jpeg", "progressive-jpeg", "png", "webp", "webp-lossless"])
def test_a_kept_original_loses_its_gps_and_keeps_its_pixels_and_colour(make):
    raw = make()
    assert Image.open(io.BytesIO(raw)).getexif().get_ifd(ExifTags.IFD.GPSInfo)  # the upload has a position

    prepared = prepare_image(raw, max_size=LOGO.max_size, allowed=LOGO.allowed_extensions, variations=LOGO.variations)

    _clean(prepared.original)
    assert _same_pixels(prepared.original, raw)  # nothing re-encoded
    assert len(prepared.original) < len(raw)
    for variant in prepared.variants.values():
        _clean(variant)  # encoded anew: no metadata, the colour profile carried over


def test_with_no_variants_to_make_the_original_still_loses_its_metadata():
    raw = _jpeg()
    prepared = prepare_image(raw, max_size=LOGO.max_size, allowed=LOGO.allowed_extensions, variations={})
    _clean(prepared.original)
    assert _same_pixels(prepared.original, raw)


def test_a_photo_the_camera_turned_is_turned_before_its_metadata_goes():
    """Orientation 6 and a GPS position: the stored picture is upright (the
    tag that said so is gone, so the pixels must be turned) and has no GPS."""
    raw = _jpeg(orientation=6)
    prepared = prepare_image(raw, max_size=LOGO.max_size, allowed=LOGO.allowed_extensions, variations=LOGO.variations)
    stored = _clean(prepared.original)
    assert stored.size == (64, 96)
    # And the stripping itself refuses to drop an orientation it was not given turned.
    with pytest.raises(ValueError):
        without_metadata(raw, "JPEG")


def test_a_cameras_second_frame_and_its_exif_are_cut_off():
    raw = _mpo()
    assert Image.open(io.BytesIO(raw)).format == "MPO"
    prepared = prepare_image(raw, max_size=LOGO.max_size, allowed=LOGO.allowed_extensions, variations={})
    stored = _clean(prepared.original)
    assert stored.format == "JPEG" and getattr(stored, "n_frames", 1) == 1
    first = Image.open(io.BytesIO(raw))
    first.seek(0)
    assert stored.convert("RGB").tobytes() == first.convert("RGB").tobytes()


def test_a_file_that_does_not_parse_as_it_claims_is_encoded_anew_without_metadata(monkeypatch):
    from app.core.storage import fields

    def broken(raw, fmt):
        raise ValueError("does not parse")

    monkeypatch.setattr(fields, "without_metadata", broken)
    raw = _png()
    prepared = prepare_image(raw, max_size=LOGO.max_size, allowed=LOGO.allowed_extensions, variations={})
    _clean(prepared.original)
    assert _same_pixels(prepared.original, raw)  # a PNG encoded anew is still lossless


# ─── Email pictures, product photos, the AI draft ───────────────────────


@pytest.fixture
def puts(monkeypatch):
    seen = {}
    from app.core.storage import objects

    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: seen.__setitem__(key, data))
    monkeypatch.setattr(objects, "presigned_url", lambda key, *a, **k: f"https://bucket.test/{key}")
    monkeypatch.setattr(objects, "photo_url", lambda key, *a, **k: f"https://bucket.test/{key}")
    return seen


@pytest.mark.parametrize("make", [_jpeg, _png, _webp], ids=["jpeg", "png", "webp"])
def test_an_email_picture_and_a_product_photo_lose_their_gps(puts, make):
    raw = make()
    email = images.store_image(raw, "emails/1/", max_width=600, name="email")
    photo = merch.product_photo("events/1/merch", raw)
    for key in (email["key"], photo["key"]):
        _clean(puts[key])
        assert _same_pixels(puts[key], raw)


def test_resized_and_turned_pictures_keep_their_colour_profile(puts):
    turned = _jpeg(size=(1600, 900), orientation=6)
    out = merch.product_photo("events/1/merch", turned)
    stored = _clean(puts[out["key"]])
    assert stored.size == (900, 1600)
    out = images.store_image(_webp(), "emails/1/", max_width=40, name="email")
    _clean(puts[out["key"]])


def test_the_ai_draft_sends_a_poster_without_its_metadata():
    for make, mime in ((_jpeg, "image/jpeg"), (_png, "image/png"), (_webp, "image/webp")):
        raw = make()
        sent_mime, sent = draft.attachment(raw)
        assert sent_mime == mime
        _clean(sent)
        assert _same_pixels(sent, raw)
    # One the camera turned is drawn upright, as a JPEG with nothing else in it.
    sent_mime, sent = draft.attachment(_jpeg(orientation=6))
    assert sent_mime == "image/jpeg" and Image.open(io.BytesIO(sent)).size == (64, 96)
    assert not Image.open(io.BytesIO(sent)).getexif()
