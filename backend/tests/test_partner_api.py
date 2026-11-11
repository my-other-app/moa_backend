"""The Integrations API (app/api/partner, docs/INTEGRATIONS_API.md,
feature-register/integrations-api-contract.md).

Who may call and how: keys (hashed, shown once, scopes, expiry, IP
allowlist, revoke, rotate), the admin's allocation, partner apps over OAuth
(PKCE, refresh rotation and reuse, revoke, client credentials, suspension,
disconnecting). Then what every route keeps: the error shape and headers,
rate limits, Idempotency-Key, external ids, ETags, cursors and
updated_since. Then the resources through the domain services -- events,
registrations and their money rules, check-ins, certificates, the club,
clubs:create -- the sandbox test credentials work on, the audit log, and
the OpenAPI document.
"""
import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.api.audit.models import AdminAuditLogs
from app.api.checkins.models import CheckIns
from app.api.club_access.models import ClubMembers
from app.api.closeout.models import EventStatus
from app.api.events.models import EventCategories, EventRegistrationsLink, Events
from app.api.partner import allocation, keys
from app.api.partner.models import (
    PartnerAllocations, PartnerApiKeys, PartnerConnections, PartnerExternalIds, PartnerExternalPayments, PartnerIdempotency,
    PartnerSandboxObjects, PartnerUsageDaily,
)
from app.config import settings
from app.core.auth.authentication import get_password_hash
from tests.conftest import auth_header

V1 = "/partner/v1"
API = "/api/v1"
NOW = datetime.now(timezone.utc)
REDIRECT = "https://partner.example.com/callback"
EVERYTHING = ["club:read", "club:write", "events:read", "events:write", "events:publish", "registrations:read",
              "registrations:write", "attendees:read", "checkins:write", "certificates:read", "certificates:write"]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone(timedelta(hours=5, minutes=30))).isoformat()


@pytest.fixture(autouse=True)
def partner_on(monkeypatch):
    monkeypatch.setattr(settings, "PARTNER_API_ENABLED", True)
    from app.api.partner import openapi

    openapi.reset()


@pytest_asyncio.fixture
async def world(session, make_club, make_event, make_user, make_admin):
    owner, club = await make_club(name="Robotics Club", email="robotics@example.com")
    session.add(EventCategories(name="Workshop", created_by_id=owner.id))
    await session.flush()
    upcoming = await make_event(club=club, created_by=owner, name="Bot Build Night", event_datetime=NOW + timedelta(days=10), duration=3.0, max_participants=50)
    other_owner, other_club = await make_club(name="Chess Club", email="chess@example.com")
    secret = await make_event(club=other_club, created_by=other_owner, name="Chess Secret Summit", event_datetime=NOW + timedelta(days=3))
    await allocation.put(session, club.id, enabled=True)
    admin = await make_admin(email="ops@example.com")
    await session.commit()
    return SimpleNamespace(
        owner=owner, club=club, other_club=other_club, admin=admin,
        ids=SimpleNamespace(club=club.id, other_club=other_club.id, upcoming=upcoming.id, secret=secret.id, owner=owner.id),
    )


async def key_for(session, club_id, scopes=EVERYTHING, *, livemode=True, ip_allowlist=None, expires_at=None, name="Our website"):
    _row, raw = await keys.create(
        session, club_id, name=name, livemode=livemode, scopes=scopes, ip_allowlist=ip_allowlist, attendees_ack=True, expires_at=expires_at,
    )
    await session.commit()
    return raw


def H(raw: str, **extra) -> dict:
    return {"Authorization": f"Bearer {raw}", **extra}


def event_body(**over) -> dict:
    body = {
        "name": "API Demo Day", "description": "Made by a partner.", "starts_at": iso(NOW + timedelta(days=20)),
        "ends_at": iso(NOW + timedelta(days=20, hours=3)), "venue": {"name": "Lab 3", "city": "Kozhikode"}, "capacity": 2,
    }
    body.update(over)
    return body


def person(n: int = 1) -> dict:
    return {"person": {"name": f"Guest {n}", "email": f"guest{n}@example.com", "phone": "9988776655"}}


async def published_event(client, raw, **over) -> str:
    r = await client.post(f"{V1}/events", json=event_body(**over), headers=H(raw))
    assert r.status_code == 201, r.text
    event_id = r.json()["id"]
    r = await client.post(f"{V1}/events/{event_id}/publish", headers=H(raw))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "published"
    return event_id


async def club_allows_paid_outside(client, session, mint_token, owner, event_id: int):
    """The club turns on "Accept registrations paid outside MyOtherApp" the one way it can: the dashboard's edit form."""
    ev = await session.get(Events, event_id, populate_existing=True)
    form = {"name": ev.name, "event_datetime": ev.event_datetime.isoformat(), "duration": str(ev.duration), "category_id": str(ev.category_id),
            "has_fee": "true" if ev.has_fee else "false", "reg_startdate": ev.reg_startdate.isoformat(), "external_payments_allowed": "true"}
    if ev.reg_fee is not None:
        form["reg_fee"] = str(ev.reg_fee)
    if ev.max_participants:
        form["max_participants"] = str(ev.max_participants)
    r = await client.put(f"{API}/events/update/{event_id}", data=form, headers=auth_header(await mint_token(owner)))
    assert r.status_code == 200, r.text
    assert r.json()["external_payments_allowed"] is True


# ─── Who may call ────────────────────────────────────────────────────────


async def test_no_credential_is_401_in_the_api_shape_with_its_headers(client, world):
    r = await client.get(f"{V1}/me")
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "UNAUTHENTICATED" and err["request_id"] and r.headers["X-Request-Id"] == err["request_id"]
    assert r.headers["WWW-Authenticate"].startswith("Bearer")
    r = await client.get(f"{V1}/me", headers=H("moa_live_" + "x" * 40))
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"
    r = await client.get(f"{V1}/me", headers=H("nonsense"))
    assert r.status_code == 401


async def test_me_says_who_the_key_is(client, session, world):
    raw = await key_for(session, world.ids.club, ["events:read"])
    r = await client.get(f"{V1}/me", headers=H(raw))
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["type"] == "api_key" and me["club"] == {"id": str(world.ids.club), "name": "Robotics Club", "slug": me["club"]["slug"]}
    assert me["partner"] is None and me["scopes"] == ["events:read"] and me["livemode"] is True
    assert me["rate_limit"] == {"read_per_min": 600, "write_per_min": 120}
    for header in ("X-Request-Id", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
        assert header in r.headers
    assert r.headers["X-RateLimit-Limit"] == "600" and r.headers["Cache-Control"] == "no-store"


async def test_a_key_is_stored_only_as_its_digest_and_shown_once(client, session, world, mint_token):
    h = auth_header(await mint_token(world.owner))
    r = await client.post(f"{API}/clubs/integrations/keys", json={"name": "ERP", "mode": "live", "scopes": ["events:read"]}, headers=h)
    assert r.status_code == 201, r.text
    secret = r.json()["secret"]
    assert secret.startswith("moa_live_") and len(secret) == len("moa_live_") + 40
    assert r.json()["prefix"] == secret[:17]
    row = await session.scalar(select(PartnerApiKeys).where(PartnerApiKeys.client_id == r.json()["client_id"]))
    assert row.key_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in json.dumps({c.name: str(getattr(row, c.name)) for c in row.__table__.columns})
    listed = (await client.get(f"{API}/clubs/integrations/keys", headers=h)).json()["keys"]
    assert len(listed) == 1 and "secret" not in listed[0] and listed[0]["scopes"] == ["events:read"]
    assert (await client.get(f"{V1}/me", headers=H(secret))).status_code == 200
    # Test mode has its own prefix.
    r = await client.post(f"{API}/clubs/integrations/keys", json={"name": "Sandbox", "mode": "test", "scopes": ["events:read"]}, headers=h)
    assert r.json()["secret"].startswith("moa_test_") and r.json()["livemode"] is False


async def test_keys_need_the_allocation_room_and_the_disclosure_tick(client, session, world, mint_token, make_club):
    h = auth_header(await mint_token(world.owner))
    r = await client.post(f"{API}/clubs/integrations/keys", json={"name": "x", "scopes": ["attendees:read"]}, headers=h)
    assert r.status_code == 422 and r.json()["error_code"] == "DISCLOSURE_REQUIRED"
    r = await client.post(f"{API}/clubs/integrations/keys", json={"name": "x", "scopes": ["clubs:create"]}, headers=h)
    assert r.status_code == 422
    r = await client.post(f"{API}/clubs/integrations/keys", json={"name": "x", "scopes": ["attendees:read"], "attendees_disclosure_ack": True}, headers=h)
    assert r.status_code == 201
    await allocation.put(session, world.ids.club, enabled=True, max_keys=1)
    await session.commit()
    r = await client.post(f"{API}/clubs/integrations/keys", json={"name": "y", "scopes": ["events:read"]}, headers=h)
    assert r.status_code == 409 and r.json()["error_code"] == "KEY_LIMIT"
    # A club the admin never allocated cannot make one.
    other, _club = await make_club(name="Quiz Club", email="quiz@example.com")
    await session.commit()
    r = await client.post(f"{API}/clubs/integrations/keys", json={"name": "z", "scopes": ["events:read"]}, headers=auth_header(await mint_token(other)))
    assert r.status_code == 403 and r.json()["error_code"] == "NOT_ALLOCATED"
    overview = (await client.get(f"{API}/clubs/integrations", headers=auth_header(await mint_token(other)))).json()
    assert overview["allocated"] is False and overview["source"] is None


async def test_only_the_owner_and_admins_manage_integrations(client, session, world):
    from app.api.auth.schemas import AuthTokenData
    from app.core.auth.jwt import create_access_token

    seen = {}
    for role in ("editor", "door", "admin"):
        m = ClubMembers(club_id=world.ids.club, email=f"{role}@robotics.example.com", full_name=role, role=role, status="active",
                        password=get_password_hash("Password123!"))
        session.add(m)
        await session.commit()
        await session.refresh(world.owner)
        token = create_access_token(data=AuthTokenData(
            user_id=world.ids.owner, token_type="access_token", token_version=world.owner.token_version, member_id=m.id,
        ).model_dump())
        seen[role] = (await client.get(f"{API}/clubs/integrations/keys", headers=auth_header(token))).status_code
    assert seen == {"editor": 403, "door": 403, "admin": 200}


async def test_revoked_expired_and_wrong_address_keys_are_refused(client, session, world):
    raw = await key_for(session, world.ids.club, ["events:read"])
    row = await session.scalar(select(PartnerApiKeys).where(PartnerApiKeys.key_hash == keys.digest(raw)))
    row.expires_at = NOW - timedelta(seconds=1)
    await session.commit()
    r = await client.get(f"{V1}/me", headers=H(raw))
    assert r.status_code == 401 and "expired" in r.json()["error"]["message"]
    fenced = await key_for(session, world.ids.club, ["events:read"], ip_allowlist=["203.0.113.0/24"])
    r = await client.get(f"{V1}/me", headers=H(fenced))
    assert r.status_code == 403 and r.json()["error"]["code"] == "IP_NOT_ALLOWED"
    r = await client.get(f"{V1}/me", headers=H(fenced, **{"X-Forwarded-For": "203.0.113.7"}))
    assert r.status_code == 200, r.text
    # The allowlist must be addresses.
    with pytest.raises(Exception):
        keys.check_allowlist(["not-an-ip"])


async def test_rotating_keeps_the_key_and_ends_the_old_secret_revoking_ends_it(client, session, world, mint_token):
    h = auth_header(await mint_token(world.owner))
    made = (await client.post(f"{API}/clubs/integrations/keys", json={"name": "ERP", "scopes": ["events:read"]}, headers=h)).json()
    rotated = (await client.post(f"{API}/clubs/integrations/keys/{made['id']}/rotate", headers=h)).json()
    assert rotated["client_id"] == made["client_id"] and rotated["secret"] != made["secret"] and rotated["rotated_at"]
    assert (await client.get(f"{V1}/me", headers=H(made["secret"]))).status_code == 401
    assert (await client.get(f"{V1}/me", headers=H(rotated["secret"]))).status_code == 200
    assert (await client.delete(f"{API}/clubs/integrations/keys/{made['id']}", headers=h)).status_code == 200
    assert (await client.get(f"{V1}/me", headers=H(rotated["secret"]))).status_code == 401
    audits = set(await session.scalars(select(AdminAuditLogs.action).where(AdminAuditLogs.action.like("partner.key.%"))))
    assert {"partner.key.create", "partner.key.rotate", "partner.key.revoke"} <= audits


async def test_the_allocation_turned_off_closes_every_key_at_once(client, session, world, mint_token):
    raw = await key_for(session, world.ids.club, ["events:read"])
    admin_h = auth_header(await mint_token(world.admin))
    r = await client.put(f"{API}/admin/integrations/clubs/{world.ids.club}", json={"enabled": False, "notes": "Paused"}, headers=admin_h)
    assert r.status_code == 200, r.text
    r = await client.get(f"{V1}/me", headers=H(raw))
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_ALLOCATED"
    r = await client.put(f"{API}/admin/integrations/clubs/{world.ids.club}", json={"enabled": True, "rate_tier": "high", "max_keys": 9}, headers=admin_h)
    assert r.json()["rate_limit"] == {"read_per_min": 3000, "write_per_min": 600}
    r = await client.get(f"{V1}/me", headers=H(raw))
    assert r.status_code == 200 and r.json()["rate_limit"]["read_per_min"] == 3000
    listed = (await client.get(f"{API}/admin/integrations/clubs", headers=admin_h)).json()["clubs"]
    assert any(c["club"]["id"] == world.ids.club and c["live_keys"] == 1 for c in listed)
    detail = (await client.get(f"{API}/admin/integrations/clubs/{world.ids.club}", headers=admin_h)).json()
    assert detail["max_keys"] == 9 and len(detail["keys"]) == 1 and len(detail["usage"]) == 30


async def test_the_plan_flag_is_declared_and_off(session, world):
    from app.api.subscriptions.models import FEATURE_KEYS, PlanFeatures

    assert "api_access" in FEATURE_KEYS and PlanFeatures.__table__.c.api_access.server_default.arg == "false"
    # No row and no plan with it: not allocated.
    assert (await allocation.of(session, world.ids.other_club)).allocated is False


async def test_scopes_are_held_on_every_route(client, session, world):
    raw = await key_for(session, world.ids.club, ["events:read"])
    r = await client.post(f"{V1}/events", json=event_body(), headers=H(raw))
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN_SCOPE"
    assert r.json()["error"]["details"]["needed"] == ["events:write"]
    assert (await client.get(f"{V1}/events/{world.ids.upcoming}/registrations", headers=H(raw))).status_code == 403
    assert (await client.get(f"{V1}/events", headers=H(raw))).status_code == 200


# ─── What every route keeps ──────────────────────────────────────────────


async def test_rate_limits_answer_429_with_retry_after(client, session, world, monkeypatch):
    monkeypatch.setitem(allocation.LIMITS, "standard", (2, 1))
    raw = await key_for(session, world.ids.club, ["events:read", "events:write"])
    first = await client.get(f"{V1}/events", headers=H(raw))
    assert first.headers["X-RateLimit-Limit"] == "2" and first.headers["X-RateLimit-Remaining"] == "1"
    await client.get(f"{V1}/events", headers=H(raw))
    r = await client.get(f"{V1}/events", headers=H(raw))
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    assert int(r.headers["Retry-After"]) >= 1 and r.headers["X-RateLimit-Remaining"] == "0"
    # Writes have a bucket of their own.
    assert (await client.post(f"{V1}/events", json=event_body(), headers=H(raw))).status_code == 201
    assert (await client.post(f"{V1}/events", json=event_body(name="Second one"), headers=H(raw))).status_code == 429
    usage = await session.scalar(select(func.sum(PartnerUsageDaily.rate_limited)))
    assert usage == 2


async def test_an_idempotency_key_replays_the_first_answer_and_refuses_another_body(client, session, world):
    raw = await key_for(session, world.ids.club)
    h = H(raw, **{"Idempotency-Key": "create-demo-1"})
    first = await client.post(f"{V1}/events", json=event_body(), headers=h)
    again = await client.post(f"{V1}/events", json=event_body(), headers=h)
    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() and again.headers["Idempotent-Replayed"] == "true" and again.headers["ETag"] == first.headers["ETag"]
    assert await session.scalar(select(func.count()).select_from(Events).where(Events.name == "API Demo Day")) == 1
    r = await client.post(f"{V1}/events", json=event_body(name="Something else"), headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "IDEMPOTENCY_MISMATCH"
    # Another client's same key is its own.
    other = await key_for(session, world.ids.club, name="Another")
    r = await client.post(f"{V1}/events", json=event_body(name="Their own"), headers=H(other, **{"Idempotency-Key": "create-demo-1"}))
    assert r.status_code == 201
    row = await session.scalar(select(PartnerIdempotency).where(PartnerIdempotency.key == "create-demo-1").limit(1))
    assert row.expires_at - row.created_at == timedelta(hours=24)


async def test_a_refusal_is_kept_for_its_key_too(client, session, world):
    raw = await key_for(session, world.ids.club)
    h = H(raw, **{"Idempotency-Key": "bad-1"})
    bad = event_body(starts_at=iso(NOW - timedelta(days=1)), ends_at=iso(NOW))
    first = await client.post(f"{V1}/events", json=bad, headers=h)
    again = await client.post(f"{V1}/events", json=bad, headers=h)
    assert first.status_code == again.status_code == 422 and again.headers.get("Idempotent-Replayed") == "true"


async def test_external_ids_find_objects_and_refuse_a_second_post(client, session, world):
    raw = await key_for(session, world.ids.club)
    made = await client.post(f"{V1}/events", json=event_body(external_id="evt-31"), headers=H(raw))
    assert made.status_code == 201 and made.json()["external_id"] == "evt-31"
    r = await client.post(f"{V1}/events", json=event_body(external_id="evt-31", name="Again"), headers=H(raw))
    assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_EXISTS" and r.json()["error"]["details"]["id"] == made.json()["id"]
    found = (await client.get(f"{V1}/events", params={"external_id": "evt-31"}, headers=H(raw))).json()["data"]
    assert [e["id"] for e in found] == [made.json()["id"]]
    # Another credential's ids are its own: it may use the same one, and does not see the first's.
    other = await key_for(session, world.ids.club, name="ERP")
    theirs = await client.get(f"{V1}/events/{made.json()['id']}", headers=H(other))
    assert theirs.json()["external_id"] is None
    assert (await client.post(f"{V1}/events", json=event_body(external_id="evt-31", name="ERP's"), headers=H(other))).status_code == 201
    # PATCH moves it.
    r = await client.patch(f"{V1}/events/{made.json()['id']}", json={"external_id": "evt-32"}, headers=H(raw))
    assert r.status_code == 200 and r.json()["external_id"] == "evt-32"
    assert await session.scalar(select(func.count()).select_from(PartnerExternalIds).where(PartnerExternalIds.object_id == "pending")) == 0


async def test_etags_if_match_412_and_if_none_match_304(client, session, world):
    raw = await key_for(session, world.ids.club)
    made = await client.post(f"{V1}/events", json=event_body(), headers=H(raw))
    event_id, tag = made.json()["id"], made.headers["ETag"]
    r = await client.get(f"{V1}/events/{event_id}", headers=H(raw, **{"If-None-Match": tag}))
    assert r.status_code == 304
    r = await client.patch(f"{V1}/events/{event_id}", json={"capacity": 80}, headers=H(raw, **{"If-Match": tag}))
    assert r.status_code == 200, r.text
    assert r.json()["capacity"] == 80 and r.headers["ETag"] != tag
    r = await client.patch(f"{V1}/events/{event_id}", json={"capacity": 90}, headers=H(raw, **{"If-Match": tag}))
    assert r.status_code == 412 and r.json()["error"]["code"] == "PRECONDITION_FAILED"
    assert (await client.get(f"{V1}/events/{event_id}", headers=H(raw))).json()["capacity"] == 80


async def test_lists_page_by_cursor_and_sync_by_updated_since(client, session, world):
    raw = await key_for(session, world.ids.club)
    ids = [(await client.post(f"{V1}/events", json=event_body(name=f"Event {i}"), headers=H(raw))).json()["id"] for i in range(3)]
    page = (await client.get(f"{V1}/events", params={"limit": 2}, headers=H(raw))).json()
    assert len(page["data"]) == 2 and page["next_cursor"]
    rest = (await client.get(f"{V1}/events", params={"limit": 2, "cursor": page["next_cursor"]}, headers=H(raw))).json()
    seen = [e["id"] for e in page["data"] + rest["data"]]
    assert seen == sorted(seen, key=int) and set(ids) <= set(seen) and rest["next_cursor"] is None
    later = datetime.now(timezone.utc)
    await client.patch(f"{V1}/events/{ids[0]}", json={"description": "Changed"}, headers=H(raw))
    changed = (await client.get(f"{V1}/events", params={"updated_since": later.isoformat()}, headers=H(raw))).json()["data"]
    assert [e["id"] for e in changed] == [ids[0]]
    assert (await client.get(f"{V1}/events", params={"cursor": "garbage"}, headers=H(raw))).status_code == 422
    assert (await client.get(f"{V1}/events", params={"limit": 101}, headers=H(raw))).status_code == 422
    drafts = (await client.get(f"{V1}/events", params={"status": "draft"}, headers=H(raw))).json()["data"]
    assert {e["id"] for e in drafts} == set(ids)


async def test_a_body_with_an_unknown_field_is_a_validation_error(client, session, world):
    raw = await key_for(session, world.ids.club)
    r = await client.post(f"{V1}/events", json={**event_body(), "nmae": "typo"}, headers=H(raw))
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION" and "nmae" in r.json()["error"]["details"]["fields"]
    r = await client.post(f"{V1}/events", json=event_body(starts_at="2026-12-01T10:00:00"), headers=H(raw))
    assert r.status_code == 422 and "offset" in r.json()["error"]["message"]


# ─── Events ──────────────────────────────────────────────────────────────


async def test_events_are_drafts_until_published_and_another_clubs_are_not_found(client, session, world):
    raw = await key_for(session, world.ids.club)
    made = (await client.post(f"{V1}/events", json=event_body(), headers=H(raw))).json()
    assert made["status"] == "draft" and made["public_url"] is None and made["livemode"] is True
    assert made["venue"]["name"] == "Lab 3, Kozhikode" and made["ticket_types"] and made["timezone"] == "Asia/Kolkata"
    row = await session.get(Events, int(made["id"]))
    assert row.lifecycle is EventStatus.draft and row.club_id == world.ids.club and row.max_participants == 2
    published = (await client.post(f"{V1}/events/{made['id']}/publish", headers=H(raw))).json()
    assert published["status"] == "published" and published["public_url"].endswith(f"/events/{published['slug']}")
    assert (await client.get(f"{V1}/events/{world.ids.secret}", headers=H(raw))).status_code == 404
    listed = {e["id"] for e in (await client.get(f"{V1}/events", headers=H(raw))).json()["data"]}
    assert str(world.ids.secret) not in listed and str(world.ids.upcoming) in listed
    actions = set(await session.scalars(select(AdminAuditLogs.action).where(AdminAuditLogs.entity_id == made["id"])))
    assert {"partner.event.create", "partner.event.publish"} <= actions
    line = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "partner.event.create"))
    assert line.details["via"] == "partner_api" and line.details["client_type"] == "api_key" and line.actor_id == world.ids.owner


async def test_an_event_is_edited_through_the_dashboards_own_service(client, session, world):
    raw = await key_for(session, world.ids.club)
    made = (await client.post(f"{V1}/events", json=event_body(), headers=H(raw))).json()
    r = await client.patch(f"{V1}/events/{made['id']}", json={
        "name": "Renamed Day", "starts_at": iso(NOW + timedelta(days=21)), "ends_at": iso(NOW + timedelta(days=21, hours=5)),
        "price": {"amount": 0}, "registration": {"requires_approval": True},
    }, headers=H(raw))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["name"] == "Renamed Day" and out["registration"]["requires_approval"] is True and out["external_payments_allowed"] is False
    row = await session.get(Events, int(made["id"]), populate_existing=True)
    assert row.duration == 5 and row.requires_approval and not row.external_payments_allowed
    r = await client.patch(f"{V1}/events/{made['id']}", json={"ends_at": iso(NOW + timedelta(days=30))}, headers=H(raw))
    assert r.status_code == 422


async def test_only_the_club_turns_on_payments_taken_outside(client, session, world, mint_token):
    """Decision 5: the API reads `external_payments_allowed` but never sets it (422 FIELD_READ_ONLY), live or test."""
    raw = await key_for(session, world.ids.club)
    test = await key_for(session, world.ids.club, livemode=False, name="Sandbox")
    for key in (raw, test):
        r = await client.post(f"{V1}/events", json=event_body(external_payments_allowed=True), headers=H(key))
        assert r.status_code == 422 and r.json()["error"]["code"] == "FIELD_READ_ONLY"
        assert r.json()["error"]["details"]["fields"] == {"external_payments_allowed": "Read-only."}
    for value in (True, False):
        r = await client.patch(f"{V1}/events/{world.ids.upcoming}", json={"external_payments_allowed": value}, headers=H(raw))
        assert r.status_code == 422 and r.json()["error"]["code"] == "FIELD_READ_ONLY"
    assert (await session.get(Events, world.ids.upcoming, populate_existing=True)).external_payments_allowed is False
    # The club turns it on in the dashboard; the API shows it, and a partner still can't turn it off.
    await club_allows_paid_outside(client, session, mint_token, world.owner, world.ids.upcoming)
    assert (await client.get(f"{V1}/events/{world.ids.upcoming}", headers=H(raw))).json()["external_payments_allowed"] is True
    r = await client.patch(f"{V1}/events/{world.ids.upcoming}", json={"external_payments_allowed": False, "capacity": 70}, headers=H(raw))
    assert r.status_code == 422
    row = await session.get(Events, world.ids.upcoming, populate_existing=True)
    assert row.external_payments_allowed is True and row.max_participants == 50
    # An edit through the API leaves the club's setting as it is.
    assert (await client.patch(f"{V1}/events/{world.ids.upcoming}", json={"capacity": 70}, headers=H(raw))).json()["external_payments_allowed"] is True
    schema = (await client.get(f"{V1}/openapi.json")).json()["components"]["schemas"]
    assert schema["EventCreate"]["properties"]["external_payments_allowed"]["readOnly"] is True


async def test_cancelling_refuses_an_event_with_online_payments(client, session, world, monkeypatch):
    raw = await key_for(session, world.ids.club)
    from app.api.club_ops import cancel as cancel_service

    real_state = cancel_service.state

    async def paid_state(session_, event):
        out = await real_state(session_, event)
        return {**out, "paid_online": 3} if event.id == world.ids.upcoming else out

    monkeypatch.setattr(cancel_service, "state", paid_state)
    r = await client.post(f"{V1}/events/{world.ids.upcoming}/cancel", json={"reason": "Rain"}, headers=H(raw))
    assert r.status_code == 409 and r.json()["error"]["code"] == "CANCEL_IN_DASHBOARD" and r.json()["error"]["details"]["paid_online"] == 3
    monkeypatch.setattr(cancel_service, "state", real_state)
    r = await client.post(f"{V1}/events/{world.ids.upcoming}/cancel", json={"reason": "Rain"}, headers=H(raw))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"
    assert (await session.get(Events, world.ids.upcoming, populate_existing=True)).lifecycle is EventStatus.cancelled
    assert (await client.post(f"{V1}/events/{world.ids.upcoming}/publish", headers=H(raw))).status_code == 409


# ─── Registrations ───────────────────────────────────────────────────────


async def test_a_registration_goes_through_the_domain_and_hides_contacts_without_the_scope(client, session, world):
    raw = await key_for(session, world.ids.club)
    reader = await key_for(session, world.ids.club, ["registrations:read"], name="Reader")
    r = await client.post(f"{V1}/events/{world.ids.upcoming}/registrations", json={**person(1), "external_id": "reg-1"}, headers=H(raw))
    assert r.status_code == 201, r.text
    reg = r.json()
    assert reg["status"] == "confirmed" and reg["ticket"]["status"] == "valid" and reg["ticket"]["code"].startswith("MOA")
    assert reg["person"]["email"] == "guest1@example.com" and reg["payment"]["status"] == "not_required"
    assert "sig=" in reg["ticket"]["qr_url"]
    row = await session.get(EventRegistrationsLink, reg["id"])
    assert row is not None and row.event_id == world.ids.upcoming
    assert (await session.get(Events, world.ids.upcoming, populate_existing=True)).going_count == 1
    blind = (await client.get(f"{V1}/registrations/{reg['id']}", headers=H(reader))).json()
    assert "person" not in blind and "answers" not in blind and blind["ticket"]["code"] == reg["ticket"]["code"]
    page = (await client.get(f"{V1}/events/{world.ids.upcoming}/registrations", headers=H(raw))).json()["data"]
    assert page[0]["person"]["name"] == "Guest 1"
    counted = list(await session.scalars(select(AdminAuditLogs).where(AdminAuditLogs.action == "partner.attendees.read")))
    assert counted and all("email" not in json.dumps(c.details) or c.details["fields"] for c in counted)
    assert sum(c.details["count"] for c in counted) >= 1
    qr = await client.get(urlsplit(reg["ticket"]["qr_url"]).path + "?" + urlsplit(reg["ticket"]["qr_url"]).query)
    assert qr.status_code == 200 and qr.headers["content-type"] == "image/png"
    assert (await client.get(f"{V1}/tickets/{reg['ticket']['code']}/qr.png", params={"sig": "forged"})).status_code == 404


async def test_the_same_address_twice_and_a_full_event_are_refused(client, session, world):
    raw = await key_for(session, world.ids.club)
    event_id = await published_event(client, raw, capacity=2)
    assert (await client.post(f"{V1}/events/{event_id}/registrations", json=person(1), headers=H(raw))).status_code == 201
    r = await client.post(f"{V1}/events/{event_id}/registrations", json=person(1), headers=H(raw))
    assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_REGISTERED"
    assert (await client.post(f"{V1}/events/{event_id}/registrations", json=person(2), headers=H(raw))).status_code == 201
    r = await client.post(f"{V1}/events/{event_id}/registrations", json=person(3), headers=H(raw))
    assert r.status_code == 409 and r.json()["error"]["code"] == "EVENT_FULL"


async def test_a_paid_event_refuses_without_a_payment_and_takes_one_paid_outside(client, session, world, make_event, mint_token):
    raw = await key_for(session, world.ids.club)
    paid = await make_event(club=world.club, created_by=world.owner, name="Paid Workshop", event_datetime=NOW + timedelta(days=5),
                            has_fee=True, reg_fee=499, max_participants=10)
    await session.commit()
    paid_id, paid_slug = paid.id, paid.slug
    r = await client.post(f"{V1}/events/{paid_id}/registrations", json=person(1), headers=H(raw))
    assert r.status_code == 409 and r.json()["error"]["code"] == "PAYMENT_REQUIRED"
    assert r.json()["error"]["details"]["checkout_url"].endswith(f"/events/{paid_slug}") and r.json()["error"]["details"]["amount"] == 49900
    with_pay = {**person(1), "payment": {"status": "paid_externally", "amount": 49900, "currency": "INR", "reference": "UPI-77"}}
    r = await client.post(f"{V1}/events/{paid_id}/registrations", json=with_pay, headers=H(raw))
    assert r.status_code == 409 and r.json()["error"]["code"] == "EXTERNAL_PAYMENTS_OFF"
    await club_allows_paid_outside(client, session, mint_token, world.owner, paid_id)
    r = await client.post(f"{V1}/events/{paid_id}/registrations", json=with_pay, headers=H(raw))
    assert r.status_code == 201, r.text
    reg = r.json()
    assert reg["status"] == "confirmed" and reg["payment"] == {"status": "paid_externally", "amount": 49900, "currency": "INR", "reference": "UPI-77"}
    row = await session.get(EventRegistrationsLink, reg["id"], populate_existing=True)
    # Paid at nothing: it holds its paid place, and no income is ours.
    assert row.is_paid is True and float(row.paid_amount) == 0 and row.payment_receipt is None
    ext = await session.get(PartnerExternalPayments, row.id)
    assert ext.amount_paise == 49900 and ext.reference == "UPI-77"
    assert (await session.get(Events, paid_id, populate_existing=True)).paid_count == 1
    # The free-event rule the other way round.
    r = await client.post(f"{V1}/events/{world.ids.upcoming}/registrations", json={**person(5), "payment": with_pay["payment"]}, headers=H(raw))
    assert r.status_code == 422
    # Cancelling one paid outside is the API's; one paid online is the dashboard's.
    assert (await client.post(f"{V1}/registrations/{reg['id']}/cancel", headers=H(raw))).json()["status"] == "cancelled"
    online = (await client.post(f"{V1}/events/{paid_id}/registrations", json={**person(2), "payment": with_pay["payment"]}, headers=H(raw))).json()
    row = await session.get(EventRegistrationsLink, online["id"])
    await session.delete(await session.get(PartnerExternalPayments, row.id))
    row.payment_receipt, row.paid_amount = "rcpt_online_1", 499
    await session.commit()
    r = await client.post(f"{V1}/registrations/{online['id']}/cancel", headers=H(raw))
    assert r.status_code == 409 and r.json()["error"]["code"] == "CANCEL_IN_DASHBOARD"


async def test_an_event_that_reviews_registrations_takes_a_request(client, session, world):
    from app.api.approvals.models import RegistrationRequests

    raw = await key_for(session, world.ids.club)
    event_id = await published_event(client, raw, registration={"requires_approval": True})
    r = await client.post(f"{V1}/events/{event_id}/registrations", json={**person(4), "external_id": "req-4"}, headers=H(raw))
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["status"] == "pending_approval" and out["approval"]["status"] == "pending" and out["ticket"] is None
    assert await session.get(RegistrationRequests, out["id"]) is not None
    assert (await client.get(f"{V1}/registrations/{out['id']}", headers=H(raw))).json()["status"] == "pending_approval"
    waiting = (await client.get(f"{V1}/events/{event_id}/registrations", params={"status": "pending_approval"}, headers=H(raw))).json()["data"]
    assert [w["id"] for w in waiting] == [out["id"]] and waiting[0]["external_id"] == "req-4"
    r = await client.post(f"{V1}/registrations/{out['id']}/cancel", headers=H(raw))
    assert r.json()["status"] == "withdrawn"


async def test_a_registration_is_corrected_with_if_match(client, session, world):
    raw = await key_for(session, world.ids.club)
    reg = await client.post(f"{V1}/events/{world.ids.upcoming}/registrations", json=person(1), headers=H(raw))
    r = await client.patch(f"{V1}/registrations/{reg.json()['id']}", json={"name": "Guest One"}, headers=H(raw, **{"If-Match": reg.headers["ETag"]}))
    assert r.status_code == 200 and r.json()["person"]["name"] == "Guest One"
    r = await client.patch(f"{V1}/registrations/{reg.json()['id']}", json={"name": "Stale"}, headers=H(raw, **{"If-Match": reg.headers["ETag"]}))
    assert r.status_code == 412


# ─── Check-ins ───────────────────────────────────────────────────────────


async def test_a_second_scan_is_a_duplicate_never_a_second_check_in(client, session, world):
    raw = await key_for(session, world.ids.club)
    reg = (await client.post(f"{V1}/events/{world.ids.upcoming}/registrations", json={**person(1), "external_id": "r-1"}, headers=H(raw))).json()
    first = await client.post(f"{V1}/checkins", json={"ticket_code": reg["ticket"]["code"], "gate": "Main gate"}, headers=H(raw))
    assert first.status_code == 201, first.text
    assert first.json()["duplicate"] is False and first.json()["gate"] == "Main gate" and first.json()["via"] == "api"
    again = await client.post(f"{V1}/checkins", json={"external_id": "r-1"}, headers=H(raw))
    assert again.status_code == 200 and again.json()["duplicate"] is True and again.json()["id"] == first.json()["id"]
    assert await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.registration_id == reg["id"])) == 1
    listed = (await client.get(f"{V1}/events/{world.ids.upcoming}/checkins", headers=H(raw))).json()["data"]
    assert [c["id"] for c in listed] == [first.json()["id"]]
    assert (await client.get(f"{V1}/registrations/{reg['id']}", headers=H(raw))).json()["checked_in_at"]
    assert (await client.post(f"{V1}/registrations/{reg['id']}/cancel", headers=H(raw))).json()["error"]["code"] == "ALREADY_CHECKED_IN"
    undone = await client.delete(f"{V1}/checkins/{first.json()['id']}", headers=H(raw))
    assert undone.status_code == 200 and undone.json()["deleted"] is True
    assert await session.scalar(select(func.count()).select_from(CheckIns).where(CheckIns.registration_id == reg["id"])) == 0
    assert (await client.post(f"{V1}/checkins", json={"ticket_code": "MOA-NOPE-NOPE"}, headers=H(raw))).status_code == 404
    r = await client.post(f"{V1}/checkins", json={"ticket_code": "x", "registration_id": reg["id"]}, headers=H(raw))
    assert r.status_code == 422


# ─── Certificates ────────────────────────────────────────────────────────


async def test_certificates_are_issued_by_the_existing_service_and_verified(client, session, world):
    raw = await key_for(session, world.ids.club)
    a = (await client.post(f"{V1}/events/{world.ids.upcoming}/registrations", json=person(1), headers=H(raw))).json()
    b = (await client.post(f"{V1}/events/{world.ids.upcoming}/registrations", json=person(2), headers=H(raw))).json()
    await client.post(f"{V1}/checkins", json={"registration_id": a["id"]}, headers=H(raw))
    job = await client.post(f"{V1}/events/{world.ids.upcoming}/certificates", json={"all_checked_in": True, "send": False}, headers=H(raw))
    assert job.status_code == 202, job.text
    assert job.json()["status"] == "completed" and job.json()["result"]["issued"] == 1
    certs = (await client.get(f"{V1}/events/{world.ids.upcoming}/certificates", headers=H(raw))).json()["data"]
    assert [c["registration_id"] for c in certs] == [a["id"]] and certs[0]["name"] == "Guest 1"
    again = (await client.post(f"{V1}/events/{world.ids.upcoming}/certificates", json={"registration_ids": [a["id"], b["id"]], "send": False}, headers=H(raw))).json()
    assert again["result"]["issued"] == 1 and again["result"]["already_issued"] == 1
    number = certs[0]["number"]
    verified = (await client.get(f"{V1}/certificates/{number}", headers=H(raw))).json()
    assert verified["valid"] is True and verified["registration_id"] == a["id"] and verified["pdf_url"]
    assert (await client.get(f"{V1}/certificate-jobs/{job.json()['id']}", headers=H(raw))).json()["id"] == job.json()["id"]
    assert (await client.post(f"{V1}/events/{world.ids.upcoming}/certificates", json={}, headers=H(raw))).status_code == 422


# ─── The club ────────────────────────────────────────────────────────────


async def test_the_club_reads_and_edits_its_profile(client, session, world):
    raw = await key_for(session, world.ids.club)
    got = await client.get(f"{V1}/club", headers=H(raw))
    assert got.status_code == 200 and got.json()["name"] == "Robotics Club" and got.json()["status"] == "active"
    r = await client.patch(f"{V1}/club", json={"tagline": "Robots, every Friday", "links": {"website": "https://robotics.example.edu"}},
                           headers=H(raw, **{"If-Match": got.headers["ETag"]}))
    assert r.status_code == 200, r.text
    assert r.json()["tagline"] == "Robots, every Friday" and r.json()["links"]["website"] == "https://robotics.example.edu"
    assert r.json()["name"] == "Robotics Club"
    r = await client.patch(f"{V1}/club", json={"owner": "me"}, headers=H(raw))
    assert r.status_code == 422


# ─── Test mode ───────────────────────────────────────────────────────────


async def test_test_keys_work_on_the_sandbox_and_never_meet_live_objects(client, session, world):
    live = await key_for(session, world.ids.club)
    test = await key_for(session, world.ids.club, livemode=False, name="Sandbox")
    events_before = await session.scalar(select(func.count()).select_from(Events))
    regs_before = await session.scalar(select(func.count()).select_from(EventRegistrationsLink).execution_options(include_deleted=True))
    paid = await client.post(f"{V1}/events", json=event_body(price={"amount": 9900}), headers=H(test))
    assert paid.status_code == 201, paid.text
    assert paid.json()["external_payments_allowed"] is False and paid.json()["price"] == {"amount": 9900, "currency": "INR"}
    # The money rules hold: no payment, and no payment from outside on a test event either (only a club turns that on).
    r = await client.post(f"{V1}/events/{paid.json()['id']}/registrations", json=person(1), headers=H(test))
    assert r.status_code == 409 and r.json()["error"]["code"] == "PAYMENT_REQUIRED" and r.json()["error"]["details"]["checkout_url"] is None
    pay = {"status": "paid_externally", "amount": 9900, "currency": "INR"}
    r = await client.post(f"{V1}/events/{paid.json()['id']}/registrations", json={**person(1), "payment": pay}, headers=H(test))
    assert r.status_code == 409 and r.json()["error"]["code"] == "EXTERNAL_PAYMENTS_OFF"
    made = await client.post(f"{V1}/events", json=event_body(name="Free test night"), headers=H(test))
    event = made.json()
    assert event["id"].startswith("test_evt_") and event["livemode"] is False and event["status"] == "draft"
    assert (await client.get(f"{V1}/events/{event['id']}", headers=H(live))).status_code == 404
    assert (await client.get(f"{V1}/events/{world.ids.upcoming}", headers=H(test))).status_code == 404
    assert event["id"] not in {e["id"] for e in (await client.get(f"{V1}/events", headers=H(live))).json()["data"]}
    assert [e["id"] for e in (await client.get(f"{V1}/events", headers=H(test))).json()["data"]] == [paid.json()["id"], event["id"]]
    r = await client.post(f"{V1}/checkins", json={"ticket_code": "TEST-AAAA-BBBB"}, headers=H(test))
    assert r.status_code == 404
    # The door refuses a draft.
    reg = await client.post(f"{V1}/events/{event['id']}/registrations", json=person(1), headers=H(test))
    assert reg.status_code == 201 and reg.json()["id"].startswith("test_reg_") and reg.json()["livemode"] is False
    r = await client.post(f"{V1}/checkins", json={"registration_id": reg.json()["id"]}, headers=H(test))
    assert r.status_code == 409 and r.json()["error"]["code"] == "EVENT_NOT_OPEN"
    assert (await client.post(f"{V1}/events/{event['id']}/publish", headers=H(test))).json()["status"] == "published"
    first = await client.post(f"{V1}/checkins", json={"ticket_code": reg.json()["ticket"]["code"]}, headers=H(test))
    again = await client.post(f"{V1}/checkins", json={"registration_id": reg.json()["id"]}, headers=H(test))
    assert first.status_code == 201 and again.status_code == 200 and again.json()["duplicate"] is True
    r = await client.post(f"{V1}/events/{event['id']}/registrations", json=person(1), headers=H(test))
    assert r.json()["error"]["code"] == "ALREADY_REGISTERED"
    job = (await client.post(f"{V1}/events/{event['id']}/certificates", json={"all_checked_in": True}, headers=H(test))).json()
    assert job["livemode"] is False and job["result"]["issued"] == 1
    certs = (await client.get(f"{V1}/events/{event['id']}/certificates", headers=H(test))).json()["data"]
    assert (await client.get(f"{V1}/certificates/{certs[0]['number']}", headers=H(test))).json()["valid"] is True
    assert (await client.get(f"{V1}/certificates/{certs[0]['number']}", headers=H(live))).status_code == 404
    # Nothing reached the real tables.
    assert await session.scalar(select(func.count()).select_from(Events)) == events_before
    assert await session.scalar(select(func.count()).select_from(EventRegistrationsLink).execution_options(include_deleted=True)) == regs_before
    assert await session.scalar(select(func.count()).select_from(PartnerSandboxObjects)) >= 4


async def test_the_sandbox_holds_capacity_and_the_registration_window(client, session, world):
    test = await key_for(session, world.ids.club, livemode=False)
    event_id = (await client.post(f"{V1}/events", json=event_body(capacity=1), headers=H(test))).json()["id"]
    await client.post(f"{V1}/events/{event_id}/publish", headers=H(test))
    assert (await client.post(f"{V1}/events/{event_id}/registrations", json=person(1), headers=H(test))).status_code == 201
    r = await client.post(f"{V1}/events/{event_id}/registrations", json=person(2), headers=H(test))
    assert r.json()["error"]["code"] == "EVENT_FULL"
    r = await client.patch(f"{V1}/events/{event_id}", json={"registration": {"closes_at": iso(NOW - timedelta(minutes=1))}}, headers=H(test))
    assert r.status_code == 200
    r = await client.post(f"{V1}/events/{event_id}/registrations", json=person(3), headers=H(test))
    assert r.json()["error"]["code"] == "REGISTRATION_CLOSED"
    cancelled = await client.post(f"{V1}/events/{event_id}/cancel", json={"reason": "Test"}, headers=H(test))
    assert cancelled.json()["status"] == "cancelled"


# ─── Partner apps ────────────────────────────────────────────────────────


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


async def register_app(client, mint_token, admin, *, scopes=None) -> dict:
    body = {"name": "Campus ERP", "website": "https://erp.example.com", "redirect_uris": [REDIRECT],
            "allowed_scopes": scopes or ["events:read", "events:write", "registrations:read", "attendees:read", "clubs:create"]}
    r = await client.post(f"{API}/admin/integrations/partner-apps", json=body, headers=auth_header(await mint_token(admin)))
    assert r.status_code == 201, r.text
    assert r.json()["client_secret"].startswith("moa_pcs_")
    return r.json()


def basic(app: dict) -> dict:
    raw = f"{app['client_id']}:{app['client_secret']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def connect(client, mint_token, world, app, *, scopes=("events:read", "registrations:read"), mode="live", ack=False):
    verifier, challenge = pkce()
    params = {"client_id": app["client_id"], "redirect_uri": REDIRECT, "response_type": "code", "scope": " ".join(scopes),
              "state": "s-1", "code_challenge": challenge, "code_challenge_method": "S256", "mode": mode}
    r = await client.get(f"/partner/oauth/authorize?{urlencode(params)}")
    assert r.status_code == 303 and r.headers["location"].startswith(settings.MCP_DASHBOARD_URL.rstrip("/") + "/integrations/connect?")
    h = auth_header(await mint_token(world.owner))
    page = await client.get(f"{API}/clubs/integrations/connect", params=params, headers=h)
    assert page.status_code == 200, page.text
    assert page.json()["app"]["name"] == "Campus ERP" and [s["key"] for s in page.json()["scopes"]] == list(scopes)
    decided = await client.post(f"{API}/clubs/integrations/connect", json={**params, "decision": "allow", "scopes": list(scopes), "attendees_disclosure_ack": ack}, headers=h)
    assert decided.status_code == 200, decided.text
    back = urlsplit(decided.json()["redirect_to"])
    query = parse_qs(back.query)
    assert f"{back.scheme}://{back.netloc}{back.path}" == REDIRECT and query["state"] == ["s-1"]
    return query["code"][0], verifier


async def test_a_partner_connects_with_pkce_refreshes_and_reuse_ends_it(client, session, world, mint_token):
    app = await register_app(client, mint_token, world.admin)
    code, verifier = await connect(client, mint_token, world, app)
    r = await client.post("/partner/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": "x" * 43}, headers=basic(app))
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"
    code, verifier = await connect(client, mint_token, world, app)
    r = await client.post("/partner/oauth/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}, headers=basic(app))
    assert r.status_code == 200, r.text
    tokens = r.json()
    assert tokens["access_token"].startswith("moa_pat_") and tokens["refresh_token"].startswith("moa_prt_") and tokens["club_id"] == str(world.ids.club)
    me = (await client.get(f"{V1}/me", headers=H(tokens["access_token"]))).json()
    assert me["type"] == "partner_app" and me["partner"]["name"] == "Campus ERP" and me["club"]["id"] == str(world.ids.club)
    assert me["scopes"] == ["events:read", "registrations:read"]
    assert (await client.get(f"{V1}/events", headers=H(tokens["access_token"]))).status_code == 200
    assert (await client.post(f"{V1}/events", json=event_body(), headers=H(tokens["access_token"]))).status_code == 403
    # The code is good once; the wrong secret is no client.
    r = await client.post("/partner/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}, headers=basic(app))
    assert r.json()["error"] == "invalid_grant"
    code2, verifier2 = await connect(client, mint_token, world, app)
    r = await client.post("/partner/oauth/token", data={"grant_type": "authorization_code", "code": code2, "code_verifier": verifier2,
                                                        "client_id": app["client_id"], "client_secret": "wrong"})
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"
    r = await client.post("/partner/oauth/token", data={"grant_type": "authorization_code", "code": code2, "code_verifier": verifier2}, headers=basic(app))
    tokens = r.json()
    # One live connection per app and club: the newest.
    assert await session.scalar(select(func.count()).select_from(PartnerConnections).where(PartnerConnections.revoked_at.is_(None))) == 1
    fresh = (await client.post("/partner/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}, headers=basic(app))).json()
    assert fresh["access_token"] != tokens["access_token"]
    assert (await client.get(f"{V1}/me", headers=H(fresh["access_token"]))).status_code == 200
    reused = await client.post("/partner/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}, headers=basic(app))
    assert reused.status_code == 400 and reused.json()["error"] == "invalid_grant"
    assert (await client.get(f"{V1}/me", headers=H(fresh["access_token"]))).status_code == 401
    assert (await session.scalar(select(PartnerConnections).order_by(PartnerConnections.created_at.desc()).limit(1))).revoked_reason == "refresh_reused"


async def test_revoking_disconnecting_and_suspending_end_access(client, session, world, mint_token):
    app = await register_app(client, mint_token, world.admin)
    code, verifier = await connect(client, mint_token, world, app)
    tokens = (await client.post("/partner/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}, headers=basic(app))).json()
    admin_h = auth_header(await mint_token(world.admin))
    r = await client.post(f"{API}/admin/integrations/partner-apps/{app['id']}/suspend", json={"reason": "Abuse report"}, headers=admin_h)
    assert r.json()["status"] == "suspended"
    r = await client.get(f"{V1}/me", headers=H(tokens["access_token"]))
    assert r.status_code == 403 and r.json()["error"]["code"] == "APP_SUSPENDED"
    await client.post(f"{API}/admin/integrations/partner-apps/{app['id']}/resume", headers=admin_h)
    assert (await client.get(f"{V1}/me", headers=H(tokens["access_token"]))).status_code == 200
    h = auth_header(await mint_token(world.owner))
    listed = (await client.get(f"{API}/clubs/integrations/connected-apps", headers=h)).json()["apps"]
    assert len(listed) == 1 and listed[0]["app"]["name"] == "Campus ERP"
    assert (await client.delete(f"{API}/clubs/integrations/connected-apps/{listed[0]['id']}", headers=h)).status_code == 200
    assert (await client.get(f"{V1}/me", headers=H(tokens["access_token"]))).status_code == 401
    code, verifier = await connect(client, mint_token, world, app)
    tokens = (await client.post("/partner/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": verifier}, headers=basic(app))).json()
    assert (await client.post("/partner/oauth/revoke", data={"token": tokens["refresh_token"]}, headers=basic(app))).status_code == 200
    assert (await client.get(f"{V1}/me", headers=H(tokens["access_token"]))).status_code == 401


async def test_attendees_read_needs_the_disclosure_tick_at_consent(client, session, world, mint_token):
    app = await register_app(client, mint_token, world.admin)
    verifier, challenge = pkce()
    params = {"client_id": app["client_id"], "redirect_uri": REDIRECT, "response_type": "code", "scope": "registrations:read attendees:read",
              "state": "s", "code_challenge": challenge, "code_challenge_method": "S256"}
    h = auth_header(await mint_token(world.owner))
    page = (await client.get(f"{API}/clubs/integrations/connect", params=params, headers=h)).json()
    assert "Campus ERP" in page["disclosure"]
    r = await client.post(f"{API}/clubs/integrations/connect", json={**params, "decision": "allow", "scopes": ["registrations:read", "attendees:read"]}, headers=h)
    assert r.status_code == 422 and r.json()["error_code"] == "DISCLOSURE_REQUIRED"
    denied = await client.post(f"{API}/clubs/integrations/connect", json={**params, "decision": "deny"}, headers=h)
    assert "error=access_denied" in denied.json()["redirect_to"]
    # An unregistered redirect never gets a code.
    bad = await client.get(f"{API}/clubs/integrations/connect", params={**params, "redirect_uri": "https://evil.example.com/cb"}, headers=h)
    assert bad.status_code == 400 and bad.json()["errors"]["redirect_to"] is None


async def test_a_partner_creates_a_club_waiting_for_approval(client, session, world, mint_token, monkeypatch):
    from app.api.clubs.models import Clubs
    from app.api.partner import live

    invited = []
    monkeypatch.setattr(live, "_send_owner_invite", lambda to, **kw: invited.append((to, kw)))
    app = await register_app(client, mint_token, world.admin)
    r = await client.post("/partner/oauth/token", data={"grant_type": "client_credentials"}, headers=basic(app))
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "clubs:create" and "refresh_token" not in r.json()
    app_token = r.json()["access_token"]
    me = (await client.get(f"{V1}/me", headers=H(app_token))).json()
    assert me["club"] is None and me["scopes"] == ["clubs:create"]
    assert (await client.get(f"{V1}/events", headers=H(app_token))).status_code == 403
    body = {"name": "Drama Society", "owner": {"email": "drama.sec@example.com", "name": "Meera"}, "tagline": "All the world's a stage", "external_id": "c-9"}
    r = await client.post(f"{V1}/clubs", json=body, headers=H(app_token))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "pending_approval" and out["external_id"] == "c-9" and "access_token" not in json.dumps(out)
    as_club = H(app_token, **{"MOA-Club-Id": out["id"]})
    club = await session.get(Clubs, int(out["id"]))
    assert club.is_verified is False and club.tagline == "All the world's a stage"
    alloc_row = await session.get(PartnerAllocations, club.id)
    assert alloc_row.enabled and alloc_row.source == "partner_created"
    from app.core import after_commit

    await after_commit.drain()
    assert invited and invited[0][0] == "drama.sec@example.com" and invited[0][1]["partner"] == "Campus ERP"
    r = await client.get(f"{V1}/me", headers=as_club)
    assert r.status_code == 403 and r.json()["error"]["code"] == "CLUB_NOT_ACTIVE"
    r = await client.get(f"{V1}/me", headers=H(app_token, **{"MOA-Club-Id": str(world.ids.club)}))
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_CONNECTED"
    assert (await client.post(f"{V1}/clubs", json=body, headers=H(app_token))).json()["error"]["code"] == "ALREADY_EXISTS"
    listed = (await client.get(f"{V1}/clubs", headers=H(app_token))).json()["data"]
    assert [c["id"] for c in listed] == [out["id"]]
    # Approved, its connection's token acts for it.
    club.is_verified = True
    await session.commit()
    me = (await client.get(f"{V1}/me", headers=as_club)).json()
    assert me["club"]["name"] == "Drama Society" and "attendees:read" not in me["scopes"] and "clubs:create" not in me["scopes"]
    assert me["scopes"] == ["events:read", "events:write", "registrations:read"]
    made = await client.post(f"{V1}/events", json=event_body(), headers=as_club)
    assert made.status_code == 201 and made.json()["club_id"] == out["id"]
    idem = await session.scalar(select(PartnerIdempotency).where(PartnerIdempotency.path == f"{V1}/clubs"))
    assert idem is None or "moa_pat_" not in (idem.response_body or "")


async def test_an_app_without_clubs_create_cannot_create(client, session, world, mint_token):
    app = await register_app(client, mint_token, world.admin, scopes=["events:read"])
    r = await client.post("/partner/oauth/token", data={"grant_type": "client_credentials", "scope": "clubs:create"}, headers=basic(app))
    assert r.status_code == 400 and r.json()["error"] == "invalid_scope"


# ─── The document ────────────────────────────────────────────────────────


def _refs(node, found: set) -> set:
    if isinstance(node, dict):
        if "$ref" in node:
            found.add(node["$ref"])
        for value in node.values():
            _refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _refs(value, found)
    return found


async def test_the_openapi_document_is_valid_31_for_exactly_this_surface(client, world):
    r = await client.get(f"{V1}/openapi.json")
    assert r.status_code == 200
    doc = r.json()
    assert doc["openapi"] == "3.1.0" and doc["info"]["title"] == "MyOtherApp Integrations API" and doc["info"]["version"]
    assert all(path.startswith("/partner/v1/") for path in doc["paths"])
    for path in ("/partner/v1/me", "/partner/v1/events", "/partner/v1/events/{event_id}/registrations", "/partner/v1/checkins",
                 "/partner/v1/events/{event_id}/certificates", "/partner/v1/clubs", "/partner/v1/club"):
        assert path in doc["paths"], path
    schemas = doc["components"]["schemas"]
    for ref in _refs(doc, set()):
        assert ref.startswith("#/components/schemas/") and ref.rsplit("/", 1)[1] in schemas, ref
    assert {"apiKey", "partnerOAuth"} == set(doc["components"]["securitySchemes"])
    flows = doc["components"]["securitySchemes"]["partnerOAuth"]["flows"]
    assert flows["authorizationCode"]["tokenUrl"].endswith("/partner/oauth/token")
    for path, item in doc["paths"].items():
        for method, op in item.items():
            assert op.get("responses") and op.get("operationId") and "x-scopes" in op, (path, method)
            if method in ("post", "delete"):
                assert any(p["name"] == "Idempotency-Key" for p in op["parameters"])
            if method == "patch":
                assert any(p["name"] == "If-Match" for p in op["parameters"])
            for status, response in op["responses"].items():
                assert "X-Request-Id" in response.get("headers", {}), (path, method, status)
    assert doc["paths"]["/partner/v1/events"]["post"]["x-scopes"] == ["events:write"]
    assert schemas["EventCreate"].get("examples") and schemas["EventOut"].get("examples") and schemas["ErrorBody"].get("examples")
    assert "HTTPValidationError" not in schemas
    assert {"scanned_at", "updated_at", "duplicate", "gate", "via"} <= set(schemas["CheckinOut"]["properties"])
    assert {"registration_id", "revoked_at", "object"} <= set(schemas["CertificateVerification"]["properties"])
    from jsonschema import Draft202012Validator

    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)


async def test_the_kill_switch_hides_the_surface(client, session, world, monkeypatch):
    raw = await key_for(session, world.ids.club)
    monkeypatch.setattr(settings, "PARTNER_API_ENABLED", False)
    assert (await client.get(f"{V1}/me", headers=H(raw))).status_code == 404
    assert (await client.get(f"{V1}/openapi.json")).status_code == 404
    assert (await client.post("/partner/oauth/token", data={"grant_type": "client_credentials"})).status_code == 404


# ─── Origins and webhooks ────────────────────────────────────────────────


async def test_a_change_through_the_api_carries_its_origin_and_it_is_reset(client, session, world, monkeypatch):
    from app.api.events import service as events_service
    from app.core import api_origin

    seen = []
    real = events_service.create_event

    async def watching(*args, **kwargs):
        seen.append(api_origin.current_origin())
        return await real(*args, **kwargs)

    monkeypatch.setattr(events_service, "create_event", watching)
    raw = await key_for(session, world.ids.club)
    client_id = (await client.get(f"{V1}/me", headers=H(raw))).json()["client_id"]
    assert (await client.post(f"{V1}/events", json=event_body(), headers=H(raw))).status_code == 201
    assert seen == [api_origin.Origin(type="api_key", client_id=client_id)]
    assert api_origin.current_origin() is None


async def test_sandbox_changes_are_emitted_as_test_webhooks(client, session, world, monkeypatch):
    import sys
    import types

    calls = []
    fake = types.ModuleType("app.api.webhooks_out")
    fake.emit = lambda club_id, type_, obj, origin: calls.append((club_id, type_, obj, origin))
    monkeypatch.setitem(sys.modules, "app.api.webhooks_out", fake)
    test = await key_for(session, world.ids.club, livemode=False)
    event_id = (await client.post(f"{V1}/events", json=event_body(), headers=H(test))).json()["id"]
    await client.post(f"{V1}/events/{event_id}/publish", headers=H(test))
    reg = (await client.post(f"{V1}/events/{event_id}/registrations", json=person(1), headers=H(test))).json()
    await client.post(f"{V1}/checkins", json={"registration_id": reg["id"]}, headers=H(test))
    assert [c[1] for c in calls] == ["event.created", "event.published", "registration.created", "checkin.created"]
    assert all(c[0] == world.ids.club and c[2]["livemode"] is False and c[3].type == "api_key" for c in calls)


async def test_the_builders_are_plain_functions_with_the_answers_shapes(client, session, world):
    """What webhooks_out's emitters will call: the same shapes as the GETs."""
    from app.api.partner import shapes

    raw = await key_for(session, world.ids.club)
    reg = (await client.post(f"{V1}/events/{world.ids.upcoming}/registrations", json=person(1), headers=H(raw))).json()
    checkin = (await client.post(f"{V1}/checkins", json={"registration_id": reg["id"]}, headers=H(raw))).json()
    row = await session.get(EventRegistrationsLink, reg["id"], populate_existing=True)
    event = await session.get(Events, world.ids.upcoming, populate_existing=True)
    payload = await shapes.registration_payload(session, row, event)
    got = (await client.get(f"{V1}/registrations/{reg['id']}", headers=H(raw))).json()
    assert {k: v for k, v in payload.items() if k not in ("ticket", "updated_at")} == {k: v for k, v in got.items() if k not in ("ticket", "updated_at", "external_id")} | {"external_id": None}
    assert shapes.ticket_object(row, event)["code"] == got["ticket"]["code"] and shapes.payment_object(row, event) == got["payment"]
    assert (await shapes.event_payload(session, event))["id"] == str(world.ids.upcoming)
    ci = await session.scalar(select(CheckIns).where(CheckIns.registration_id == row.id))
    assert shapes.checkin_object(ci)["id"] == checkin["id"]
    log = SimpleNamespace(id="9b0e", status=SimpleNamespace(value="captured"), amount=499, created_at=NOW, updated_at=NOW)
    assert shapes.payment_log_object(log, row) == {
        "id": "9b0e", "object": "payment", "registration_id": reg["id"], "event_id": str(world.ids.upcoming), "status": "captured",
        "amount": 49900, "currency": "INR", "created_at": iso(NOW), "updated_at": iso(NOW), "livemode": True,
    }
