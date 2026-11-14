"""Profiles (feature-register/profiles-networking-contract.md): my profile and
its settings, handles, links, projects, the QR code, and what each viewer
sees of a public profile -- the visibility matrix, private profiles, the
404s, the vCard and the rate limits."""
from datetime import datetime, timedelta, timezone
import io

import pytest
from sqlalchemy import select

from app.api.profiles import limits, rules
from app.api.profiles.models import ProfileCodes, ProfileProjects
from app.api.reports.models import UserBlocks
from app.api.users.models import UserTypes
from tests.conftest import auth_header
from tests.profiles_world import API, person, profile_row, save


@pytest.fixture
async def world(client, session, make_user, mint_token):
    owner, oh = await person(session, make_user, mint_token, "asha", "Asha Nair", headline="Builds robots", bio="Line one\nLine two", city="Kochi", phone="+919800000001")
    viewer, vh = await person(session, make_user, mint_token, "dev", "Dev Rao")
    await session.commit()
    return dict(owner=owner, oh=oh, viewer=viewer, vh=vh, session=session)


# ─── My profile ─────────────────────────────────────────────────────────


async def test_my_profile_starts_with_the_defaults_and_says_what_is_missing(client, session, make_user, mint_token):
    # An account with no profile row yet (an email sign-up) reads the defaults.
    user = await make_user(email="fresh@example.com", username="fresh")
    await session.commit()
    r = await client.get(f"{API}/me/profile", headers=auth_header(await mint_token(user)))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["handle"] == "fresh" and body["is_me"] is True and body["relation"] is None
    assert body["settings"] == {
        "visibility": {"links": "everyone", "projects": "everyone", "achievements": "everyone", "interests": "everyone", "org": "everyone"},
        "share_email": "nobody", "share_phone": "nobody", "is_private": False, "indexable": False, "notify_saves": True,
        "recap_emails": True, "scanning_enabled": True, "show_events": False, "show_certificates": True, "show_talks": True, "show_clubs": False,
    }
    assert body["completeness"] == {"score": 0, "missing": ["headline", "links", "projects", "photo"]}
    assert body["email"] == "fresh@example.com" and body["hidden_sections"] == []


async def test_patch_changes_only_what_is_sent(client, world):
    h = world["oh"]
    r = await client.patch(f"{API}/me/profile", json={
        "headline": "  Robots\tand   rockets ", "visibility": {"links": "met"}, "share_email": "mutual", "is_private": True, "recap_emails": False,
    }, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["headline"] == "Robots and rockets" and body["bio"] == "Line one\nLine two" and body["city"] == "Kochi"
    s = body["settings"]
    assert s["visibility"]["links"] == "met" and s["visibility"]["projects"] == "everyone"
    assert s["share_email"] == "mutual" and s["is_private"] is True and s["recap_emails"] is False and s["notify_saves"] is True
    assert body["completeness"]["missing"] == ["links", "projects", "photo"] and body["completeness"]["score"] == 25
    # Texts clear with null; switches may not be null; bad levels are refused.
    cleared = await client.patch(f"{API}/me/profile", json={"city": None}, headers=h)
    assert cleared.json()["city"] is None
    assert (await client.patch(f"{API}/me/profile", json={"is_private": None}, headers=h)).status_code == 422
    assert (await client.patch(f"{API}/me/profile", json={"visibility": {"links": "friends"}}, headers=h)).status_code == 422
    assert (await client.patch(f"{API}/me/profile", json={"visibility": {"email": "me"}}, headers=h)).status_code == 422
    assert (await client.patch(f"{API}/me/profile", json={"share_phone": "everyone"}, headers=h)).status_code == 422
    assert (await client.patch(f"{API}/me/profile", json={"headline": "x" * 121}, headers=h)).status_code == 422
    unknown_org = await client.patch(f"{API}/me/profile", json={"org_id": 999999}, headers=h)
    assert unknown_org.status_code == 422 and unknown_org.json()["error_code"] == "ORG_NOT_FOUND"


async def test_a_handle_follows_the_rules(client, session, world, make_user):
    h = world["oh"]
    ok = await client.patch(f"{API}/me/profile", json={"handle": "@Asha.Nair"}, headers=h)
    assert ok.status_code == 200 and ok.json()["handle"] == "asha.nair"
    assert (await client.get(f"{API}/profiles/ASHA.NAIR")).status_code == 200  # case-blind
    assert (await client.get(f"{API}/profiles/asha")).status_code == 404  # the old handle is gone

    for bad in ("ab", "a" * 31, "a..b", "-ab", "ab-", "has space", "émile", "a/b", ".ab"):
        r = await client.patch(f"{API}/me/profile", json={"handle": bad}, headers=h)
        assert (r.status_code, r.json()["error_code"]) == (422, "HANDLE_INVALID"), bad
    for kept in ("admin", "network", "live", "compete", "for-clubs", "u", "by-code", "Sitemaps"):
        r = await client.patch(f"{API}/me/profile", json={"handle": kept}, headers=h)
        if len(kept) >= 3:
            assert (r.status_code, r.json()["error_code"]) == (422, "HANDLE_RESERVED"), kept
        else:
            assert r.status_code == 422, kept
    # Taken: by another account whatever the case, and by a deleted one.
    other = await make_user(email="ravi@example.com", username="Ravi")
    gone = await make_user(email="gone@example.com", username="gone_one")
    gone.is_deleted = True
    await session.commit()
    for taken in ("ravi", "RAVI", "gone_one"):
        r = await client.patch(f"{API}/me/profile", json={"handle": taken}, headers=h)
        assert (r.status_code, r.json()["error_code"]) == (409, "HANDLE_TAKEN"), taken
    assert other.username == "Ravi"
    # Every website word in the contract is kept.
    for word in "about answers api certificates claim clubs data-deletion developers events feedback for-clubs help me media og p play pricing privacy register requests sitemaps speakers support terms threads tickets topics u verify live compete network".split():
        assert word in rules.RESERVED


async def test_links_are_the_whole_ordered_list_and_only_safe_ones(client, world):
    h = world["oh"]
    links = [
        {"type": "github", "url": "https://github.com/asha", "label": "Code"},
        {"type": "website", "url": "HTTP://asha.example.in/about?x=1#top"},
    ]
    r = await client.put(f"{API}/me/profile/links", json={"links": links}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["links"] == [
        {"type": "github", "url": "https://github.com/asha", "label": "Code"},
        {"type": "website", "url": "http://asha.example.in/about?x=1#top", "label": None},
    ]
    # Replaced, not appended.
    r = await client.put(f"{API}/me/profile/links", json={"links": [links[1]]}, headers=h)
    assert [l["type"] for l in r.json()["links"]] == ["website"]

    for url in (
        "javascript:alert(1)", " JavaScript:alert(1)", "data:text/html;base64,PHNjcmlwdD4=", "ftp://files.example.com",
        "https://user:pw@example.com", "https://user@example.com", "https://", "https://localhost", "https://exa mple.com",
        "https://example.com/" + "a" * 500, "//example.com", "example.com", 'https://example.com/"onmouseover', "https://example.com\\@evil.com",
        "https://example.com:99999",
    ):
        r = await client.put(f"{API}/me/profile/links", json={"links": [{"type": "other", "url": url}]}, headers=h)
        assert (r.status_code, r.json()["error_code"]) == (422, "LINK_INVALID"), url
    assert (await client.put(f"{API}/me/profile/links", json={"links": [{"type": "myspace", "url": "https://x.com"}]}, headers=h)).status_code == 422
    eleven = [{"type": "other", "url": f"https://site{i}.example.com"} for i in range(11)]
    r = await client.put(f"{API}/me/profile/links", json={"links": eleven}, headers=h)
    assert (r.status_code, r.json()["error_code"]) == (422, "LINK_INVALID")
    # Nothing changed by the refusals.
    assert [l["type"] for l in (await client.get(f"{API}/me/profile", headers=h)).json()["links"]] == ["website"]


async def test_projects_are_added_edited_ordered_and_deleted(client, session, world):
    h = world["oh"]
    a = await client.post(f"{API}/me/profile/projects", json={"title": "Rover", "tags": ["ROS", "ros", "  C++ "], "month": "2026-03", "url": "https://rover.example.com"}, headers=h)
    assert a.status_code == 200, a.text
    a = a.json()
    assert a["tags"] == ["ROS", "C++"] and a["month"] == "2026-03-01" and a["image"] is None
    b = (await client.post(f"{API}/me/profile/projects", json={"title": "Drone", "role": "Lead"}, headers=h)).json()
    edited = await client.patch(f"{API}/me/profile/projects/{a['id']}", json={"summary": "Mars-style rover", "url": None}, headers=h)
    assert edited.status_code == 200 and edited.json()["summary"] == "Mars-style rover" and edited.json()["url"] is None
    assert (await client.patch(f"{API}/me/profile/projects/{a['id']}", json={"title": None}, headers=h)).status_code == 422

    ordered = await client.put(f"{API}/me/profile/projects/order", json={"ids": [b["id"], a["id"]]}, headers=h)
    assert [p["title"] for p in ordered.json()["projects"]] == ["Drone", "Rover"]
    bad = await client.put(f"{API}/me/profile/projects/order", json={"ids": [b["id"]]}, headers=h)
    assert (bad.status_code, bad.json()["error_code"]) == (422, "ORDER_INVALID")

    for body in ({"title": "x", "tags": ["a", "b", "c", "d", "e", "f"]}, {"title": "x", "tags": ["y" * 25]}, {"title": "  "}, {"title": "x", "month": "March"}):
        assert (await client.post(f"{API}/me/profile/projects", json=body, headers=h)).status_code == 422, body
    bad_url = await client.post(f"{API}/me/profile/projects", json={"title": "x", "url": "javascript:void(0)"}, headers=h)
    assert bad_url.json()["error_code"] == "LINK_INVALID"

    # Someone else's project is not theirs to touch.
    assert (await client.patch(f"{API}/me/profile/projects/{a['id']}", json={"role": "x"}, headers=world["vh"])).status_code == 404
    assert (await client.delete(f"{API}/me/profile/projects/{a['id']}", headers=world["vh"])).status_code == 404

    assert (await client.delete(f"{API}/me/profile/projects/{b['id']}", headers=h)).status_code == 204
    assert [p["title"] for p in (await client.get(f"{API}/me/profile", headers=h)).json()["projects"]] == ["Rover"]
    # At most twelve.
    for i in range(11):
        assert (await client.post(f"{API}/me/profile/projects", json={"title": f"P{i}"}, headers=h)).status_code == 200
    full = await client.post(f"{API}/me/profile/projects", json={"title": "One too many"}, headers=h)
    assert (full.status_code, full.json()["error_code"]) == (422, "PROJECT_LIMIT")


async def test_a_project_picture_goes_through_the_image_pipeline(client, session, world, fake_s3):
    from PIL import Image

    h = world["oh"]
    project = (await client.post(f"{API}/me/profile/projects", json={"title": "Rover"}, headers=h)).json()
    buf = io.BytesIO()
    Image.new("RGB", (2400, 1200), (200, 30, 30)).save(buf, "PNG")
    r = await client.post(f"{API}/me/profile/projects/{project['id']}/image", files={"file": ("rover.png", buf.getvalue(), "image/png")}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["image"] and "users/projects/" in r.json()["image"]
    stored = [k for k in fake_s3 if "users/projects/" in k]
    assert len(stored) == 4  # the original (made smaller) and three sizes
    not_a_picture = await client.post(f"{API}/me/profile/projects/{project['id']}/image", files={"file": ("x.png", b"not an image", "image/png")}, headers=h)
    assert not_a_picture.status_code == 422
    cleared = await client.delete(f"{API}/me/profile/projects/{project['id']}/image", headers=h)
    assert cleared.status_code == 200 and cleared.json()["image"] is None
    stored_row = await session.scalar(select(ProfileProjects).where(ProfileProjects.id == project["id"]).execution_options(populate_existing=True))
    assert stored_row.image_key is None


# ─── The QR code ────────────────────────────────────────────────────────


async def test_my_qr_is_made_once_and_a_replaced_code_stops_at_once(client, session, world):
    h = world["oh"]
    first = await client.get(f"{API}/me/qr", headers=h)
    assert first.status_code == 200, first.text
    code = first.json()["code"]
    assert rules.CODE_RX.match(code) and not set(code) & set("01oil")
    assert first.json()["url"] == f"https://myotherapp.in/p/{code}"
    assert (await client.get(f"{API}/me/qr", headers=h)).json()["code"] == code

    found = await client.get(f"{API}/profiles/by-code/{code.upper()}")
    assert found.status_code == 200 and found.json()["handle"] == "asha" and found.json()["profile"]["full_name"] == "Asha Nair"

    new = await client.post(f"{API}/me/qr/replace", headers=h)
    assert new.status_code == 200 and new.json()["code"] != code
    old = await client.get(f"{API}/profiles/by-code/{code}")
    assert (old.status_code, old.json()["error_code"]) == (410, "PROFILE_CODE_INACTIVE")
    assert (await client.get(f"{API}/profiles/by-code/{new.json()['code']}")).status_code == 200
    live = (await session.scalars(select(ProfileCodes).where(ProfileCodes.user_id == world["owner"].id, ProfileCodes.replaced_at.is_(None)))).all()
    assert len(live) == 1

    await client.patch(f"{API}/me/profile", json={"scanning_enabled": False}, headers=h)
    off = await client.get(f"{API}/profiles/by-code/{new.json()['code']}")
    assert (off.status_code, off.json()["error_code"]) == (403, "PROFILE_SCANNING_OFF")
    # Scanning is QR saves only: the profile itself still opens.
    assert (await client.get(f"{API}/profiles/asha")).status_code == 200
    for unknown in ("abcdefghjk", "nope", "0" * 10, "a" * 64):
        r = await client.get(f"{API}/profiles/by-code/{unknown}")
        assert (r.status_code, r.json()["error_code"]) == (404, "NOT_FOUND"), unknown


# ─── What each viewer sees ──────────────────────────────────────────────


async def test_anyone_signed_out_reads_a_public_profile(client, session, world):
    from app.api.interests.models import InterestCategory, InterestIconType, Interests
    from app.api.users.models import UserInterests

    cat = InterestCategory(name="Tech")
    session.add(cat)
    await session.flush()
    ai = Interests(name="AI", icon="🤖", icon_type=InterestIconType.emoji, category_id=cat.id)
    session.add(ai)
    await session.flush()
    session.add(UserInterests(user_id=world["owner"].id, interest_id=ai.id))
    await session.commit()
    await client.put(f"{API}/me/profile/links", json={"links": [{"type": "github", "url": "https://github.com/asha"}]}, headers=world["oh"])

    r = await client.get(f"{API}/profiles/asha")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["full_name"] == "Asha Nair" and body["headline"] == "Builds robots" and body["bio"] == "Line one\nLine two"
    assert body["links"] == [{"type": "github", "url": "https://github.com/asha", "label": None}]
    assert body["interests"] == [{"id": ai.id, "name": "AI", "emoji": "🤖"}]
    assert body["contact"] == {"email": None, "phone": None} and body["relation"] is None and body["is_me"] is False
    assert body["hidden_sections"] == [] and body["private"] is False and body["indexable"] is False
    assert body["achievements"] == {"events_attended": None, "certificates": [], "talks": [], "clubs": None}


async def test_every_section_obeys_its_level_for_every_viewer(client, session, make_user, mint_token, world):
    owner, oh = world["owner"], world["oh"]
    await client.patch(f"{API}/me/profile", json={
        "visibility": {"links": "met", "projects": "mutual", "achievements": "me", "interests": "everyone", "org": "met"},
        "share_email": "met", "share_phone": "mutual",
    }, headers=oh)
    await client.put(f"{API}/me/profile/links", json={"links": [{"type": "github", "url": "https://github.com/asha"}]}, headers=oh)
    await client.post(f"{API}/me/profile/projects", json={"title": "Rover"}, headers=oh)
    from app.api.orgs.models import Organizations, OrgTypes

    org = Organizations(name="COEP Tech", type=OrgTypes.college)
    session.add(org)
    await session.commit()
    await client.patch(f"{API}/me/profile", json={"org_id": org.id}, headers=oh)

    saver, sh = await person(session, make_user, mint_token, "saver", "Saver One")  # saved the owner
    saved, svh = await person(session, make_user, mint_token, "saved", "Saved One")  # the owner saved them
    both, bh = await person(session, make_user, mint_token, "both", "Both Ways")  # mutual
    await session.commit()
    assert (await save(client, sh, handle="asha")).status_code == 200
    assert (await save(client, oh, handle="saved")).status_code == 200
    assert (await save(client, bh, handle="asha")).status_code == 200
    assert (await save(client, oh, handle="both")).status_code == 200

    async def seen(headers):
        r = await client.get(f"{API}/profiles/asha", headers=headers or {})
        assert r.status_code == 200, r.text
        return r.json()

    anon, stranger = await seen(None), await seen(world["vh"])
    for b in (anon, stranger):
        assert b["links"] == [] and b["projects"] == [] and b["org"] is None and b["contact"] == {"email": None, "phone": None}
        assert set(b["hidden_sections"]) == {"links", "projects", "achievements", "org"}
    assert anon["relation"] is None
    assert stranger["relation"] == {"saved_by_me": False, "saved_me": False, "mutual": False, "network_id": None}

    for headers, rel in ((sh, (True, False)), (svh, (False, True))):
        b = await seen(headers)
        assert (b["relation"]["saved_by_me"], b["relation"]["saved_me"], b["relation"]["mutual"]) == (*rel, False)
        assert [l["url"] for l in b["links"]] == ["https://github.com/asha"] and b["org"]["name"] == "COEP Tech"
        assert b["projects"] == [] and b["contact"] == {"email": "asha@example.com", "phone": None}
        assert set(b["hidden_sections"]) == {"projects", "achievements"}

    m = await seen(bh)
    assert m["relation"]["mutual"] is True and m["relation"]["network_id"] is not None
    assert [p["title"] for p in m["projects"]] == ["Rover"] and m["contact"] == {"email": "asha@example.com", "phone": "+919800000001"}
    assert m["hidden_sections"] == ["achievements"]
    assert m["achievements"] == {"events_attended": None, "certificates": None, "talks": None, "clubs": None}

    me = await seen(oh)
    assert me["is_me"] and me["relation"] is None and me["hidden_sections"] == [] and me["contact"]["phone"] == "+919800000001"

    # `me` hides a section from everyone else, the mutual included.
    await client.patch(f"{API}/me/profile", json={"visibility": {"links": "me"}, "share_email": "nobody"}, headers=oh)
    m = await seen(bh)
    assert m["links"] == [] and "links" in m["hidden_sections"] and m["contact"]["email"] is None


async def test_a_private_profile_shows_strangers_only_the_minimal_shape(client, session, make_user, mint_token, world):
    oh = world["oh"]
    await client.patch(f"{API}/me/profile", json={"is_private": True, "indexable": True, "share_email": "met"}, headers=oh)
    await client.put(f"{API}/me/profile/links", json={"links": [{"type": "github", "url": "https://github.com/asha"}]}, headers=oh)
    fan, fh = await person(session, make_user, mint_token, "fan", "Fan Person")
    await session.commit()

    minimal_keys = {"handle": "asha", "full_name": "Asha Nair", "headline": "Builds robots", "private": True}

    def is_minimal(b):
        assert {k: b[k] for k in minimal_keys} == minimal_keys
        assert b["bio"] is None and b["city"] is None and b["org"] is None and b["links"] == [] and b["projects"] == [] and b["interests"] == []
        assert b["contact"] == {"email": None, "phone": None} and b["indexable"] is False
        assert set(b["hidden_sections"]) == {"links", "projects", "achievements", "interests", "org"}

    is_minimal((await client.get(f"{API}/profiles/asha")).json())
    is_minimal((await client.get(f"{API}/profiles/asha", headers=world["vh"])).json())
    # Saving them one-way does not open it; the vCard is as small.
    saved = await save(client, fh, handle="asha")
    assert saved.status_code == 200 and saved.json()["profile"]["private"] is True
    is_minimal(saved.json()["profile"])
    b = (await client.get(f"{API}/profiles/asha", headers=fh)).json()
    is_minimal(b)
    assert b["relation"]["saved_by_me"] is True
    card = (await client.get(f"{API}/profiles/asha/vcard", headers=fh)).text
    assert "FN:Asha Nair" in card and "EMAIL" not in card and "github" not in card
    # Once the owner saves them, they see it.
    await save(client, oh, handle="fan")
    b = (await client.get(f"{API}/profiles/asha", headers=fh)).json()
    assert b["bio"] == "Line one\nLine two" and b["links"] and b["contact"]["email"] == "asha@example.com" and b["private"] is True
    assert (await client.get(f"{API}/profiles/asha", headers=oh)).json()["bio"] == "Line one\nLine two"


async def test_unknown_deleted_suspended_hidden_and_blocked_profiles_are_404(client, session, make_user, mint_token, make_club, world):
    owner, viewer = world["owner"], world["viewer"]
    code = (await client.get(f"{API}/me/qr", headers=world["oh"])).json()["code"]

    async def status(headers=None):
        return (
            (await client.get(f"{API}/profiles/asha", headers=headers or {})).status_code,
            (await client.get(f"{API}/profiles/asha/vcard", headers=headers or {})).status_code,
            (await client.get(f"{API}/profiles/by-code/{code}", headers=headers or {})).status_code,
            (await save(client, headers or world["vh"], handle="asha")).status_code if headers is not None else None,
        )

    assert (await client.get(f"{API}/profiles/nobody-here")).json()["error_code"] == "NOT_FOUND"
    # Blocked, either way round.
    session.add(UserBlocks(blocker_id=owner.id, blocked_id=viewer.id))
    await session.commit()
    assert await status(world["vh"]) == (404, 404, 404, 404)
    assert (await client.get(f"{API}/profiles/dev", headers=world["oh"])).status_code == 404
    assert (await status())[:3] == (200, 200, 200)  # signed out: no block
    await session.execute(UserBlocks.__table__.delete())
    await session.commit()
    session.add(UserBlocks(blocker_id=viewer.id, blocked_id=owner.id))
    await session.commit()
    assert await status(world["vh"]) == (404, 404, 404, 404)
    await session.execute(UserBlocks.__table__.delete())
    await session.commit()

    # Hidden by an admin.
    p = await profile_row(session, owner)
    p.suspended_at = datetime.now(timezone.utc)
    await session.commit()
    assert (await status())[:3] == (404, 404, 404)
    p.suspended_at = None
    # The account suspended.
    owner.is_active = False
    await session.commit()
    assert (await status())[:3] == (404, 404, 404)
    owner.is_active = True
    # Deleted.
    owner.is_deleted = True
    await session.commit()
    assert (await status())[:3] == (404, 404, 404)
    owner.is_deleted = False
    await session.commit()
    assert (await status())[:3] == (200, 200, 200)

    # Only people have profiles: a guest's or a club's username is not one.
    await make_user(email="g@example.com", username="a-guest", user_type=UserTypes.guest)
    club_user, _ = await make_club(name="Robo Club", email="roboclub@example.com")
    await session.commit()
    assert (await client.get(f"{API}/profiles/a-guest")).status_code == 404
    assert (await client.get(f"{API}/profiles/{club_user.username}")).status_code == 404


async def test_the_vcard_holds_what_this_viewer_may_see(client, session, make_user, mint_token, world):
    oh = world["oh"]
    await client.patch(f"{API}/me/profile", json={"headline": "Robots, rockets; and more", "share_email": "met"}, headers=oh)
    await client.put(f"{API}/me/profile/links", json={"links": [{"type": "github", "url": "https://github.com/asha"}]}, headers=oh)
    r = await client.get(f"{API}/profiles/asha/vcard")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/vcard")
    assert r.headers["content-disposition"] == 'attachment; filename="asha.vcf"'
    card = r.text
    assert card.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n") and card.endswith("END:VCARD\r\n")
    assert "FN:Asha Nair\r\n" in card and "N:Nair;Asha;;;\r\n" in card
    assert "TITLE:Robots\\, rockets\\; and more\r\n" in card
    assert "URL:https://myotherapp.in/u/asha\r\n" in card and "URL:https://github.com/asha\r\n" in card
    assert "EMAIL" not in card and "TEL" not in card
    # Saved by the owner: the address is theirs to see.
    await save(client, oh, handle="dev")
    card = (await client.get(f"{API}/profiles/asha/vcard", headers=world["vh"])).text
    assert "EMAIL;TYPE=INTERNET:asha@example.com\r\n" in card and "TEL" not in card
    assert all(len(line.encode()) <= 75 for line in card.split("\r\n"))


async def test_profile_reads_are_rate_limited_per_address(client, world, monkeypatch):
    monkeypatch.setattr(limits, "PROFILE_READS", "3/minute")
    monkeypatch.setattr(limits, "CODE_READS", "2/minute")
    assert limits.SAVES == ("60/minute", "300/hour")
    for _ in range(3):
        assert (await client.get(f"{API}/profiles/asha")).status_code == 200
    r = await client.get(f"{API}/profiles/asha/vcard")
    assert (r.status_code, r.json()["error_code"]) == (429, "RATE_LIMITED") and r.json()["errors"]["retry_after"] >= 1
    for _ in range(2):
        assert (await client.get(f"{API}/profiles/by-code/abcdefghjk")).status_code == 404
    assert (await client.get(f"{API}/profiles/by-code/abcdefghjk")).status_code == 429


async def test_the_site_may_call_the_profile_routes_from_the_browser(client):
    origin = "https://myotherapp.in"
    for method, path in (("POST", f"{API}/network"), ("GET", f"{API}/profiles/asha/vcard")):
        pre = await client.options(path, headers={
            "Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": "authorization, content-type",
        })
        assert pre.status_code == 200, pre.text
        assert pre.headers["access-control-allow-origin"] == origin
        assert method in pre.headers["access-control-allow-methods"]
        assert "authorization" in pre.headers["access-control-allow-headers"].lower()
    r = await client.get(f"{API}/profiles/nobody-here", headers={"Origin": origin})
    assert r.headers["access-control-allow-origin"] == origin


async def test_achievements_show_what_the_person_chose(client, session, world, make_club, make_event, make_registration):
    from app.api.closeout.models import EventStatus
    from app.api.clubs.models import ClubUsersLink

    owner, oh = world["owner"], world["oh"]
    host, club = await make_club(name="Hack Club", email="hack-host@example.com")
    held = await make_event(club=club, created_by=host, name="Hack Night", event_datetime=datetime.now(timezone.utc) - timedelta(days=3), lifecycle=EventStatus.published)
    hidden = await make_event(club=club, created_by=host, name="Secret Meetup", event_datetime=datetime.now(timezone.utc) - timedelta(days=2), lifecycle=EventStatus.draft)
    for e in (held, hidden):
        await make_registration(
            event=e, user=owner, is_attended=True, certificate_issued_at=datetime.now(timezone.utc), certificate_public_id=f"pub{e.id}x",
        )
    session.add(ClubUsersLink(club_id=club.id, user_id=owner.id, is_following=True))
    await session.commit()
    a = (await client.get(f"{API}/profiles/asha")).json()["achievements"]
    assert a["events_attended"] is None and a["clubs"] is None
    # Only the public event's certificate: a private event's name never shows.
    assert a["certificates"] == [{"event_name": "Hack Night", "url": f"https://myotherapp.in/certificates/pub{held.id}x"}]
    await client.patch(f"{API}/me/profile", json={"show_events": True, "show_clubs": True, "show_certificates": False}, headers=oh)
    a = (await client.get(f"{API}/profiles/asha")).json()["achievements"]
    assert a["events_attended"] == 2 and a["clubs"] == [{"name": "Hack Club", "slug": club.slug}] and a["certificates"] is None


async def test_the_owners_user_id_is_given_only_to_a_signed_in_reader(client, session, make_user, world):
    owner = world["owner"]
    code = (await client.get(f"{API}/me/qr", headers=world["oh"])).json()["code"]
    anon = (await client.get(f"{API}/profiles/asha")).json()
    assert "user_id" in anon and anon["user_id"] is None
    assert (await client.get(f"{API}/profiles/by-code/{code}")).json()["profile"]["user_id"] is None
    signed = (await client.get(f"{API}/profiles/asha", headers=world["vh"])).json()
    assert signed["user_id"] == owner.id
    assert (await client.get(f"{API}/profiles/by-code/{code}", headers=world["vh"])).json()["profile"]["user_id"] == owner.id
    assert (await client.get(f"{API}/me/profile", headers=world["oh"])).json()["user_id"] == owner.id
    # A private profile's stranger is signed in too: the id, to report or block with.
    await client.patch(f"{API}/me/profile", json={"is_private": True}, headers=world["oh"])
    private = (await client.get(f"{API}/profiles/asha", headers=world["vh"])).json()
    assert private["private"] is True and private["user_id"] == owner.id
    assert (await client.get(f"{API}/profiles/asha")).json()["user_id"] is None


SITE_TOKEN = "s" * 40
SITE_ADDR = "76.76.21.21"  # the site's server, shared by every visitor


async def test_the_sites_server_reads_in_its_own_bucket_not_its_address(client, world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SITE_TOKENS", SITE_TOKEN)
    monkeypatch.setattr(limits, "PROFILE_READS", "3/minute")
    monkeypatch.setattr(limits, "CODE_READS", "2/minute")
    monkeypatch.setattr(settings, "SITE_RATE_LIMIT", "6/minute")
    site = {"X-Forwarded-For": SITE_ADDR, "X-Site-Token": SITE_TOKEN}
    # The site's renders, no visitor named (ISR): past one shared address's 3 a minute.
    for _ in range(6):
        assert (await client.get(f"{API}/profiles/asha", headers=site)).status_code == 200
    # Its own bucket still bites (SITE_RATE_LIMIT), in RATE_LIMITED's words.
    r = await client.get(f"{API}/profiles/asha/vcard", headers=site)
    assert (r.status_code, r.json()["error_code"]) == (429, "RATE_LIMITED")
    # The same address without the token is an ordinary caller: 3 a minute.
    plain = {"X-Forwarded-For": "198.51.100.7"}
    assert [(await client.get(f"{API}/profiles/asha", headers=plain)).status_code for _ in range(4)] == [200, 200, 200, 429]


async def test_a_forwarded_visitor_counts_only_behind_the_sites_token(client, world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SITE_TOKENS", SITE_TOKEN)
    monkeypatch.setattr(limits, "CODE_READS", "2/minute")
    code = (await client.get(f"{API}/me/qr", headers=world["oh"])).json()["code"]
    path = f"{API}/profiles/by-code/{code}"

    def from_site(visitor):
        return {"X-Forwarded-For": SITE_ADDR, "X-Site-Token": SITE_TOKEN, "X-Visitor-IP": visitor}

    # Per visitor the site names: one scanner's 3rd ask is refused, another's goes.
    assert [(await client.get(path, headers=from_site("49.37.10.1"))).status_code for _ in range(3)] == [200, 200, 429]
    assert (await client.get(path, headers=from_site("49.37.10.2"))).status_code == 200
    # Without the token (or with a wrong one) the header is ignored: the caller's address counts.
    for token in (None, "t" * 40):
        limiter_reset()
        for i in range(3):
            headers = {"X-Forwarded-For": "203.0.113.9", "X-Visitor-IP": f"10.0.0.{i + 1}"}
            if token:
                headers["X-Site-Token"] = token
            status = (await client.get(path, headers=headers)).status_code
            assert status == (429 if i == 2 else 200), (token, i)


def limiter_reset():
    from app.core.rate_limit import limiter

    limiter.reset()



async def test_link_hosts_are_kept_in_ascii_and_look_alikes_are_refused(client, world):
    h = world["oh"]
    r = await client.put(f"{API}/me/profile/links", json={"links": [
        {"type": "website", "url": "https://Bücher.Example/kaufen?q=ä"},
        {"type": "website", "url": "https://пример.рф/"},
        {"type": "website", "url": "https://Example.COM:8443/a"},
    ]}, headers=h)
    assert r.status_code == 200, r.text
    assert [l["url"] for l in r.json()["links"]] == [
        "https://xn--bcher-kva.example/kaufen?q=ä", "https://xn--e1afmkfd.xn--p1ai/", "https://example.com:8443/a",
    ]
    for url in ("https://example.com\u3002evil.com/", "https://evil\uff0ecom/", "https://evil\uff61com/", "https://\u0430pple.com/", "https://paypa\u04cf.com/"):
        r = await client.put(f"{API}/me/profile/links", json={"links": [{"type": "website", "url": url}]}, headers=h)
        assert (r.status_code, r.json()["error_code"]) == (422, "LINK_INVALID"), url


async def test_an_old_reserved_username_has_no_public_page_until_it_changes(client, session, make_user, mint_token):
    user, uh = await person(session, make_user, mint_token, "support", "Support Desk")
    long_, lh = await person(session, make_user, mint_token, "a" * 40, "Long Name")
    await session.commit()
    code = (await client.get(f"{API}/me/qr", headers=uh)).json()["code"]
    for path in (f"{API}/profiles/support", f"{API}/profiles/support/vcard", f"{API}/profiles/by-code/{code}"):
        assert (await client.get(path)).status_code == 404, path
    assert (await save(client, lh, handle="support")).status_code == 404
    me = (await client.get(f"{API}/me/profile", headers=uh)).json()
    assert me["handle"] == "support" and me["handle_needs_change"] is True
    # A long old username still has its page, and is offered a change too.
    assert (await client.get(f"{API}/profiles/{'a' * 40}")).status_code == 200
    assert (await client.get(f"{API}/me/profile", headers=lh)).json()["handle_needs_change"] is True
    # Changed: the page is theirs again.
    r = await client.patch(f"{API}/me/profile", json={"handle": "help.desk"}, headers=uh)
    assert r.status_code == 200 and r.json()["handle_needs_change"] is False
    assert (await client.get(f"{API}/profiles/help.desk")).status_code == 200
    assert (await client.get(f"{API}/profiles/by-code/{code}")).status_code == 200


async def test_a_handle_given_up_is_held_for_30_days(client, session, make_user, mint_token, world):
    from app.api.profiles.models import ReleasedHandles
    from app.api.users import service as users

    oh, vh = world["oh"], world["vh"]
    assert (await client.patch(f"{API}/me/profile", json={"handle": "asha.n"}, headers=oh)).status_code == 200
    # Someone else can't take it, now or in 29 days...
    r = await client.patch(f"{API}/me/profile", json={"handle": "asha"}, headers=vh)
    assert (r.status_code, r.json()["error_code"]) == (409, "HANDLE_TAKEN")
    assert (await client.get(f"{API}/profiles/asha")).status_code == 404
    # ...nor is it a newcomer's generated username.
    newcomer = users.Users(full_name="Asha", email="asha2@example.com")
    assert await users.generate_username(newcomer, session) != "asha"
    # Its owner may take it back.
    assert (await client.patch(f"{API}/me/profile", json={"handle": "asha"}, headers=oh)).status_code == 200
    assert (await client.patch(f"{API}/me/profile", json={"handle": "asha.n"}, headers=oh)).status_code == 200
    # After 30 days it is free.
    await session.execute(ReleasedHandles.__table__.update().values(released_at=datetime.now(timezone.utc) - timedelta(days=31)))
    await session.commit()
    assert (await client.patch(f"{API}/me/profile", json={"handle": "asha"}, headers=vh)).status_code == 200

    # An account's own deletion holds its handle too.
    gone, gh = await person(session, make_user, mint_token, "leaving", "Leaving Soon")
    await session.commit()
    assert (await client.delete(f"{API}/user/delete", headers=gh)).status_code in (200, 204)
    r = await client.patch(f"{API}/me/profile", json={"handle": "leaving"}, headers=oh)
    assert (r.status_code, r.json()["error_code"]) == (409, "HANDLE_TAKEN")


async def test_the_site_counts_each_visitor_it_names_on_every_profile_read(client, world, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "SITE_TOKENS", SITE_TOKEN)
    monkeypatch.setattr(limits, "PROFILE_READS", "3/minute")

    def from_site(visitor):
        return {"X-Forwarded-For": SITE_ADDR, "X-Site-Token": SITE_TOKEN, "X-Visitor-IP": visitor}

    # The page, its vCard and the picture reads (the same profile read, fresh) share one visitor's 3 a minute.
    one = from_site("49.37.20.1")
    got = [
        (await client.get(f"{API}/profiles/asha", headers=one)).status_code,
        (await client.get(f"{API}/profiles/asha/vcard", headers=one)).status_code,
        (await client.get(f"{API}/profiles/asha", headers=one)).status_code,
        (await client.get(f"{API}/profiles/asha/vcard", headers=one)).status_code,
    ]
    assert got == [200, 200, 200, 429]
    # Another visitor through the same server is their own.
    assert (await client.get(f"{API}/profiles/asha", headers=from_site("49.37.20.2"))).status_code == 200
