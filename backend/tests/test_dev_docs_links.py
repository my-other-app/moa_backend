"""Private developer docs (app/api/partner/docs_links.py,
feature-register/private-dev-docs-contract.md).

The signed links that open the docs: a club's owner or admin makes them
(for themselves, 7 days; to share, 30 days; at most 20 live), a platform
admin with manage_content makes theirs and partner apps'; each is hashed,
shown once, listed and revocable. The website's check,
GET /api/v1/developers/access, answers 404 {ok: false} for every link that
no longer works, whatever the reason -- each way one dies is here -- and is
limited per address. Then the OpenAPI document's gate: a partner credential,
a docs token or the website's server, else 401.
"""
import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.api.audit.models import AdminAuditLogs
from app.api.club_access.models import ClubMembers
from app.api.partner import allocation, docs_links
from app.api.partner.models import DeveloperDocsLinks, PartnerApps
from app.api.users.models import AdminRoles
from app.config import settings
from app.core.auth.authentication import get_password_hash
from tests.conftest import auth_header
from tests.test_partner_api import H, basic, key_for, partner_on, register_app, world  # noqa: F401

API = "/api/v1"
V1 = "/partner/v1"
CLUB = f"{API}/clubs/integrations/docs-links"
ADMIN = f"{API}/admin/integrations/docs-links"
ACCESS = f"{API}/developers/access"
SITE_TOKEN = secrets.token_hex(32)
VERCEL = "76.76.21.21"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def token_in(url: str) -> str:
    assert url.startswith("https://myotherapp.in/developers/access?t=moa_docs_"), url
    return url.split("?t=", 1)[1]


async def owner_h(mint_token, world):
    return auth_header(await mint_token(world.owner))


async def member_h(session, world, role: str, email: str):
    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token

    m = ClubMembers(club_id=world.ids.club, email=email, full_name=f"Ravi ({role})", role=role, status="active",
                    password=get_password_hash("Password123!"))
    session.add(m)
    await session.commit()
    await session.refresh(world.owner)
    token = create_access_token(data=AuthTokenData(
        user_id=world.ids.owner, token_type="access_token", token_version=world.owner.token_version, member_id=m.id,
    ).model_dump())
    return m, auth_header(token)


async def check(client, raw, **headers):
    return await client.get(ACCESS, params={"t": raw}, headers=headers)


async def make(client, h, purpose="self", **body):
    r = await client.post(CLUB, json={"purpose": purpose, **body}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def link_row(session, raw) -> DeveloperDocsLinks:
    return await session.scalar(select(DeveloperDocsLinks).where(DeveloperDocsLinks.token_hash == hashlib.sha256(raw.encode()).hexdigest()))


# ─── A club's links ──────────────────────────────────────────────────────


async def test_a_club_link_is_hashed_shown_once_and_listed(client, session, world, mint_token):
    h = await owner_h(mint_token, world)
    made = await make(client, h, "self")
    raw = token_in(made["url"])
    assert len(raw) == len("moa_docs_") + 43
    link = made["link"]
    assert link["kind"] == "club" and link["purpose"] == "self" and link["status"] == "active" and link["label"] is None
    assert link["club"] == {"id": world.ids.club, "name": "Robotics Club"}
    assert link["created_by"]["id"] == world.ids.owner and link["created_by"]["name"] and link["created_by"]["member_id"] is None
    assert link["last_used_at"] is None and link["revoked_at"] is None and link["use_count"] == 0
    expires = datetime.fromisoformat(link["expires_at"])
    assert timedelta(days=6, hours=23) < expires - _now() <= timedelta(days=7)
    row = await link_row(session, raw)
    assert row is not None and str(row.id) == link["id"]
    assert raw not in json.dumps({c.name: str(getattr(row, c.name)) for c in row.__table__.columns})

    shared = await make(client, h, "share", label="Ravi, our web agency")
    assert shared["link"]["purpose"] == "share" and shared["link"]["label"] == "Ravi, our web agency"
    assert timedelta(days=29, hours=23) < datetime.fromisoformat(shared["link"]["expires_at"]) - _now() <= timedelta(days=30)

    listed = (await client.get(CLUB, headers=h)).json()["links"]
    assert [x["id"] for x in listed] == [shared["link"]["id"], link["id"]]  # newest first
    text = json.dumps(listed)
    assert raw not in text and "url" not in listed[0] and token_in(shared["url"]) not in text


async def test_the_check_answers_and_counts_a_use_at_most_once_a_minute(client, session, world, mint_token):
    made = await make(client, await owner_h(mint_token, world), "share")
    raw = token_in(made["url"])
    r = await check(client, raw)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "kind": "club", "expires_at": made["link"]["expires_at"],
                        "club": {"id": world.ids.club, "name": "Robotics Club"}}
    assert "no-store" in r.headers["Cache-Control"] and "private" in r.headers["Cache-Control"]
    assert (await check(client, raw)).status_code == 200
    row = await link_row(session, raw)
    await session.refresh(row)
    assert row.use_count == 1 and row.last_used_at is not None
    row.last_used_at = _now() - timedelta(minutes=2)
    await session.commit()
    # In the header too (kept out of access logs).
    assert (await client.get(ACCESS, headers={"MOA-Docs-Token": raw})).status_code == 200
    await session.refresh(row)
    assert row.use_count == 2
    listed = (await client.get(CLUB, headers=await owner_h(mint_token, world))).json()["links"]
    assert listed[0]["use_count"] == 2 and listed[0]["last_used_at"]


async def test_revoking_ends_a_link_and_both_are_audited(client, session, world, mint_token):
    h = await owner_h(mint_token, world)
    made = await make(client, h, "share", label="Agency")
    raw = token_in(made["url"])
    r = await client.delete(f"{CLUB}/{made['link']['id']}", headers=h)
    assert r.status_code == 200 and r.json() == {"revoked": True}
    r = await check(client, raw)
    assert r.status_code == 404 and r.json() == {"ok": False}
    listed = (await client.get(CLUB, headers=h)).json()["links"]
    assert listed[0]["status"] == "revoked" and listed[0]["revoked_at"]
    assert (await client.delete(f"{CLUB}/not-a-link", headers=h)).status_code == 404
    actions = list(await session.scalars(select(AdminAuditLogs.action).where(AdminAuditLogs.action.like("partner.docs_link.%"))))
    assert sorted(actions) == ["partner.docs_link.create", "partner.docs_link.revoke"]


async def test_a_club_sees_only_its_own_and_ended_links_drop_off_after_30_days(client, session, world, mint_token, make_club):
    h = await owner_h(mint_token, world)
    old = await make(client, h, "share")
    other_owner, other_club = await make_club(name="Debate Club", email="debate@example.com")
    await allocation.put(session, other_club.id, enabled=True)
    await session.commit()
    theirs = await make(client, auth_header(await mint_token(other_owner)), "share")
    assert (await client.delete(f"{CLUB}/{theirs['link']['id']}", headers=h)).status_code == 404
    assert [x["id"] for x in (await client.get(CLUB, headers=h)).json()["links"]] == [old["link"]["id"]]
    row = await session.get(DeveloperDocsLinks, uuid.UUID(old["link"]["id"]))
    row.expires_at = _now() - timedelta(days=31)
    await session.commit()
    assert (await client.get(CLUB, headers=h)).json()["links"] == []
    row.expires_at = _now() - timedelta(days=2)
    await session.commit()
    assert [x["status"] for x in (await client.get(CLUB, headers=h)).json()["links"]] == ["expired"]


async def test_a_club_not_allocated_cannot_make_one(client, session, world, mint_token, make_club):
    other, _club = await make_club(name="Quiz Club", email="quiz@example.com")
    await session.commit()
    r = await client.post(CLUB, json={"purpose": "self"}, headers=auth_header(await mint_token(other)))
    assert r.status_code == 403 and r.json()["error_code"] == "NOT_ALLOCATED"
    r = await client.post(CLUB, json={"purpose": "everyone"}, headers=await owner_h(mint_token, world))
    assert r.status_code == 422
    r = await client.post(CLUB, json={"purpose": "share", "label": "x" * 81}, headers=await owner_h(mint_token, world))
    assert r.status_code == 422


async def test_at_most_20_live_links_a_club(client, session, world, mint_token):
    h = await owner_h(mint_token, world)
    for _ in range(docs_links.MAX_LIVE_PER_CLUB):
        await docs_links.create_for_club(session, world.ids.club, purpose="share", user_id=world.ids.owner)
    await session.commit()
    r = await client.post(CLUB, json={"purpose": "self"}, headers=h)
    assert r.status_code == 409 and r.json()["error_code"] == "DOCS_LINK_LIMIT"
    first = (await client.get(CLUB, headers=h)).json()["links"][0]
    assert (await client.delete(f"{CLUB}/{first['id']}", headers=h)).status_code == 200
    assert (await client.post(CLUB, json={"purpose": "self"}, headers=h)).status_code == 201
    # An expired one leaves room too.
    row = (await session.scalars(select(DeveloperDocsLinks).where(DeveloperDocsLinks.revoked_at.is_(None)))).first()
    row.expires_at = _now() - timedelta(seconds=1)
    await session.commit()
    assert (await client.post(CLUB, json={"purpose": "share"}, headers=h)).status_code == 201


async def test_only_the_owner_and_admins_make_them(client, session, world):
    _editor, eh = await member_h(session, world, "editor", "editor@robotics.example.com")
    assert (await client.post(CLUB, json={"purpose": "self"}, headers=eh)).status_code == 403
    assert (await client.get(CLUB, headers=eh)).status_code == 403
    admin, ah = await member_h(session, world, "admin", "admin@robotics.example.com")
    made = await make(client, ah, "self")
    assert made["link"]["created_by"]["member_id"] == admin.id and made["link"]["created_by"]["name"] == "Ravi (admin)"


# ─── Every way a link dies ───────────────────────────────────────────────


async def test_a_club_link_dies_when_it_expires_or_the_club_loses_access(client, session, world, mint_token, monkeypatch):
    from app.api.clubs.models import Clubs
    from app.api.users.models import Users

    raw = token_in((await make(client, await owner_h(mint_token, world), "share"))["url"])
    assert (await check(client, raw)).status_code == 200
    # API access turned off, and on again: checked on every use.
    await allocation.put(session, world.ids.club, enabled=False)
    await session.commit()
    assert (await check(client, raw)).json() == {"ok": False}
    await allocation.put(session, world.ids.club, enabled=True)
    await session.commit()
    assert (await check(client, raw)).status_code == 200
    # The club no longer active: waiting for approval, its account suspended, deleted.
    club = await session.get(Clubs, world.ids.club)
    club.is_verified = False
    await session.commit()
    assert (await check(client, raw)).status_code == 404
    club.is_verified = True
    owner = await session.get(Users, world.ids.owner)
    owner.is_active = False
    await session.commit()
    assert (await check(client, raw)).status_code == 404
    owner.is_active = True
    await session.commit()
    assert (await check(client, raw)).status_code == 200
    # The kill switch.
    monkeypatch.setattr(settings, "PARTNER_API_ENABLED", False)
    assert (await check(client, raw)).status_code == 404
    monkeypatch.setattr(settings, "PARTNER_API_ENABLED", True)
    assert (await check(client, raw)).status_code == 200
    # Expired.
    row = await link_row(session, raw)
    row.expires_at = _now() - timedelta(seconds=1)
    await session.commit()
    assert (await check(client, raw)).json() == {"ok": False}
    club.is_deleted = True
    await session.commit()
    row.expires_at = _now() + timedelta(days=1)
    await session.commit()
    assert (await check(client, raw)).status_code == 404


async def test_a_club_link_dies_when_its_maker_is_no_longer_an_admin(client, session, world):
    member, ah = await member_h(session, world, "admin", "maker@robotics.example.com")
    raw = token_in((await make(client, ah, "share"))["url"])
    assert (await check(client, raw)).status_code == 200
    member.role = "editor"
    await session.commit()
    assert (await check(client, raw)).status_code == 404
    member.role = "admin"
    await session.commit()
    assert (await check(client, raw)).status_code == 200
    member.status = "removed"
    await session.commit()
    assert (await check(client, raw)).status_code == 404
    # Their row deleted: the link goes with it.
    await session.delete(member)
    await session.commit()
    assert await link_row(session, raw) is None
    assert (await check(client, raw)).status_code == 404


async def test_every_bad_token_is_the_same_404(client, session, world):
    for raw in (None, "", "nonsense", "moa_docs_" + "x" * 43, "moa_docs_" + "x" * 500, "moa_live_" + "x" * 40):
        r = await client.get(ACCESS, params={"t": raw} if raw is not None else None)
        assert r.status_code == 404 and r.json() == {"ok": False}, raw


# ─── The admin's links ───────────────────────────────────────────────────


async def test_an_admins_own_link_dies_with_their_role(client, session, world, mint_token):
    h = auth_header(await mint_token(world.admin))
    r = await client.post(ADMIN, json={"purpose": "self"}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()["link"]["kind"] == "admin" and r.json()["link"]["purpose"] == "self"
    assert timedelta(days=6, hours=23) < datetime.fromisoformat(r.json()["link"]["expires_at"]) - _now() <= timedelta(days=7)
    raw = token_in(r.json()["url"])
    assert (await check(client, raw)).json()["kind"] == "admin"
    world.admin.admin_role = AdminRoles.viewer
    await session.commit()
    assert (await check(client, raw)).status_code == 404
    world.admin.admin_role = AdminRoles.owner
    await session.commit()
    assert (await check(client, raw)).status_code == 200
    world.admin.is_active = False
    await session.commit()
    assert (await check(client, raw)).status_code == 404


async def test_a_partner_apps_link_dies_with_the_app(client, session, world, mint_token):
    h = auth_header(await mint_token(world.admin))
    app = await register_app(client, mint_token, world.admin)
    r = await client.post(ADMIN, json={"partner_app_id": app["id"], "label": "EventHub devs"}, headers=h)
    assert r.status_code == 201, r.text
    link = r.json()["link"]
    assert link["kind"] == "partner_app" and link["purpose"] == "share" and link["label"] == "EventHub devs"
    assert link["partner_app"] == {"id": app["id"], "name": "Campus ERP"}
    assert timedelta(days=29, hours=23) < datetime.fromisoformat(link["expires_at"]) - _now() <= timedelta(days=30)
    raw = token_in(r.json()["url"])
    found = (await check(client, raw)).json()
    assert found["kind"] == "partner_app" and found["partner_app"] == {"id": app["id"], "name": "Campus ERP"} and "club" not in found
    assert (await client.post(f"{API}/admin/integrations/partner-apps/{app['id']}/suspend", json={"reason": "x"}, headers=h)).status_code == 200
    assert (await check(client, raw)).status_code == 404
    r = await client.post(ADMIN, json={"partner_app_id": app["id"]}, headers=h)
    assert r.status_code == 409 and r.json()["error_code"] == "APP_SUSPENDED"
    await client.post(f"{API}/admin/integrations/partner-apps/{app['id']}/resume", headers=h)
    assert (await check(client, raw)).status_code == 200
    row = await session.get(PartnerApps, uuid.UUID(app["id"]))
    await session.delete(row)
    await session.commit()
    assert (await check(client, raw)).status_code == 404


async def test_the_admin_lists_filters_and_revokes_every_link(client, session, world, mint_token):
    h = auth_header(await mint_token(world.admin))
    club_link = await make(client, await owner_h(mint_token, world), "share", label="Agency")
    app = await register_app(client, mint_token, world.admin)
    partner = (await client.post(ADMIN, json={"partner_app_id": app["id"]}, headers=h)).json()
    own = (await client.post(ADMIN, json={"purpose": "self"}, headers=h)).json()
    listed = (await client.get(ADMIN, headers=h)).json()["links"]
    assert [x["id"] for x in listed] == [own["link"]["id"], partner["link"]["id"], club_link["link"]["id"]]
    assert listed[2]["club"] == {"id": world.ids.club, "name": "Robotics Club"} and listed[1]["partner_app"]["name"] == "Campus ERP"
    assert "url" not in json.dumps(listed)
    only = (await client.get(ADMIN, params={"partner_app_id": app["id"]}, headers=h)).json()["links"]
    assert [x["id"] for x in only] == [partner["link"]["id"]]
    assert [x["id"] for x in (await client.get(ADMIN, params={"kind": "club"}, headers=h)).json()["links"]] == [club_link["link"]["id"]]
    assert (await client.get(ADMIN, params={"club_id": world.ids.club}, headers=h)).json()["links"][0]["id"] == club_link["link"]["id"]
    r = await client.delete(f"{ADMIN}/{club_link['link']['id']}", headers=h)
    assert r.status_code == 200 and r.json() == {"revoked": True}
    assert (await check(client, token_in(club_link["url"]))).status_code == 404
    assert (await client.get(CLUB, headers=await owner_h(mint_token, world))).json()["links"][0]["status"] == "revoked"
    audits = list(await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action.like("partner.docs_link.%"))))
    assert sorted(a.action for a in audits) == ["partner.docs_link.create"] * 3 + ["partner.docs_link.revoke"]
    assert {a.entity_type for a in audits} == {"club", "partner_app", "user"}


async def test_the_admins_routes_need_manage_content(client, session, world, mint_token, make_admin):
    viewer = await make_admin(email="viewer@example.com", role=AdminRoles.viewer)
    await session.commit()
    h = auth_header(await mint_token(viewer))
    assert (await client.post(ADMIN, json={"purpose": "self"}, headers=h)).status_code == 403
    assert (await client.get(ADMIN, headers=h)).status_code == 403
    assert (await client.delete(f"{ADMIN}/{uuid.uuid4()}", headers=h)).status_code == 403
    # A club cannot use them; an admin must say what the link is for.
    assert (await client.get(ADMIN, headers=await owner_h(mint_token, world))).status_code == 403
    oh = auth_header(await mint_token(world.admin))
    assert (await client.post(ADMIN, json={}, headers=oh)).status_code == 422
    assert (await client.post(ADMIN, json={"purpose": "share"}, headers=oh)).status_code == 422
    r = await client.post(ADMIN, json={"partner_app_id": str(uuid.uuid4())}, headers=oh)
    assert r.status_code == 404 and r.json()["error_code"] == "APP_NOT_FOUND"


# ─── The check's rate limit ──────────────────────────────────────────────


async def test_the_check_is_limited_per_address_and_per_visitor_behind_the_site(client, session, world, monkeypatch):
    monkeypatch.setattr(settings, "SITE_TOKENS", SITE_TOKEN)
    raw = "moa_docs_" + "q" * 43
    seen = [(await check(client, raw, **{"X-Forwarded-For": "49.37.10.1"})).status_code for _ in range(30)]
    assert seen == [404] * 30
    assert (await check(client, raw, **{"X-Forwarded-For": "49.37.10.1"})).status_code == 429
    assert (await check(client, raw, **{"X-Forwarded-For": "49.37.10.2"})).status_code == 404
    # The site's server asks for each visitor: the visitor it names is counted, behind a valid token only.
    site = {"X-Forwarded-For": VERCEL, "X-Site-Token": SITE_TOKEN}
    assert [(await check(client, raw, **site, **{"X-Visitor-IP": f"49.37.20.{i}"})).status_code for i in range(40)] == [404] * 40
    assert (await check(client, raw, **site, **{"X-Visitor-IP": "49.37.10.1"})).status_code == 429
    spoofed = {"X-Forwarded-For": "49.37.30.1", "X-Site-Token": secrets.token_hex(32)}
    assert [(await check(client, raw, **spoofed, **{"X-Visitor-IP": f"10.0.0.{i}"})).status_code for i in range(31)][-1] == 429


async def test_the_check_is_in_no_openapi_document(client, session, world):
    from doth import application

    assert not [p for p in application.openapi()["paths"] if "developers" in p]
    doc = (await client.get(f"{V1}/openapi.json", headers=H(await key_for(session, world.ids.club, ["events:read"])))).json()
    assert not [p for p in doc["paths"] if "developers" in p or "docs-links" in p]


# ─── The OpenAPI document's gate (decision 6) ────────────────────────────


async def test_the_document_needs_a_credential_and_says_so_in_the_api_shape(client, session, world):
    r = await client.get(f"{V1}/openapi.json")
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "UNAUTHENTICATED" and err["request_id"] and "private" in err["message"]
    assert r.headers["WWW-Authenticate"].startswith("Bearer") and "no-store" in r.headers["Cache-Control"]
    assert "paths" not in r.text
    for headers in ({"Authorization": "Bearer nonsense"}, H("moa_live_" + "x" * 40), {"MOA-Docs-Token": "moa_docs_" + "x" * 43},
                    {"X-Site-Token": secrets.token_hex(32)}):
        assert (await client.get(f"{V1}/openapi.json", headers=headers)).status_code == 401, headers
    assert (await client.get(f"{V1}/openapi.json", params={"docs_token": "moa_docs_" + "x" * 43})).status_code == 401


def _is_the_document(r) -> bool:
    return r.status_code == 200 and r.json()["openapi"] == "3.1.0" and r.json()["paths"]


async def test_a_club_key_live_or_test_opens_the_document_while_it_works(client, session, world):
    from app.api.partner import keys
    from app.api.partner.models import PartnerApiKeys

    live = await key_for(session, world.ids.club, ["events:read"])
    r = await client.get(f"{V1}/openapi.json", headers=H(live))
    assert _is_the_document(r)
    assert r.headers["Cache-Control"] == "private, no-store" and "MOA-Docs-Token" in r.headers["Vary"]
    test = await key_for(session, world.ids.club, ["events:read"], livemode=False)
    assert _is_the_document(await client.get(f"{V1}/openapi.json", headers=H(test)))
    row = await session.scalar(select(PartnerApiKeys).where(PartnerApiKeys.key_hash == keys.digest(live)))
    await keys.revoke(session, row)
    await session.commit()
    assert (await client.get(f"{V1}/openapi.json", headers=H(live))).status_code == 401


async def test_a_partner_apps_token_opens_the_document(client, session, world, mint_token):
    app = await register_app(client, mint_token, world.admin)
    r = await client.post("/partner/oauth/token", data={"grant_type": "client_credentials"}, headers=basic(app))
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    assert _is_the_document(await client.get(f"{V1}/openapi.json", headers=H(token)))
    h = auth_header(await mint_token(world.admin))
    await client.post(f"{API}/admin/integrations/partner-apps/{app['id']}/suspend", json={"reason": "x"}, headers=h)
    assert (await client.get(f"{V1}/openapi.json", headers=H(token))).status_code == 401


async def test_a_docs_token_opens_the_document_in_the_header_or_the_query(client, session, world, mint_token):
    h = await owner_h(mint_token, world)
    made = await make(client, h, "share")
    raw = token_in(made["url"])
    assert _is_the_document(await client.get(f"{V1}/openapi.json", headers={"MOA-Docs-Token": raw}))
    assert _is_the_document(await client.get(f"{V1}/openapi.json", params={"docs_token": raw}))
    row = await link_row(session, raw)
    await session.refresh(row)
    assert row.use_count == 1  # at most once a minute
    await client.delete(f"{CLUB}/{made['link']['id']}", headers=h)
    assert (await client.get(f"{V1}/openapi.json", headers={"MOA-Docs-Token": raw})).status_code == 401


async def test_the_websites_server_opens_the_document_with_its_token(client, session, world, monkeypatch):
    monkeypatch.setattr(settings, "SITE_TOKENS", SITE_TOKEN)
    assert _is_the_document(await client.get(f"{V1}/openapi.json", headers={"X-Site-Token": SITE_TOKEN}))
    assert (await client.get(f"{V1}/openapi.json", headers={"X-Site-Token": secrets.token_hex(32)})).status_code == 401
    monkeypatch.setattr(settings, "PARTNER_API_ENABLED", False)
    assert (await client.get(f"{V1}/openapi.json", headers={"X-Site-Token": SITE_TOKEN})).status_code == 404


async def test_the_club_docs_url_stays_the_public_page(client, world, mint_token):
    overview = (await client.get(f"{API}/clubs/integrations", headers=await owner_h(mint_token, world))).json()
    assert overview["docs_url"] == "https://myotherapp.in/developers"
