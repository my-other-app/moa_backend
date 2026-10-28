"""An event's own sponsors and partners (docs/EVENT_PARTNERS.md): rows that
save one at a time, each kind in its own order, at most fifty an event; a
logo of their own; some copied from the club's partners; shown on the
event's public page and in the club's own event payload."""
import io
import re
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app.api.club_access import policy
from app.api.closeout.models import EventStatus
from app.api.clubs.models import ClubPartners
from app.api.event_partners.models import MAX_PER_EVENT, EventPartners
from app.api.event_partners.schemas import https_link
from app.api.security import service as security
from app.core import site_revalidate
from app.core.email import email as email_mod
from app.core.storage import objects
from tests.conftest import auth_header


def _png(size=(32, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "#17a58f").save(buf, "PNG")
    return buf.getvalue()


def _url(event, rest: str = "") -> str:
    return f"/api/v1/events/{event.id}/partners{rest}"


@pytest.fixture
async def world(session, make_club, make_event, mint_token):
    owner, club = await make_club(name="Circuit Society", email="ep-owner@example.com")
    rival_owner, rival = await make_club(name="Rhythm Collective", email="ep-rival@example.com")
    start = datetime.now(timezone.utc) + timedelta(days=5)
    event = await make_event(club=club, created_by=owner, name="Hack Night", lifecycle=EventStatus.published, event_datetime=start)
    other = await make_event(club=club, created_by=owner, name="Solder Sprint", lifecycle=EventStatus.published, event_datetime=start)
    await session.commit()
    return dict(
        owner=owner, club=club, rival=rival, event=event, other=other,
        h=auth_header(await mint_token(owner)), rival_h=auth_header(await mint_token(rival_owner)),
    )


async def _add(client, world, kind, name, **kw):
    r = await client.post(_url(world["event"]), json={"kind": kind, "name": name, **kw}, headers=world["h"])
    assert r.status_code == 201, r.text
    return r.json()


# ─── Rows ────────────────────────────────────────────────────────────────


async def test_rows_are_made_listed_sponsors_first_and_corrected(client, world):
    h, event = world["h"], world["event"]
    assert (await client.get(_url(event), headers=h)).json() == []

    media = await _add(client, world, "partner", "  Radio City ", label=" Media partner ", link="https://radiocity.example/")
    assert media == {
        "id": media["id"], "kind": "partner", "name": "Radio City", "label": "Media partner",
        "link": "https://radiocity.example/", "position": 0, "logo": None, "club_partner_id": None,
    }
    title = await _add(client, world, "sponsor", "Kinetic Labs", label="Title sponsor")
    gold = await _add(client, world, "sponsor", "Arc Foundry", label="")
    venue = await _add(client, world, "partner", "COEP Makerspace", label="Venue partner")
    assert (title["position"], gold["position"], venue["position"]) == (0, 1, 1)
    assert gold["label"] is None and gold["link"] is None

    listed = (await client.get(_url(event), headers=h)).json()
    assert [(p["kind"], p["name"]) for p in listed] == [
        ("sponsor", "Kinetic Labs"), ("sponsor", "Arc Foundry"), ("partner", "Radio City"), ("partner", "COEP Makerspace"),
    ]

    # Only what is sent changes; an empty label or link clears it.
    fixed = await client.patch(_url(event, f"/{media['id']}"), json={"label": "", "link": None}, headers=h)
    assert fixed.status_code == 200, fixed.text
    assert (fixed.json()["name"], fixed.json()["label"], fixed.json()["link"]) == ("Radio City", None, None)
    renamed = await client.patch(_url(event, f"/{gold['id']}"), json={"name": "Arc Foundry Ltd", "label": "Gold sponsor"}, headers=h)
    assert (renamed.json()["name"], renamed.json()["label"]) == ("Arc Foundry Ltd", "Gold sponsor")

    # A new kind puts it at the end of that kind.
    moved = await client.patch(_url(event, f"/{media['id']}"), json={"kind": "sponsor"}, headers=h)
    assert (moved.json()["kind"], moved.json()["position"]) == ("sponsor", 2)
    assert [p["name"] for p in (await client.get(_url(event), headers=h)).json()] == ["Kinetic Labs", "Arc Foundry Ltd", "Radio City", "COEP Makerspace"]

    # Removed: gone from the list, and not there to remove again.
    assert (await client.delete(_url(event, f"/{title['id']}"), headers=h)).json() == {"deleted": True}
    assert [p["name"] for p in (await client.get(_url(event), headers=h)).json()] == ["Arc Foundry Ltd", "Radio City", "COEP Makerspace"]
    gone = await client.delete(_url(event, f"/{title['id']}"), headers=h)
    assert gone.status_code == 404 and gone.json()["error_code"] == "EVENT_PARTNER_NOT_FOUND"
    assert (await client.patch(_url(event, f"/{title['id']}"), json={"name": "Back"}, headers=h)).status_code == 404


@pytest.mark.parametrize("bad", [
    {"kind": "patron", "name": "X"},
    {"kind": "sponsor", "name": "   "},
    {"kind": "sponsor", "name": "x" * 101},
    {"kind": "sponsor", "name": "X", "label": "x" * 61},
    {"kind": "sponsor", "name": "X", "link": "http://plain.example"},
    {"kind": "sponsor", "name": "X", "link": "kinetic.example"},
    {"kind": "sponsor", "name": "X", "link": "javascript:alert(1)"},
    {"kind": "sponsor", "name": "X", "link": "https://no spaces.example"},
    {"kind": "sponsor", "name": "X", "link": "https://" + "a" * 500 + ".example"},
    {"kind": "sponsor"},
])
async def test_a_bad_row_is_refused_with_what_is_wrong(client, world, bad):
    r = await client.post(_url(world["event"]), json=bad, headers=world["h"])
    assert r.status_code == 422, r.text
    assert r.json()["errors"]
    assert (await client.get(_url(world["event"]), headers=world["h"])).json() == []


def test_links_are_https_with_a_host():
    assert https_link("  https://kinetic.example/x?y=1  ") == "https://kinetic.example/x?y=1"
    assert https_link("HTTPS://Kinetic.example") == "HTTPS://Kinetic.example"
    assert https_link("") is None and https_link(None) is None
    for bad in ("http://kinetic.example", "ftp://kinetic.example", "https://localhost", "https://", "//kinetic.example", "mailto:a@b.example"):
        with pytest.raises(ValueError):
            https_link(bad)


async def test_each_kind_is_put_in_its_own_order(client, world):
    h, event = world["h"], world["event"]
    a, b, c = [await _add(client, world, "sponsor", n) for n in ("A", "B", "C")]
    p = await _add(client, world, "partner", "P")
    r = await client.put(_url(event, "/order"), json={"kind": "sponsor", "ids": [c["id"], a["id"], b["id"]]}, headers=h)
    assert r.status_code == 200, r.text
    assert [(x["name"], x["position"]) for x in r.json()] == [("C", 0), ("A", 1), ("B", 2), ("P", 0)]

    # Every live row of the kind, each once, or nothing moves.
    for ids in ([c["id"], a["id"]], [c["id"], a["id"], b["id"], b["id"]], [c["id"], a["id"], b["id"], p["id"]], [c["id"], a["id"], 99999999]):
        refused = await client.put(_url(event, "/order"), json={"kind": "sponsor", "ids": ids}, headers=h)
        assert refused.status_code == 422 and refused.json()["error_code"] == "EVENT_PARTNERS_ORDER", ids
    assert [x["name"] for x in (await client.get(_url(event), headers=h)).json()] == ["C", "A", "B", "P"]

    # A removed row is not part of the order any more; a new one goes last.
    await client.delete(_url(event, f"/{a['id']}"), headers=h)
    assert (await client.put(_url(event, "/order"), json={"kind": "sponsor", "ids": [b["id"], c["id"]]}, headers=h)).status_code == 200
    d = await _add(client, world, "sponsor", "D")
    assert d["position"] == 2
    assert [x["name"] for x in (await client.get(_url(event), headers=h)).json()] == ["B", "C", "D", "P"]


async def test_at_most_fifty_an_event(client, session, world):
    event = world["event"]
    session.add_all([EventPartners(event_id=event.id, kind="sponsor" if i % 2 else "partner", name=f"P{i}", position=i) for i in range(MAX_PER_EVENT - 1)])
    session.add(EventPartners(event_id=event.id, kind="sponsor", name="Removed", is_deleted=True))
    await session.commit()
    await _add(client, world, "sponsor", "The fiftieth")
    over = await client.post(_url(event), json={"kind": "partner", "name": "One too many"}, headers=world["h"])
    assert over.status_code == 422 and over.json()["error_code"] == "EVENT_PARTNERS_LIMIT"
    assert "50" in over.json()["message"]
    # Another event has its own fifty.
    assert (await client.post(_url(world["other"]), json={"kind": "partner", "name": "Elsewhere"}, headers=world["h"])).status_code == 201


async def test_a_logo_is_the_rows_own_square_picture(client, world, fake_s3):
    h, event = world["h"], world["event"]
    row = await _add(client, world, "sponsor", "Kinetic Labs")
    r = await client.put(_url(event, f"/{row['id']}/logo"), files={"logo": ("kinetic.png", _png((1600, 400)), "image/png")}, headers=h)
    assert r.status_code == 200, r.text
    logo = r.json()["logo"]
    assert set(logo) == {"thumbnail", "medium", "large", "original"}
    assert all("/events/partners/" in url for url in logo.values())
    assert any(re.search(r"events/partners/v-[0-9a-f]{32}\.large\.png$", k) for k in fake_s3)
    # The same row reads it back.
    assert (await client.get(_url(event), headers=h)).json()[0]["logo"] == logo

    assert (await client.put(_url(event, f"/{row['id']}/logo"), files={"logo": ("x.txt", b"no", "text/plain")}, headers=h)).status_code == 422
    other = await client.put(_url(world["other"], f"/{row['id']}/logo"), files={"logo": ("k.png", _png(), "image/png")}, headers=h)
    assert other.status_code == 404 and other.json()["error_code"] == "EVENT_PARTNER_NOT_FOUND"


def test_the_logo_is_never_cropped():
    """Each size is the whole picture fitted inside its square box."""
    from app.core.storage.fields import prepare_image

    column = EventPartners.__table__.c.logo.type
    made = prepare_image(_png((1600, 400)), max_size=column.max_size, allowed=column.allowed_extensions, variations=column.variations)
    sizes = {name: Image.open(io.BytesIO(data)).size for name, data in made.variants.items()}
    assert sizes == {"thumbnail": (150, 38), "medium": (400, 100), "large": (1080, 270)}


# ─── From the club's partners ────────────────────────────────────────────


@pytest.fixture
async def club_partners(session, world):
    club = world["club"]
    kinetic = ClubPartners(club_id=club.id, name="Kinetic Labs", role="Industry partner", sort_order=0, logo="clubs/partners/v-0123456789abcdef0123456789abcdef.png")
    gdg = ClubPartners(club_id=club.id, name="GDG Pune", role=None, sort_order=1)
    long_role = ClubPartners(club_id=club.id, name="Long Role Co", role="x" * 80, sort_order=2)
    left = ClubPartners(club_id=club.id, name="Left Already", sort_order=3, is_deleted=True)
    theirs = ClubPartners(club_id=world["rival"].id, name="Their Sponsor")
    session.add_all([kinetic, gdg, long_role, left, theirs])
    await session.commit()
    return dict(kinetic=kinetic, gdg=gdg, long_role=long_role, left=left, theirs=theirs)


async def test_club_partners_are_copied_with_a_logo_of_their_own(client, session, world, club_partners, fake_s3, monkeypatch):
    h, event = world["h"], world["event"]
    kinetic, gdg, long_role = club_partners["kinetic"], club_partners["gdg"], club_partners["long_role"]
    reads = []
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: (reads.append(key), _png((300, 300)))[1])

    await _add(client, world, "partner", "Already There")
    r = await client.post(_url(event, "/from-club"), json={"kind": "partner", "club_partner_ids": [gdg.id, kinetic.id, long_role.id]}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["skipped"] == []
    added = body["added"]
    assert [(p["name"], p["label"], p["link"], p["position"], p["club_partner_id"]) for p in added] == [
        ("GDG Pune", None, None, 1, gdg.id),
        ("Kinetic Labs", "Industry partner", None, 2, kinetic.id),
        ("Long Role Co", "x" * 60, None, 3, long_role.id),
    ]
    # Only the one with a logo was read, and it is stored as the event's own file.
    assert reads == ["clubs/partners/v-0123456789abcdef0123456789abcdef.png"]
    assert added[0]["logo"] is None
    assert all("/events/partners/" in url for url in added[1]["logo"].values())
    copied = await session.get(EventPartners, added[1]["id"])
    await session.refresh(copied)
    from app.core.storage.fields import stored_key

    assert stored_key(copied.logo).startswith("events/partners/") or "/events/partners/" in stored_key(copied.logo)

    # Asked again (with one new): what is already on the event is skipped, of either kind.
    again = await client.post(_url(event, "/from-club"), json={"kind": "sponsor", "club_partner_ids": [kinetic.id, kinetic.id, gdg.id]}, headers=h)
    assert again.status_code == 201 and again.json() == {"added": [], "skipped": [kinetic.id, gdg.id]}

    # The copy stands alone: the club partner's changes and removal never reach it.
    assert (await client.patch(f"/api/v1/clubs/partners/{kinetic.id}", json={"name": "Kinetic Renamed"}, headers=h)).status_code == 200
    assert (await client.delete(f"/api/v1/clubs/partners/{kinetic.id}", headers=h)).status_code == 200
    row = next(p for p in (await client.get(_url(event), headers=h)).json() if p["id"] == added[1]["id"])
    assert row["name"] == "Kinetic Labs" and row["logo"] == added[1]["logo"]


async def test_only_the_clubs_own_live_partners_are_copied(client, world, club_partners):
    h, event = world["h"], world["event"]
    for bad in (club_partners["theirs"].id, club_partners["left"].id, 99999999):
        r = await client.post(_url(event, "/from-club"), json={"kind": "partner", "club_partner_ids": [club_partners["gdg"].id, bad]}, headers=h)
        assert r.status_code == 422 and r.json()["error_code"] == "CLUB_PARTNER_NOT_FOUND", bad
        assert str(bad) in r.json()["message"]
    assert (await client.post(_url(event, "/from-club"), json={"kind": "partner", "club_partner_ids": []}, headers=h)).status_code == 422
    assert (await client.post(_url(event, "/from-club"), json={"kind": "both", "club_partner_ids": [club_partners["gdg"].id]}, headers=h)).status_code == 422
    assert (await client.get(_url(event), headers=h)).json() == []


async def test_a_logo_that_cannot_be_read_still_adds_the_partner(client, world, club_partners, monkeypatch):
    monkeypatch.setattr(objects, "fetch", lambda key, quiet_if_missing=False: None)
    r = await client.post(_url(world["event"], "/from-club"), json={"kind": "sponsor", "club_partner_ids": [club_partners["kinetic"].id]}, headers=world["h"])
    assert r.status_code == 201, r.text
    assert [(p["name"], p["logo"]) for p in r.json()["added"]] == [("Kinetic Labs", None)]


async def test_copying_past_fifty_is_refused_whole(client, session, world, club_partners):
    event = world["event"]
    session.add_all([EventPartners(event_id=event.id, kind="sponsor", name=f"P{i}", position=i) for i in range(MAX_PER_EVENT - 1)])
    await session.commit()
    ids = [club_partners["gdg"].id, club_partners["long_role"].id]
    r = await client.post(_url(event, "/from-club"), json={"kind": "partner", "club_partner_ids": ids}, headers=world["h"])
    assert r.status_code == 422 and r.json()["error_code"] == "EVENT_PARTNERS_LIMIT"
    assert "room for 1 more" in r.json()["message"]
    assert len((await client.get(_url(event), headers=world["h"])).json()) == MAX_PER_EVENT - 1


# ─── Who may ─────────────────────────────────────────────────────────────


async def test_only_the_owning_club_or_an_admin_reaches_them(client, world, make_admin, mint_token):
    event, rival_h = world["event"], world["rival_h"]
    row = await _add(client, world, "sponsor", "Kinetic Labs")
    assert (await client.get(_url(event))).status_code == 401
    assert (await client.get(_url(event), headers=rival_h)).status_code == 403
    assert (await client.post(_url(event), json={"kind": "sponsor", "name": "Ours now"}, headers=rival_h)).status_code == 403
    assert (await client.patch(_url(event, f"/{row['id']}"), json={"name": "Ours now"}, headers=rival_h)).status_code == 403
    assert (await client.delete(_url(event, f"/{row['id']}"), headers=rival_h)).status_code == 403
    assert (await client.get("/api/v1/events/99999999/partners", headers=world["h"])).status_code == 404
    # A row of another event is not reached through this one.
    assert (await client.patch(_url(world["other"], f"/{row['id']}"), json={"name": "X"}, headers=world["h"])).status_code == 404

    admin = await make_admin(email="ep-admin@example.com")
    ah = auth_header(await mint_token(admin))
    assert (await client.post(_url(event), json={"kind": "partner", "name": "By an admin"}, headers=ah)).status_code == 201
    assert [p["name"] for p in (await client.get(_url(event), headers=ah)).json()] == ["Kinetic Labs", "By an admin"]


async def _member_session(client, world, monkeypatch, email, role) -> dict:
    sent: list[dict] = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)
    r = await client.post("/api/v1/clubs/access/invite", json={"email": email, "role": role}, headers=world["h"])
    assert r.status_code == 200, r.text
    token = re.search(r"token=([A-Za-z0-9_\-]+)", sent[-1]["text"]).group(1)
    assert (await client.post(f"/api/v1/clubs/access/invite/{token}/accept", json={"password": "Member-pass-1"})).status_code == 200
    signed = await client.post("/api/v1/auth/token", data={"username": email, "password": "Member-pass-1", "scope": "club"})
    assert signed.status_code == 200, signed.text
    return auth_header(signed.json()["access_token"])


async def test_door_staff_read_them_and_editors_change_them(client, world, monkeypatch):
    event = world["event"]
    row = await _add(client, world, "sponsor", "Kinetic Labs")
    door = await _member_session(client, world, monkeypatch, "door-ep@example.com", "door")
    assert [p["name"] for p in (await client.get(_url(event), headers=door)).json()] == ["Kinetic Labs"]
    for refused in (
        await client.post(_url(event), json={"kind": "sponsor", "name": "X"}, headers=door),
        await client.patch(_url(event, f"/{row['id']}"), json={"name": "X"}, headers=door),
        await client.delete(_url(event, f"/{row['id']}"), headers=door),
        await client.put(_url(event, "/order"), json={"kind": "sponsor", "ids": [row["id"]]}, headers=door),
    ):
        assert refused.status_code == 403 and refused.json()["error_code"] == "ROLE_FORBIDDEN"

    editor = await _member_session(client, world, monkeypatch, "editor-ep@example.com", "editor")
    r = await client.patch(_url(event, f"/{row['id']}"), json={"label": "Title sponsor"}, headers=editor)
    assert r.status_code == 200 and r.json()["label"] == "Title sponsor"


@pytest.mark.parametrize("role,method,allowed", [
    ("door", "GET", True), ("door", "POST", False), ("door", "PATCH", False), ("door", "PUT", False), ("door", "DELETE", False),
    ("editor", "GET", True), ("editor", "POST", True), ("editor", "PUT", True), ("editor", "DELETE", True),
    ("admin", "POST", True),
])
def test_the_role_rules_match_the_organisers(role, method, allowed):
    assert policy.allows(role, method, "/api/v1/events/7/partners") is allowed
    assert policy.allows(role, method, "/api/v1/events/7/partners/3/logo") is allowed
    # The same rules as the event's organisers: its setup, not money or the club's profile.
    assert policy.allows(role, "PUT", "/api/v1/events/7/partners/order") is policy.allows(role, "PUT", "/api/v1/events/7/organisers")


# ─── Where they show ─────────────────────────────────────────────────────


async def test_the_event_payloads_carry_both_lists(client, session, world, fake_s3):
    h, event = world["h"], world["event"]
    info = (await client.get(f"/api/v1/events/info/{event.id}", headers=h)).json()
    assert info["sponsors"] == [] and info["partners"] == []

    title = await _add(client, world, "sponsor", "Kinetic Labs", label="Title sponsor", link="https://kinetic.example")
    await client.put(_url(event, f"/{title['id']}/logo"), files={"logo": ("k.png", _png(), "image/png")}, headers=h)
    await _add(client, world, "sponsor", "Arc Foundry")
    media = await _add(client, world, "partner", "Radio City", label="Media partner")
    gone = await _add(client, world, "partner", "Withdrew")
    await client.delete(_url(event, f"/{gone['id']}"), headers=h)

    # The club's own event payload (and the admin's, and the app's older read): the rows as the dashboard has them.
    info = (await client.get(f"/api/v1/events/info/{event.id}", headers=h)).json()
    assert [p["name"] for p in info["sponsors"]] == ["Kinetic Labs", "Arc Foundry"]
    assert [p["name"] for p in info["partners"]] == ["Radio City"]
    assert ".medium.png" in info["sponsors"][0]["logo"]["medium"] and info["sponsors"][0]["position"] == 0
    # Read without signing in too: it is the public event's.
    assert [p["name"] for p in (await client.get(f"/api/v1/events/info/{event.id}")).json()["partners"]] == ["Radio City"]

    # The public page: one logo URL each.
    page = (await client.get(f"/api/v1/discover/events/{event.slug}")).json()
    assert page["sponsors"][0] == {
        "id": title["id"], "kind": "sponsor", "name": "Kinetic Labs", "label": "Title sponsor",
        "link": "https://kinetic.example", "logo": info["sponsors"][0]["logo"]["medium"],
    }
    assert [(p["name"], p["logo"]) for p in page["sponsors"][1:]] == [("Arc Foundry", None)]
    assert page["partners"] == [{"id": media["id"], "kind": "partner", "name": "Radio City", "label": "Media partner", "link": None, "logo": None}]
    # Another event's page shows none of them.
    other = (await client.get(f"/api/v1/discover/events/{world['other'].slug}")).json()
    assert other["sponsors"] == [] and other["partners"] == []


async def test_a_change_tells_the_site_to_render_the_event_again(client, session, world, monkeypatch):
    import httpx

    got = []
    monkeypatch.setattr(site_revalidate.settings, "SITE_REVALIDATE_SECRET", "s")
    monkeypatch.setattr(site_revalidate.settings, "SITE_REVALIDATE_URL", "https://site.test/api/revalidate")
    monkeypatch.setattr(
        site_revalidate, "_http",
        lambda: httpx.Client(transport=httpx.MockTransport(lambda req: (got.append(req.content), httpx.Response(200))[1])),
    )
    site_revalidate.reset_for_tests()
    try:
        await _add(client, world, "sponsor", "Kinetic Labs")
        await site_revalidate.flush()
        told = b"".join(got).decode()
        assert world["event"].slug in told and f'"{world["event"].id}"' in told
    finally:
        site_revalidate.reset_for_tests()
