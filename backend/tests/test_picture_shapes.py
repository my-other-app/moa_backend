"""One shape for each club picture (app/core/storage/shapes.py): a logo is
square, a poster 4:5, a banner 3:1, the same in the app, on the public site,
on both dashboards and on a club's own page.

The sizes made of an upload fit each picture inside a box of its shape and
never crop it, so a poster that is not 4:5 still has every size. Posters are
stored in events/posters/ now; the ones stored with the club logos before
keep their sizes."""
import io
import re
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app.api.clubs.models import ClubPartners, Clubs
from app.api.event_partners.models import EventPartners
from app.api.events.models import Events
from app.config import settings
from app.core.storage import fields, media, shapes
from app.core.storage.fields import prepare_image
from tests.conftest import auth_header

BASE = settings.S3_BASE_PATH.rstrip("/")
HEX = "0123456789abcdef0123456789abcdef"
NOW = datetime.now(timezone.utc)

POSTER = Events.__table__.c.poster.type
LOGO = Clubs.__table__.c.logo.type
COVER = Clubs.__table__.c.cover.type


def at(rest: str) -> str:
    return f"{BASE}/{rest}" if BASE else rest


def _png(size) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (23, 165, 143)).save(out, "PNG")
    return out.getvalue()


def _sizes(column, size) -> dict:
    made = prepare_image(_png(size), max_size=column.max_size, allowed=column.allowed_extensions, variations=column.variations)
    return {name: Image.open(io.BytesIO(data)).size for name, data in made.variants.items()}


# ─── A club's own page ───────────────────────────────────────────────────


@pytest.fixture
async def campus(client, session, make_club, make_event, mint_token):
    owner, club = await make_club(name="Pixel Society", email="pixels-shapes@example.com")
    club.logo = at(f"clubs/logos/v-{HEX}.png")
    club.cover = at(f"clubs/covers/v-{HEX}.jpeg")
    soon = await make_event(club=club, created_by=owner, name="Hack Night", event_datetime=NOW + timedelta(days=5))
    soon.poster = at(f"events/posters/v-{'1' * 32}.jpeg")
    session.add(ClubPartners(club_id=club.id, name="Acme", logo=at(f"clubs/partners/v-{'3' * 32}.png")))
    await session.commit()
    # Keys were assigned as strings: read the rows back as production would, pictures and all.
    session.expire_all()
    h = auth_header(await mint_token(owner))
    await client.patch("/api/v1/clubs/site", json={"template": "campus"}, headers=h)
    await client.post("/api/v1/clubs/site/publish", headers=h)
    return club


async def test_the_club_page_frames_posters_4_5_whole_and_banners_3_1(client, campus, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", "")
    html = (await client.get(f"/api/v1/sites/{campus.slug}")).text
    # The poster: its large size, whole, over a blurred copy of the same file.
    poster = quote(at(f"events/posters/v-{'1' * 32}.large.jpeg"), safe="")
    frame = re.search(r'<span class="poster"><img class="bg" src="([^"]+)"[^>]*aria-hidden="true"[^>]*><img class="fg" src="([^"]+)"', html)
    assert frame and frame.group(1) == frame.group(2) and frame.group(1).endswith(poster)
    assert "aspect-ratio:4/5" in html and ".poster .fg{object-fit:contain}" in html
    # The banner: 3:1, above the words, at its large size.
    assert "aspect-ratio:3/1" in html and 'class="cover"' in html and "covers%2Fv-" in html and ".large.jpeg" in html
    # Logos: whole on white; one that is not square gets room around it.
    assert ".brand img,.logo img{object-fit:contain;background:#fff}" in html and "classList.add('pad')" in html


async def test_the_club_page_serves_new_posters_through_its_image_route(client, campus, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", "")
    r = await client.get(f"/api/v1/sites/{campus.slug}/i", params={"k": f"events/posters/v-{'1' * 32}.large.jpeg"}, follow_redirects=False)
    assert r.status_code == 302 and f"events/posters/v-{'1' * 32}.large.jpeg" in r.headers["location"]
    # Still nothing outside the club's pictures and posters.
    r = await client.get(f"/api/v1/sites/{campus.slug}/i", params={"k": "docs/ticket/1/a.png"}, follow_redirects=False)
    assert r.status_code == 404


# ─── The sizes ────────────────────────────────────────────────────────────


def test_a_4_5_poster_has_a_size_for_a_row_a_card_and_the_page():
    assert _sizes(POSTER, shapes.POSTER) == {"thumbnail": (240, 300), "medium": (540, 675), "large": (1080, 1350)}


def test_a_poster_of_another_shape_is_never_cropped():
    # Wide: each size is the whole picture, as wide as its box allows.
    assert _sizes(POSTER, (1600, 900)) == {"thumbnail": (240, 135), "medium": (540, 304), "large": (1080, 608)}
    # A story (9:16): as tall as its box allows.
    assert _sizes(POSTER, (1080, 1920)) == {"thumbnail": (169, 300), "medium": (380, 675), "large": (759, 1350)}


def test_a_logo_has_sizes_up_to_the_1080_asked_for():
    assert _sizes(LOGO, shapes.LOGO) == {"thumbnail": (150, 150), "medium": (500, 500), "large": (1080, 1080)}
    # A wordmark keeps its shape: the clients show it whole, on white.
    assert _sizes(LOGO, (1330, 300)) == {"thumbnail": (150, 34), "medium": (500, 113), "large": (1080, 244)}


def test_a_banner_is_3_1_and_its_large_size_is_twice_the_asked_for_size():
    assert _sizes(COVER, shapes.COVER) == {"medium": (1500, 500), "large": (1500, 500)}  # never enlarged
    assert _sizes(COVER, (3000, 1000)) == {"medium": (1500, 500), "large": (3000, 1000)}
    # No thumbnail: a banner never had one, and a client asking for one of an
    # older banner would get a link to nothing.
    assert set(COVER.variations) == {"medium", "large"}


def test_every_column_keeps_the_size_names_the_clients_read():
    assert set(POSTER.variations) == set(LOGO.variations) == {"thumbnail", "medium", "large"}
    assert EventPartners.__table__.c.logo.type.variations == shapes.LOGO_SIZES
    # Partner and team pictures are drawn at 28-88 px: unchanged, and no new size names.
    assert set(ClubPartners.__table__.c.logo.type.variations) == {"thumbnail", "medium"}


# ─── Where posters are kept ──────────────────────────────────────────────


@pytest.fixture
def stored(monkeypatch):
    puts: dict = {}

    def upload_fileobj(fileobj, bucket, key, **kwargs):
        puts[key] = fileobj.read()

    monkeypatch.setattr(POSTER.s3_client, "upload_fileobj", upload_fileobj)
    return puts


def test_a_new_poster_is_stored_in_events_posters_with_every_size(stored):
    key = POSTER.upload({"bytes": _png((1080, 1350)), "filename": "poster.png"})
    assert key.startswith(at("events/posters/v-")) and media.is_versioned(key) and media.is_public(key)
    for name in ("thumbnail", "medium", "large"):
        assert fields.variant_key(key, name) in stored


def test_a_poster_stored_with_the_logos_before_keeps_its_sizes(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", "https://media.test")
    old = POSTER.process_result_value(at(f"clubs/logos/v-{HEX}.jpeg"), None)
    assert old["medium"] == f"https://media.test/clubs/logos/v-{HEX}.medium.jpeg"
    assert old.widths == {"thumbnail": 240, "medium": 540, "large": 1080}
    new = POSTER.process_result_value(at(f"events/posters/v-{HEX}.jpeg"), None)
    assert new["large"] == f"https://media.test/events/posters/v-{HEX}.large.jpeg"
    # A picture from any other folder has no sizes of its own, as before.
    other = POSTER.process_result_value(at(f"clubs/threads/v-{HEX}.jpeg"), None)
    assert set(other.values()) == {f"https://media.test/clubs/threads/v-{HEX}.jpeg"}


def test_the_moved_field_without_earlier_folders_is_an_image_field():
    field = shapes.MovedImageField(upload_to="x/", variations={"medium": {"width": 2, "height": 2}})
    assert field.earlier == () and field.has_variants("x/a.png") and not field.has_variants("y/a.png")
