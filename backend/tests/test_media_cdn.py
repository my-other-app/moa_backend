"""The media CDN (app/core/storage/media.py): every public picture's address
is made in one place -- `<APP_MEDIA_CDN_URL>/<key>` when the setting is on,
the signed link exactly as before when it is off -- uploads get versioned
keys the CDN may keep for a year, addresses handed out before keep working,
and nothing private is ever given a CDN address, whatever asks.

Nothing here reaches S3 or the network: signing is local work (watched, not
replaced), bytes are read through a stand-in for `objects.get_bytes`."""
import asyncio
import io
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image
from pydantic import ValidationError
from sqlalchemy.orm import configure_mappers

import app.db.registry  # noqa: F401 -- every model, so every image column is seen
from app.api.agenda import service as agenda
from app.api.agenda.models import AgendaItems
from app.api.closeout.models import EventStatus
from app.api.discover import geo
from app.api.clubs.models import ClubPartners, Clubs, ClubTeamMembers
from app.api.emails import service as emails
from app.api.event_designs.router import _club_layout_prefix, _layout_prefix
from app.api.events import doc_cache, documents
from app.api.events.models import Events, EventSpeakers
from app.api.events.tickets import poster_url
from app.api.merch import service as merch
from app.api.sites.render import image_srcset, image_url
from app.api.store.models import EventStoreItems
from app.api.threads import service as threads_service
from app.api.threads.models import ClubThreads
from app.api.users.models import UserAvatars
from app.config import AppConfig, settings
from app.core.storage import bucket, fields, media, objects
from app.core.storage.fields import S3FileField, S3ImageField
from app.db.base import AbstractSQLModel
from tests.conftest import auth_header

CDN = "https://media.test"
BASE = settings.S3_BASE_PATH.rstrip("/")
HEX = "0123456789abcdef0123456789abcdef"
LEGACY = "0b6f2d6e-4a51-4b0e-9a2b-1f7c4b9d2e11"
NOW = datetime.now(timezone.utc)


def at(rest: str) -> str:
    """A key in the bucket: `rest` under the base path."""
    return f"{BASE}/{rest}"


@pytest.fixture
def cdn(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", CDN)
    return CDN


@pytest.fixture
def no_cdn(monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", "")


@pytest.fixture
def signer(monkeypatch):
    """Every signing: (key, seconds it is valid for)."""
    signed = []
    real = fields._SIGNER.generate_presigned_url

    def spy(operation, Params=None, ExpiresIn=3600, **kwargs):
        signed.append((Params["Key"], ExpiresIn))
        return real(operation, Params=Params, ExpiresIn=ExpiresIn, **kwargs)

    monkeypatch.setattr(fields._SIGNER, "generate_presigned_url", spy)
    fields._signed.clear()
    yield signed
    fields._signed.clear()


def _image_columns():
    """(model, column name, column type) for every image and file column of every model."""
    configure_mappers()
    out = []
    for mapper in AbstractSQLModel.registry.mappers:
        for column in mapper.columns:
            if isinstance(column.type, (S3ImageField, S3FileField)):
                out.append((mapper.class_, column.key, column.type))
    return out


def _public_columns():
    return [(m, c, t) for m, c, t in _image_columns() if isinstance(t, S3ImageField) and t.public]


def _private_columns():
    return [(m, c, t) for m, c, t in _image_columns() if not (isinstance(t, S3ImageField) and t.public)]


def _in(field, name: str) -> str:
    return f"{field.base_path.rstrip('/')}/{name}"


def _png(size=(40, 30)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (23, 165, 143)).save(out, "PNG")
    return out.getvalue()


# ─── The setting ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("given, kept", [
    ("", ""),
    ("  ", ""),
    ("https://media.myotherapp.in", "https://media.myotherapp.in"),
    ("https://Media.MyOtherApp.in/", "https://media.myotherapp.in"),
    ("media.myotherapp.in", "https://media.myotherapp.in"),
    ("http://localhost:8787", "http://localhost:8787"),
])
def test_the_setting_is_an_origin(given, kept):
    assert AppConfig(MEDIA_CDN_URL=given).MEDIA_CDN_URL == kept


@pytest.mark.parametrize("given", ["https://media.myotherapp.in/img", "https://media.myotherapp.in?x=1", "ftp://media.myotherapp.in", "https://"])
def test_a_setting_that_would_break_every_picture_is_refused_at_startup(given):
    with pytest.raises(ValidationError):
        AppConfig(MEDIA_CDN_URL=given)


def test_off_by_default():
    assert AppConfig().MEDIA_CDN_URL == "" and not media.enabled()


# ─── Which pictures are public ────────────────────────────────────────────


def test_every_public_column_stores_into_a_public_folder_and_no_private_one_does():
    """The column's `public=True` and the CDN's folders must agree: a public
    column outside the folders would fall back to signing (slow, not wrong);
    a private column inside them would put private files on the CDN."""
    public, private = _public_columns(), _private_columns()
    assert {m.__name__ for m, _, _ in public} >= {"Events", "EventSpeakers", "Clubs", "ClubTeamMembers", "ClubPartners", "ClubThreads", "UserAvatars"}
    for model, column, field in public:
        assert media.is_public(_in(field, f"v-{HEX}.png")), (model.__name__, column)
    for model, column, field in private:
        assert not media.is_public(_in(field, f"v-{HEX}.png")), (model.__name__, column)
        assert not media.is_public(_in(field, f"{LEGACY}.png")), (model.__name__, column)


def _private_keys():
    """Every kind of private object the code writes, by the same code that names it."""
    reg = uuid.uuid4()
    yield doc_cache.key_for("ticket", reg, 3, "a" * 24)                   # docs/ticket/...
    yield doc_cache.key_for("certificate", reg, 0, "b" * 24)              # docs/certificate/...
    yield doc_cache.key_for("idcard", reg, 1, "c" * 24)
    yield f"{_layout_prefix(7)}template-{HEX}.png"                         # a ticket's artwork
    yield f"{_layout_prefix(7)}image-{HEX}.png"                            # a signature, a logo in a design
    yield f"{_layout_prefix(7)}speaker-{HEX}.png"                          # an agenda photo: signed, never on the CDN
    yield f"{_club_layout_prefix(3)}template-{HEX}.png"
    yield f"{emails.image_prefix(club_id=3)}email-{HEX}.png"
    yield f"{emails.image_prefix(event_id=7)}email-{HEX}.png"
    yield at(f"events/7/closeout/{HEX}.pdf")
    yield at(f"events/files/{LEGACY}.pdf")
    yield at(f"events/files/{LEGACY}.png")
    yield at(f"templates/certificates/signatures/v-{HEX}.png")
    yield at(f"users/profile_pic/v-{HEX}.png")
    for _, _, field in _private_columns():
        yield _in(field, f"v-{HEX}.png")
        yield _in(field, f"v-{HEX}.thumbnail.png")
    # And keys that only look public.
    yield at("clubs/logos/../../docs/ticket/1/0-abc.png")
    yield at("clubs/logos/x/../../../docs/a.png")
    yield at(f"clubs/logos/nested/v-{HEX}.png")
    yield at(f"clubs/logos/v-{HEX}.svg")
    yield at(f"clubs/logos/v-{HEX}.pdf")
    yield at(f"clubs/logos/v-{HEX}.html")
    yield at(f"clubs/logos/v-{HEX}.png.pdf")
    yield at(f"Clubs/Logos/v-{HEX}.png")
    yield at(f"events/seven/merch/v-{HEX}.png")
    yield at(f"events/7/merch/../layout/template-{HEX}.png")
    yield f"elsewhere/clubs/logos/v-{HEX}.png"                              # under another base path
    yield f"/organizations/logos/v-{HEX}.png"


def test_the_private_prefixes_listed():
    """The list above is what this file holds the CDN to; it must reach every private writer."""
    keys = list(_private_keys())
    for needle in ("/docs/ticket/", "/docs/certificate/", "/layout/", "/email/", "/closeout/", "/events/files/", "/templates/", "/users/profile_pic/", "/organizations/logos/"):
        assert any(needle in k for k in keys), needle


@pytest.mark.parametrize("key", list(_private_keys()))
def test_a_private_key_is_never_on_the_cdn(cdn, signer, key):
    assert not media.is_public(key)
    assert media.cdn_url(key) is None
    # The builder falls back to the signed link it always had.
    url = media.public_url(key)
    assert not url.startswith(CDN) and signer == [(key, 7 * 24 * 3600)]
    assert objects.photo_url(key).startswith("https://") and not objects.photo_url(key).startswith(CDN)
    # And an address on the CDN's host naming it is not believed.
    rest = media.relative(key) or key.lstrip("/")
    assert media.key_from_url(f"{CDN}/{rest}") is None
    assert bucket.key_from_url(f"{CDN}/{rest}") is None


@pytest.mark.parametrize("model, column, field", _private_columns(), ids=lambda v: getattr(v, "__name__", None) or (v if isinstance(v, str) else ""))
def test_a_private_column_reads_back_signed_with_the_cdn_on(cdn, model, column, field):
    value = field.process_result_value(_in(field, f"v-{HEX}.png"), None)
    links = list(value.values()) if isinstance(value, dict) else [str(value)]
    assert links and not any(link.startswith(CDN) for link in links)


# ─── Public pictures, on and off ──────────────────────────────────────────


@pytest.mark.parametrize("model, column, field", _public_columns(), ids=lambda v: getattr(v, "__name__", None) or (v if isinstance(v, str) else ""))
def test_a_public_column_is_on_the_cdn_every_size_of_it_and_signs_nothing(cdn, signer, model, column, field):
    key = _in(field, f"v-{HEX}.png")
    image = field.process_result_value(key, None)
    rest = media.relative(key)
    assert image["original"] == f"{CDN}/{rest}"
    for size in field.variations:
        assert image[size] == f"{CDN}/{rest[:-4]}.{size}.png"
    assert signer == []
    assert image.widths == {k: v["width"] for k, v in field.variations.items()}


@pytest.mark.parametrize("model, column, field", _public_columns(), ids=lambda v: getattr(v, "__name__", None) or (v if isinstance(v, str) else ""))
def test_a_public_column_is_signed_for_a_week_as_before_with_the_cdn_off(no_cdn, signer, model, column, field):
    image = field.process_result_value(_in(field, f"v-{HEX}.png"), None)
    assert all("amazonaws.com" in link for link in image.values())
    assert {s for _, s in signer} == {7 * 24 * 3600} and len(signer) == len(image)


def test_a_picture_without_sizes_of_its_own_points_every_size_at_the_original(cdn):
    field = Events.__table__.c.poster.type
    image = field.process_result_value(at(f"clubs/threads/{LEGACY}.png"), None)
    assert set(image.values()) == {f"{CDN}/clubs/threads/{LEGACY}.png"} and image.widths == {}


def test_srcset_only_on_the_cdn(monkeypatch):
    field = Events.__table__.c.poster.type
    key = _in(field, f"v-{HEX}.png")
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", "")
    assert media.srcset(field.process_result_value(key, None)) is None
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", CDN)
    image = field.process_result_value(key, None)
    assert media.srcset(image) == (
        f"{CDN}/clubs/logos/v-{HEX}.thumbnail.png 150w, {CDN}/clubs/logos/v-{HEX}.medium.png 500w, {CDN}/clubs/logos/v-{HEX}.large.png 800w"
    )
    # A picture without sizes of its own has no srcset to give.
    assert media.srcset(field.process_result_value(at(f"elsewhere/{LEGACY}.png"), None)) is None


# ─── Versioned keys ───────────────────────────────────────────────────────


def test_an_upload_through_a_column_gets_a_versioned_key_and_so_do_its_sizes(fake_s3):
    field = Clubs.__table__.c.logo.type
    key = field.store(field.prepare(_png((900, 700))))
    assert re.fullmatch(rf"{re.escape(field.base_path.rstrip('/'))}/v-[0-9a-f]{{32}}\.png", key)
    assert fake_s3[0] == key and len(fake_s3) == 1 + len(field.variations)
    for written in fake_s3:
        assert media.is_versioned(written) and media.is_public(written)
    assert {w.rsplit(".", 2)[1] for w in fake_s3[1:]} == set(field.variations)
    # The same bytes again: a new key, never the old one written over.
    again = field.store(field.prepare(_png((900, 700))))
    assert again != key


async def test_saving_a_row_with_an_upload_stores_it_under_a_versioned_key(session, fake_s3, make_club):
    _, club = await make_club(name="Versioned Logos", email="versioned@example.com")
    club.cover = {"bytes": _png((1200, 400)), "filename": "cover.png"}
    await session.flush()
    stored = fields.stored_key(club.cover)
    assert media.is_versioned(stored) and "/clubs/covers/v-" in stored


def test_a_store_photo_gets_a_versioned_key(cdn, monkeypatch):
    put = []
    monkeypatch.setattr(objects, "put_bytes", lambda key, data, content_type: put.append((key, content_type)))
    out = merch.store_photo(5, _png())
    assert re.fullmatch(rf"{re.escape(BASE)}/events/5/merch/v-[0-9a-f]{{32}}\.png", out["key"])
    assert put == [(out["key"], "image/png")] and media.is_public(out["key"])
    assert out["url"] == f"{CDN}/events/5/merch/{out['key'].rsplit('/', 1)[1]}"


def test_versioned_and_legacy_names_are_told_apart():
    assert media.is_versioned(at(f"clubs/logos/v-{HEX}.png"))
    assert media.is_versioned(at(f"clubs/logos/v-{HEX}.thumbnail.png"))
    assert media.is_versioned(f"v-{HEX}.jpeg")
    for legacy in (f"{LEGACY}.png", f"{LEGACY}.medium.png", f"{HEX}.png", f"image-{HEX}.png", f"v-{HEX[:31]}.png", f"v-{HEX.upper()}.png", f"v-{HEX}.svg"):
        assert not media.is_versioned(at(f"clubs/logos/{legacy}")), legacy
    assert re.fullmatch(r"v-[0-9a-f]{32}\.webp", media.versioned_name("webp"))


# ─── What was handed out before keeps working ─────────────────────────────


def test_a_legacy_key_in_a_public_folder_is_on_the_cdn_too(cdn):
    """Old uploads (uuid names) are served by the CDN with a short life; the
    Worker tells them from versioned ones by the name."""
    assert media.cdn_url(at(f"clubs/logos/{LEGACY}.medium.png")) == f"{CDN}/clubs/logos/{LEGACY}.medium.png"
    assert media.cdn_url(at(f"events/42/merch/{HEX}.jpg")) == f"{CDN}/events/42/merch/{HEX}.jpg"


def test_a_cdn_address_and_a_signed_one_name_the_same_key(cdn):
    key = at(f"clubs/logos/v-{HEX}.large.png")
    signed = fields._SIGNER.generate_presigned_url("get_object", Params={"Bucket": settings.S3_BUCKET, "Key": key}, ExpiresIn=60)
    assert bucket.key_from_url(signed) == key
    assert bucket.key_from_url(media.cdn_url(key)) == key
    assert bucket.key_from_url(media.cdn_url(key) + "?v=2") == key
    # Another host with the same path is nobody's.
    assert bucket.key_from_url(f"https://evil.test/clubs/logos/v-{HEX}.large.png") is None


def test_with_the_cdn_off_its_addresses_are_not_known(no_cdn):
    assert media.key_from_url(f"{CDN}/clubs/logos/v-{HEX}.png") is None
    assert media.cdn_url(at(f"clubs/logos/v-{HEX}.png")) is None


def test_a_poster_on_the_cdn_is_read_by_its_key_never_over_http(cdn, monkeypatch):
    """The ticket renderer, the wallet pass and an email's inline pictures are
    handed the poster's address and read it by key: one cached copy, and a
    ticket's fingerprint (its doc cache key) the same whether the CDN is on."""
    key = at(f"clubs/logos/v-{HEX}.large.png")
    monkeypatch.setattr(objects, "get_bytes", lambda k: b"png-bytes" if k == key else None)
    monkeypatch.setattr(documents.urllib.request, "urlopen", lambda *a, **k: pytest.fail("fetched over http"))
    assert documents.fetch_bytes(media.cdn_url(key)) == b"png-bytes"
    signed = fields.signed_url(key, public=True)
    assert bucket.key_from_url(media.cdn_url(key)) == bucket.key_from_url(signed) == key


async def test_a_page_published_before_the_cdn_redirects_through_it(client, monkeypatch, make_club, session):
    """Old pages point at /sites/{slug}/i?k=...; with the CDN on that answers
    with the permanent address, kept a day. Production keys start at the
    bucket's root (no base path)."""
    _, club = await make_club(name="Old Page Club", email="oldpage@example.com")
    await session.commit()
    monkeypatch.setattr(settings, "S3_BASE_PATH", "")
    k = f"clubs/logos/{LEGACY}.medium.png"
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", "")
    r = await client.get(f"/api/v1/sites/{club.slug}/i", params={"k": k})
    assert r.status_code == 302 and "amazonaws.com" in r.headers["location"] and r.headers["cache-control"] == "public, max-age=1800"
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", CDN)
    r = await client.get(f"/api/v1/sites/{club.slug}/i", params={"k": k})
    assert r.status_code == 302 and r.headers["location"] == f"{CDN}/{k}" and r.headers["cache-control"] == "public, max-age=86400"
    # Still nothing outside the club folders, CDN or not.
    assert (await client.get(f"/api/v1/sites/{club.slug}/i", params={"k": "docs/ticket/1/0-a.png"})).status_code == 404


# ─── Every response type, with and without the setting ────────────────────


@pytest.fixture
def background(monkeypatch, session):
    """Work after a discover response (map links, view counts) runs on the
    test's own session, one piece at a time, as in tests/test_discover.py."""
    lock = asyncio.Lock()

    @asynccontextmanager
    async def scope():
        async with lock:
            yield session

    monkeypatch.setattr(geo, "session_scope", scope)
    monkeypatch.setattr(threads_service, "session_scope", scope)


@pytest.fixture
async def world(background, session, make_club, make_event, make_user, mint_token):
    owner, club = await make_club(name="Pixel Society", email="pixels@example.com")
    logo = Clubs.__table__.c.logo.type
    club.logo = _in(logo, f"v-{HEX}.png")
    club.cover = _in(Clubs.__table__.c.cover.type, f"{LEGACY}.png")
    event = await make_event(club=club, created_by=owner, name="Pixel Night", event_datetime=NOW + timedelta(days=5), lifecycle=EventStatus.published, merch_enabled=True)
    event.poster = _in(Events.__table__.c.poster.type, f"v-{'1' * 32}.png")
    session.add(EventSpeakers(event_id=event.id, name="Dr. Rao", photo=_in(EventSpeakers.__table__.c.photo.type, f"v-{'2' * 32}.png")))
    session.add(ClubPartners(club_id=club.id, name="Acme", logo=_in(ClubPartners.__table__.c.logo.type, f"v-{'3' * 32}.png")))
    session.add(ClubTeamMembers(club_id=club.id, name="Asha", role="Lead", photo=_in(ClubTeamMembers.__table__.c.photo.type, f"v-{'4' * 32}.png")))
    session.add(ClubThreads(id=uuid.uuid4(), club_id=club.id, body="Photos from last night", image=_in(ClubThreads.__table__.c.image.type, f"v-{'5' * 32}.png")))
    session.add(UserAvatars(name="Fox", image=_in(UserAvatars.__table__.c.image.type, f"v-{'6' * 32}.png")))
    session.add(EventStoreItems(event_id=event.id, name="Tee", price=0, stock=10, photo_key=at(f"events/{event.id}/merch/v-{'7' * 32}.png")))
    await session.commit()
    # Keys were assigned as strings; read every row back as production would
    # (the test session does not expire on commit), pictures and all.
    session.expire_all()
    for row in (owner, club, event):
        await session.refresh(row)
    return dict(owner=owner, club=club, event=event, h=auth_header(await mint_token(owner)))


def _links(value) -> list:
    """Every image link in a response body."""
    if isinstance(value, dict):
        return [x for v in value.values() for x in _links(v)]
    if isinstance(value, list):
        return [x for v in value for x in _links(v)]
    if isinstance(value, str) and (value.startswith(CDN) or "amazonaws.com" in value):
        return [value]
    return []


@pytest.mark.parametrize("on", [False, True], ids=["cdn-off", "cdn-on"])
async def test_every_public_response_hands_out_cdn_addresses_only_when_on(client, monkeypatch, session, world, on):
    club_slug, event_slug, event_id = world["club"].slug, world["event"].slug, world["event"].id
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", CDN if on else "")
    # A picture's links are made when its row is read; each request reads afresh, as in production.
    session.expire_all()
    responses = {
        "event page": await client.get(f"/api/v1/discover/events/{event_slug}"),
        "club page": await client.get(f"/api/v1/discover/clubs/{club_slug}"),
        "threads": await client.get("/api/v1/discover/threads"),
        "avatars": await client.get("/api/v1/user/avatar/list"),
        "store": await client.get(f"/api/v1/events/{event_id}/store/catalog"),
    }
    for name, r in responses.items():
        assert r.status_code == 200, (name, r.text)
    want = {
        "event page": [f"v-{'1' * 32}", f"v-{'2' * 32}", f"v-{HEX}"],   # poster, speaker, the club's logo
        "club page": [f"v-{HEX}", LEGACY, f"v-{'3' * 32}", f"v-{'4' * 32}", f"v-{'5' * 32}"],  # logo, cover, partner (sponsor), team, post
        "threads": [f"v-{'5' * 32}"],
        "avatars": [f"v-{'6' * 32}"],
        "store": [f"v-{'7' * 32}"],
    }
    for name, r in responses.items():
        links = _links(r.json())
        for name_part in want[name]:
            mine = [x for x in links if name_part in x]
            assert mine, (name, name_part, links)
            if on:
                assert all(x.startswith(f"{CDN}/") and "?" not in x for x in mine), (name, mine)
            else:
                assert all("amazonaws.com" in x and "?" in x for x in mine), (name, mine)
        assert on or not any(x.startswith(CDN) for x in links), name


async def test_the_club_site_uses_the_cdn_with_srcsets_and_the_image_route_without(client, monkeypatch, world):
    h, club = world["h"], world["club"]
    await client.patch("/api/v1/clubs/site", json={"template": "campus"}, headers=h)

    monkeypatch.setattr(settings, "MEDIA_CDN_URL", "")
    await client.post("/api/v1/clubs/site/publish", headers=h)
    off = (await client.get(f"/api/v1/sites/{club.slug}")).text
    assert f"/api/v1/sites/{club.slug}/i?k=" in off and "srcset=" not in off and CDN not in off

    monkeypatch.setattr(settings, "MEDIA_CDN_URL", CDN)
    await client.post("/api/v1/clubs/site/publish", headers=h)
    on = (await client.get(f"/api/v1/sites/{club.slug}")).text
    assert "/i?k=" not in on
    # The brand mark and the poster card, each size in a srcset with how wide it is drawn.
    assert f'src="{CDN}/clubs/logos/v-{HEX}.medium.png"' in on
    assert f"{CDN}/clubs/logos/v-{HEX}.thumbnail.png 150w, {CDN}/clubs/logos/v-{HEX}.medium.png 500w, {CDN}/clubs/logos/v-{HEX}.large.png 800w" in on
    assert 'sizes="34px"' in on and 'sizes="(max-width: 720px) calc(100vw - 40px), 380px"' in on
    assert f"{CDN}/clubs/logos/v-{'1' * 32}.large.png 800w" in on  # the poster's card
    # The cover stays its original (no sizes made wide enough to stand in) and is the link preview's picture.
    assert f'<img class="cover" src="{CDN}/clubs/covers/{LEGACY}.png"' in on
    assert f'<meta property="og:image" content="{CDN}/clubs/covers/{LEGACY}.png">' in on
    assert "amazonaws.com" not in on


def test_the_site_image_helpers(monkeypatch):
    field = Clubs.__table__.c.logo.type
    logo = field.process_result_value(_in(field, f"v-{HEX}.png"), None)
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", "")
    assert image_url("https://api.test/api/v1/sites/c", logo, "medium").startswith("https://api.test/api/v1/sites/c/i?k=")
    assert image_srcset(logo) is None
    monkeypatch.setattr(settings, "MEDIA_CDN_URL", CDN)
    assert image_url("https://api.test/api/v1/sites/c", logo, "medium") == f"{CDN}/clubs/logos/v-{HEX}.medium.png"
    # A picture outside the public folders keeps the page's own route.
    other = Events.__table__.c.poster.type.process_result_value(at(f"clubs/9/layout/template-{HEX}.png"), None)
    assert image_url("https://api.test/api/v1/sites/c", other, "medium").startswith("https://api.test/api/v1/sites/c/i?k=")


async def test_emails_show_the_logo_and_poster_from_the_cdn_and_inline_them_by_key(cdn, session, world, monkeypatch):
    event = await session.get(Events, world["event"].id)
    await session.refresh(event, ["club"])
    assert emails.logo_url_of(event) == f"{CDN}/clubs/logos/v-{HEX}.medium.png"
    assert poster_url(event) == f"{CDN}/clubs/logos/v-{'1' * 32}.large.png"
    read = []
    monkeypatch.setattr(objects, "get_bytes", lambda k: read.append(k) or b"img")
    monkeypatch.setattr(documents.urllib.request, "urlopen", lambda *a, **k: pytest.fail("fetched over http"))
    assert documents.fetch_bytes(poster_url(event)) == b"img"
    assert read == [at(f"clubs/logos/v-{'1' * 32}.large.png")]


def test_an_agenda_photo_from_the_designers_folder_stays_signed_with_the_cdn_on(cdn, signer):
    """Designer uploads share a folder with ticket artwork and signatures, so
    none of it is on the CDN; the agenda's photo keeps its week-signed link."""
    key = f"{_layout_prefix(1)}speaker-{HEX}.png"
    url = agenda.item_out(AgendaItems(id=None, event_id=1, title="Keynote", speaker_photo_key=key), set())["speaker_photo_url"]
    assert not url.startswith(CDN) and signer == [(key, 7 * 24 * 3600)]
