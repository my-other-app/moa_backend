"""Image uploads: checked before they are decoded, upright, kept as sent, and
processed off the event loop.

The image columns did all of it inside `process_bind_param`, during the
flush, on the loop: a full decode, a LANCZOS resize per variant and four
S3 PUTs. EXIF orientation was never applied, the original was re-encoded at
JPEG quality 75, nothing limited the pixels, and a variant was linked whether
or not it had ever been made.
"""
import io
import threading
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.api.clubs.models import Clubs
from app.core.layout import pool
from app.core.storage import fields
from app.core.storage.fields import S3Image, prepare_image, upload
from app.response import CustomHTTPException


def _jpeg(size=(40, 20), orientation=None, quality=95) -> bytes:
    img = Image.new("RGB", size, "white")
    img.paste((255, 0, 0), (0, 0, size[0] // 2, size[1]))  # a left half to tell rotations apart
    buf = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(buf, "JPEG", quality=quality, exif=exif.tobytes())
    else:
        img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _png(size=(30, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, (10, 20, 30, 255)).save(buf, "PNG")
    return buf.getvalue()


LOGO = Clubs.logo.property.columns[0].type


@pytest.fixture
def stored(monkeypatch):
    """Every PUT the logo column makes: key -> (bytes, thread)."""
    puts: dict = {}

    def upload_fileobj(fileobj, bucket, key, **kwargs):
        puts[key] = (fileobj.read(), threading.current_thread())

    monkeypatch.setattr(LOGO.s3_client, "upload_fileobj", upload_fileobj)
    monkeypatch.setattr(LOGO.s3_client, "delete_object", lambda **kw: {})
    return puts


@pytest.fixture
async def club(session, make_club):
    _, club = await make_club(name="Upload Club", email="uploads@example.com")
    await session.commit()
    return club


def _original(puts) -> tuple[str, bytes]:
    key = next(k for k in puts if len(k.rsplit("/", 1)[-1].split(".")) == 2)  # uuid.ext, not uuid.variant.ext
    return key, puts[key][0]


async def test_a_phone_photo_is_stored_upright(session, club, stored):
    """Orientation 6: the camera was held sideways. Every variant and the
    PDFs (whose renderer ignores EXIF) showed it on its side."""
    club.logo = {"bytes": io.BytesIO(_jpeg((40, 20), orientation=6)), "filename": "phone.jpg"}
    await session.commit()

    key, data = _original(stored)
    original = Image.open(io.BytesIO(data))
    assert original.size == (20, 40) and original.getexif().get(0x0112, 1) == 1
    for name in ("thumbnail", "medium", "large"):
        variant = Image.open(io.BytesIO(stored[fields.variant_key(key, name)][0]))
        # Upright, and never blown up past the photo's own size.
        assert variant.size == (20, 40)


async def test_an_ordinary_upload_is_kept_byte_for_byte(session, club, stored):
    """Re-encoding at JPEG's default quality 75 lost detail on every upload."""
    raw = _jpeg((60, 40), quality=97)
    club.logo = {"bytes": io.BytesIO(raw), "filename": "logo.jpg"}
    await session.commit()

    _, data = _original(stored)
    assert data == raw


async def test_the_upload_is_processed_off_the_event_loop(session, club, stored, monkeypatch):
    where = {}
    real = fields.prepare_image

    def watched(raw, **kwargs):
        where["prepare"] = threading.current_thread().name
        return real(raw, **kwargs)

    monkeypatch.setattr(fields, "prepare_image", watched)
    club.logo = {"bytes": io.BytesIO(_png()), "filename": "logo.png"}
    await session.commit()

    assert where["prepare"].startswith(pool.THREAD_PREFIX)
    assert stored and all(thread is not threading.main_thread() for _, thread in stored.values())


async def test_after_saving_the_column_reads_as_an_image(session, club, stored):
    """The session keeps objects after a commit; the upload dict stayed on the
    row, and a response built from it serialised the raw upload."""
    club.logo = {"bytes": io.BytesIO(_png()), "filename": "logo.png"}
    await session.commit()

    assert isinstance(club.logo, S3Image)
    assert club.logo.variations["original"] == _original(stored)[0]
    assert set(club.logo) == {"thumbnail", "medium", "large", "original"}


async def test_the_explicit_step_answers_with_the_key(session, club, stored):
    key = await upload(Clubs.logo, {"bytes": _png(), "filename": "logo.png"})

    assert key.startswith(LOGO.base_path) and key in stored
    assert fields.variant_key(key, "medium") in stored
    club.logo = key
    await session.commit()
    assert len(stored) == 4  # the key was assigned; nothing uploaded twice


def test_too_many_pixels_is_refused_before_decoding():
    buf = io.BytesIO()
    Image.new("1", (8000, 6000)).save(buf, "PNG")  # 48 MP in a few kilobytes

    with pytest.raises(CustomHTTPException) as refused:
        prepare_image(buf.getvalue(), max_size=10 * 1024 * 1024, allowed=["png"], variations={"thumbnail": {"width": 150, "height": 150}})
    assert refused.value.status_code == 422 and "megapixels" in refused.value.message


def test_too_many_bytes_is_refused_before_anything_opens_it(monkeypatch):
    monkeypatch.setattr(fields.Image, "open", lambda *a, **k: pytest.fail("decoded an oversized upload"))

    with pytest.raises(CustomHTTPException) as refused:
        prepare_image(b"x" * 2048, max_size=1024, allowed=["png"], variations={})
    assert refused.value.status_code == 413


def test_something_that_is_not_an_image_is_refused():
    with pytest.raises(CustomHTTPException) as refused:
        prepare_image(b"hello, not a picture", max_size=1024, allowed=["png"], variations={})
    assert refused.value.status_code == 422


async def test_a_variant_that_was_never_made_is_not_linked(session, club):
    """A key from outside the column's folder (another column's, a designer
    upload, an address) has no variants; linking them was a broken image in
    every client."""
    foreign = "events/12/layout/image-abc.png"
    club.logo = foreign
    await session.commit()
    session.expunge(club)
    read = (await session.execute(select(Clubs).where(Clubs.id == club.id))).scalar_one()

    assert read.logo["medium"] == read.logo["original"] and read.logo["thumbnail"] == read.logo["original"]
    assert read.logo.variations == {"original": foreign}

    own = f"{LOGO.base_path.rstrip('/')}/{uuid.uuid4()}.png"
    read.logo = own
    await session.commit()
    session.expunge(read)
    again = (await session.execute(select(Clubs).where(Clubs.id == club.id))).scalar_one()
    assert ".medium.png" in again.logo["medium"] and again.logo.variations["medium"] == fields.variant_key(own, "medium")
