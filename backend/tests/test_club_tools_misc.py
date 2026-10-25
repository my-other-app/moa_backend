"""The smaller club tools: the contact book's filters and who reads it,
renaming a tracking link without losing what it brought, and replacing the
two-factor recovery codes."""
import uuid
from datetime import datetime, timedelta, timezone

import pyotp
import pytest
from sqlalchemy import func, select

from app.api.closeout.models import ArchivedContacts
from app.api.links.models import TrackingClicks
from app.api.security import service as security
from app.core.email import email as email_mod
from app.core.rate_limit import limiter
from tests.club_ops_helpers import acting_as, member_header
from tests.conftest import auth_header


@pytest.fixture
async def world(client, session, make_club, make_event, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: None)

    async def no_lookup(ip):
        return None

    monkeypatch.setattr(security, "lookup_place", no_lookup)
    owner, club = await make_club(name=f"Tools Club {uuid.uuid4().hex[:4]}", email=f"tools{uuid.uuid4().hex[:6]}@example.com")
    now = datetime.now(timezone.utc)
    spring = await make_event(club=club, created_by=owner, name="Spring Meet", event_datetime=now - timedelta(days=60))
    autumn = await make_event(club=club, created_by=owner, name="Autumn Meet", event_datetime=now - timedelta(days=5))
    for event, ended, people in ((spring, now - timedelta(days=60), ("Asha", "Bina")), (autumn, now - timedelta(days=5), ("Chitra",))):
        for name in people:
            session.add(ArchivedContacts(club_id=club.id, event_id=event.id, registration_id=uuid.uuid4(), name=name,
                                         email=f"{name.lower()}@example.com", phone="9000000000", role="attendee",
                                         attended=name != "Bina", event_name=event.name, event_ended_at=ended))
    await session.commit()
    return dict(owner=owner, club=club, spring=spring, autumn=autumn, now=now, h=auth_header(await mint_token(owner)))


# ─── The contact book ───────────────────────────────────────────────────


async def test_the_contact_book_filters_by_event_and_date_and_exports_the_same(client, world):
    h = world["h"]
    everyone = (await client.get("/api/v1/clubs/contacts", headers=h)).json()
    assert everyone["total"] == 3 and {g["event_name"] for g in everyone["groups"]} == {"Spring Meet", "Autumn Meet"}
    recent = (await client.get("/api/v1/clubs/contacts", params={"since": (world["now"] - timedelta(days=30)).date().isoformat()}, headers=h)).json()
    assert [c["name"] for c in recent["items"]] == ["Chitra"]
    older = (await client.get("/api/v1/clubs/contacts", params={"until": (world["now"] - timedelta(days=30)).date().isoformat()}, headers=h)).json()
    assert sorted(c["name"] for c in older["items"]) == ["Asha", "Bina"]
    came = (await client.get("/api/v1/clubs/contacts", params={"event_id": world["spring"].id, "attended": True}, headers=h)).json()
    assert [c["name"] for c in came["items"]] == ["Asha"]
    csv = await client.get("/api/v1/clubs/contacts/export.csv", params={"until": (world["now"] - timedelta(days=30)).date().isoformat()}, headers=h)
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    rows = csv.text.strip().splitlines()
    assert rows[0].startswith("Name,Email") and len(rows) == 3 and "Chitra" not in csv.text


async def test_editors_read_the_contact_book_door_staff_do_not(client, session, world):
    editor = await member_header(client, session, world["club"], "editor")
    door = await member_header(client, session, world["club"], "door")
    assert (await client.get("/api/v1/clubs/contacts", headers=editor)).status_code == 200
    assert (await client.get("/api/v1/clubs/contacts", headers=door)).status_code == 403
    assert (await client.get("/api/v1/clubs/contacts/export.csv", headers=door)).status_code == 403
    me = (await client.get("/api/v1/clubs/access/me", headers=door)).json()
    assert me["can"]["contacts"] is False


# ─── Renaming a tracking link ───────────────────────────────────────────


async def test_renaming_a_link_keeps_its_code_and_what_it_brought(client, session, world):
    h = world["h"]
    made = await client.post("/api/v1/clubs/links", json={"name": "Poster", "utm_source": "print", "utm_medium": "poster"}, headers=h)
    assert made.status_code == 201, made.text
    link = made.json()
    assert (await client.get(f"/l/{link['code']}", headers={"user-agent": "phone", "x-forwarded-for": "10.1.1.1"})).status_code == 302
    renamed = await client.patch(f"/api/v1/clubs/links/{link['id']}", json={"name": "Poster at the canteen", "utm_source": "Canteen"}, headers=h)
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["name"] == "Poster at the canteen" and renamed.json()["utm_source"] == "canteen"
    assert renamed.json()["code"] == link["code"] and renamed.json()["id"] == link["id"]
    clicks = await session.scalar(select(func.count()).select_from(TrackingClicks).where(TrackingClicks.link_id == uuid.UUID(link["id"])))
    assert clicks == 1
    door = await member_header(client, session, world["club"], "door")
    assert (await client.patch(f"/api/v1/clubs/links/{link['id']}", json={"name": "Nope"}, headers=door)).status_code == 403


# ─── Two-factor recovery codes ──────────────────────────────────────────


async def _with_two_factor(session, owner) -> str:
    secret = pyotp.random_base32()
    owner.totp_secret, owner.totp_enabled = secret, True
    await session.commit()
    return secret


async def test_a_new_set_of_recovery_codes_replaces_the_old_one(client, session, world):
    secret = await _with_two_factor(session, world["owner"])
    h = world["h"]
    wrong = await client.post("/api/v1/auth/2fa/recovery-codes", json={"code": "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"}, headers=h)
    assert wrong.status_code == 400
    first = await client.post("/api/v1/auth/2fa/recovery-codes", json={"code": pyotp.TOTP(secret).now()}, headers=h)
    assert first.status_code == 200, first.text
    old_codes = first.json()["recovery_codes"]
    assert len(old_codes) == 8
    status = (await client.get("/api/v1/auth/2fa", headers=h)).json()
    assert status == {"enabled": True, "recovery_codes_remaining": 8}

    await session.refresh(world["owner"])
    world["owner"].totp_last_step = None  # the next code in the same 30 seconds
    await session.commit()
    second = await client.post("/api/v1/auth/2fa/recovery-codes", json={"code": pyotp.TOTP(secret).now()}, headers=h)
    assert second.status_code == 200, second.text
    new_codes = second.json()["recovery_codes"]
    assert set(new_codes).isdisjoint(old_codes)
    # An old one no longer signs in; a new one does.
    creds = {"username": world["owner"].email, "password": "Password123!", "scope": "club"}
    assert (await client.post("/api/v1/auth/token", data={**creds, "totp_code": old_codes[0]})).status_code == 401
    assert (await client.post("/api/v1/auth/token", data={**creds, "totp_code": new_codes[0]})).status_code == 200


async def test_recovery_codes_are_not_made_while_acting_as_the_club_nor_by_team_members(client, session, world, make_admin):
    secret = await _with_two_factor(session, world["owner"])
    admin = await make_admin(email=f"support{uuid.uuid4().hex[:6]}@example.com")
    await session.commit()
    as_club = await acting_as(session, world["owner"], admin)
    r = await client.post("/api/v1/auth/2fa/recovery-codes", json={"code": pyotp.TOTP(secret).now()}, headers=as_club)
    assert r.status_code == 403 and r.json()["error_code"] == "NOT_WHILE_IMPERSONATING"
    member = await member_header(client, session, world["club"], "admin")
    assert (await client.post("/api/v1/auth/2fa/recovery-codes", json={"code": pyotp.TOTP(secret).now()}, headers=member)).status_code == 403
