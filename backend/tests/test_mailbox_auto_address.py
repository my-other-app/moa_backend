"""Every club's address, made from its name, and an admin changing one
(feature-register/mail-and-slugs-contract.md, Part M1 and M2).

- `handles.from_name`: the letters and digits of the name, lower case, cut
  to 30; `club<id>` when fewer than three are left; a reserved one gets a
  number, as a taken one does.
- `service.ensure_address`: made when a club is created, whatever its plan
  and whatever APP_CLUB_MAILBOX says; idempotent; `automatic`; never fails
  what called it.
- A club whose plan lacks the mailbox sees its address **reserved**: no mail
  kept, nothing told, nothing lapsed, nothing released.
- The backfill (dry run, then applied; oldest club first; deleted clubs
  skipped), and the admin's address change (no monthly limit, the old one
  forwarding 90 days, audited).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from app.api.audit.models import AdminAuditLogs
from app.api.club_alerts.models import ClubAlerts
from app.api.club_mailbox import handles, service, sweep
from app.api.club_mailbox.models import ClubMailboxAddresses, ClubMailboxes
from app.api.clubs.models import Clubs
from app.api.subscriptions import gates
from app.api.subscriptions import service as subscriptions
from app.api.subscriptions.models import BillingCycle
from app.api.users.models import AdminRoles
from app.config import settings
from tests.conftest import auth_header
from tests.test_club_mailbox import LONG_AGO, aws, owner_mails, plans  # noqa: F401 - fixtures
from tests.test_club_mailbox_cloudflare import KEY, post

API = "/api/v1"
BASE = f"{API}/clubs/mailbox"
ADMIN = f"{API}/admin/club-mailbox"
DOMAIN = "myotherapp.in"
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _mailbox_on(monkeypatch):
    monkeypatch.setattr(settings, "CLUB_MAILBOX", True)
    monkeypatch.setattr(settings, "MAILBOX_DOMAIN", DOMAIN)
    monkeypatch.setattr(settings, "MAILBOX_HOOK_SECRET", KEY)
    monkeypatch.setattr(settings, "MAILBOX_INBOUND_BUCKET", "")
    monkeypatch.setattr(settings, "MAILBOX_SNS_TOPIC_ARN", "")
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", LONG_AGO)
    monkeypatch.setattr(settings, "LIVE_SSE", False)
    monkeypatch.setattr(settings, "SUPPORT_INBOUND_ADDRESSES", "")
    monkeypatch.setattr(settings, "SALES_INBOUND_ADDRESSES", "")
    gates.reset()
    yield
    gates.reset()


async def address_rows(session, club_id):
    return (await session.scalars(
        select(ClubMailboxAddresses).where(ClubMailboxAddresses.club_id == club_id).order_by(ClubMailboxAddresses.id)
        .execution_options(populate_existing=True)
    )).all()


async def current(session, club_id):
    rows = [a for a in await address_rows(session, club_id) if a.retired_at is None]
    return rows[0] if rows else None


@pytest.fixture
async def admins(make_admin, mint_token):
    viewer = await make_admin(email="viewer-mail@example.com", role=AdminRoles.viewer)
    support = await make_admin(email="support-mail@example.com", role=AdminRoles.support)
    owner = await make_admin(email="owner-mail@example.com", role=AdminRoles.owner)
    return SimpleNamespace(
        viewer=auth_header(await mint_token(viewer)), support=auth_header(await mint_token(support)),
        owner=auth_header(await mint_token(owner)), owner_user=owner, support_user=support,
    )


@pytest.fixture
async def clubs(session, make_club, mint_token, plans):  # noqa: F811
    async def make(name, email, plan="growth"):
        owner, club = await make_club(name=name, email=email)
        await subscriptions.assign_plan(session, club_id=club.id, plan=plans[plan], cycle=BillingCycle.monthly)
        await session.commit()
        return SimpleNamespace(owner=owner, club=club, h=auth_header(await mint_token(owner)))

    return make


# ─── Made when the club is ───────────────────────────────────────────────


async def test_a_club_created_gets_its_address_whatever_the_plan_or_the_switch(client, session, admins, monkeypatch):
    monkeypatch.setattr(settings, "CLUB_MAILBOX", False)
    r = await client.post(f"{API}/clubs/admin/create", json={"name": "GDG Cloud Trivandrum", "email": "gdg@example.com"},
                          headers=admins.owner)
    assert r.status_code == 200, r.text
    club_id = r.json()["id"]
    row = await current(session, club_id)
    assert row.handle == "gdgcloudtrivandrum" and row.automatic is True
    mailbox = await session.scalar(select(ClubMailboxes).where(ClubMailboxes.club_id == club_id))
    assert mailbox.handle_changed_at is None and mailbox.created_by is None
    # No audit line for it: the club's creation is what happened.
    assert await session.scalar(select(func.count()).select_from(AdminAuditLogs).where(AdminAuditLogs.action.like("club_mailbox.%"))) == 0
    # A second club of almost the same name gets the next number.
    r = await client.post(f"{API}/clubs/admin/create", json={"name": "GDG Cloud-Trivandrum", "email": "gdg2@example.com"},
                          headers=admins.owner)
    assert r.status_code == 200, r.text
    assert (await current(session, r.json()["id"])).handle == "gdgcloudtrivandrum2"


async def test_ensure_address_is_idempotent_and_leaves_a_picked_one_alone(session, clubs, client):
    a = await clubs("Chess Circle", "chess@example.com", plan="pro")
    first = await service.ensure_address(session, a.club)
    await session.commit()
    again = await service.ensure_address(session, a.club)
    assert again.id == first.id and len(await address_rows(session, a.club.id)) == 1
    # A club that picked its own keeps it.
    b = await clubs("Robotics Guild", "robotics@example.com", plan="pro")
    r = await client.put(f"{BASE}/address", json={"handle": "robots"}, headers=b.h)
    assert r.status_code == 200, r.text
    assert (await service.ensure_address(session, b.club)).handle == "robots"


async def test_taken_and_still_forwarding_handles_are_skipped(session, clubs):
    a = await clubs("Yukthi", "yukthi@example.com", plan="pro")
    b = await clubs("Yukthi Club", "yukthi2@example.com")
    c = await clubs("Yukthi!", "yukthi3@example.com")
    await service.ensure_address(session, a.club)
    # a's address changes; yukthi forwards to it for 90 days and is nobody else's meanwhile.
    await session.commit()
    mailbox = await service.mailbox_of(session, a.club.id)
    row = await service.current_address(session, a.club.id)
    await service._move_to(session, a.club.id, mailbox, row, "yukthi.fest", NOW)
    await session.commit()
    assert (await service.ensure_address(session, b.club)).handle == "yukthiclub"
    assert (await service.ensure_address(session, c.club)).handle == "yukthi2"
    # Once its forwarding is over (and before the sweep lets it go), the name is free again.
    await session.execute(update(ClubMailboxAddresses).where(ClubMailboxAddresses.handle == "yukthi")
                          .values(forward_until=NOW - timedelta(minutes=1)))
    d = await clubs("Yukthi.", "yukthi4@example.com")
    assert (await service.ensure_address(session, d.club)).handle == "yukthi"


async def test_a_failure_is_logged_and_never_fails_the_caller(session, clubs, monkeypatch, caplog):
    a = await clubs("Chess Circle", "chess@example.com")

    async def broken(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(handles, "holder", broken)
    assert await service.ensure_address(session, a.club) is None
    assert "could not be made" in caplog.text
    # The session is still usable.
    assert await session.scalar(select(func.count()).select_from(Clubs)) >= 1


async def test_a_race_for_the_name_takes_the_next_number(session, clubs, monkeypatch):
    a = await clubs("Chess Circle", "chess@example.com")
    b = await clubs("Chess-Circle", "chess2@example.com")
    real = handles.holder

    async def blind(session, handle, now=None):
        # The look misses the other club's row, as a concurrent insert would.
        return None if handle == "chesscircle" else await real(session, handle, now)

    await service.ensure_address(session, a.club)
    monkeypatch.setattr(handles, "holder", blind)
    assert (await service.ensure_address(session, b.club)).handle == "chesscircle2"
    # The first club's address was never touched.
    assert (await current(session, a.club.id)).handle == "chesscircle"


# ─── Reserved: the plan lacks the mailbox ────────────────────────────────


async def test_a_club_without_the_plan_sees_its_address_reserved(client, session, clubs, aws, owner_mails):  # noqa: F811
    a = await clubs("Chess Circle", "chess@example.com")
    await service.ensure_address(session, a.club)
    await session.commit()
    page = (await client.get(BASE, headers=a.h)).json()
    assert page["set_up"] is True and page["address"] == f"chesscircle@{DOMAIN}" and page["automatic"] is True
    assert page["plan_allows"] is False and page["state"] == "reserved" and page["upgrade"]["required_plan"] == "pro"
    assert page["lapsed_at"] is None and page["readable_until"] is None
    # Mail to it is refused, as for a closed mailbox; nothing is kept.
    from tests.test_club_mailbox import mime

    r = await post(client, mime(to=f"chesscircle@{DOMAIN}"), to=f"chesscircle@{DOMAIN}")
    assert r.json()["action"] == "reject" and r.json()["text"] == "This mailbox is not receiving mail"
    # The sweep tells no one, lapses nothing, lets nothing go.
    done = await sweep.sweep(session)
    assert done["lapses"] == {} and done["released"] == 0
    mailbox = await service.mailbox_of(session, a.club.id)
    assert mailbox.lapsed_at is None and owner_mails == []
    assert await session.scalar(select(func.count()).select_from(ClubAlerts).where(ClubAlerts.club_id == a.club.id)) == 0
    assert (await current(session, a.club.id)).handle == "chesscircle"
    # The club can't change it on its plan.
    r = await client.put(f"{BASE}/address", json={"handle": "chessclub"}, headers=a.h)
    assert r.status_code == 403 and r.json()["error_code"] == "PLAN_FEATURE_LOCKED"


async def test_upgrading_opens_it_with_no_step_and_the_first_change_is_free(client, session, clubs, plans, aws):  # noqa: F811
    a = await clubs("Chess Circle", "chess@example.com")
    await service.ensure_address(session, a.club)
    await subscriptions.assign_plan(session, club_id=a.club.id, plan=plans["pro"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    page = (await client.get(BASE, headers=a.h)).json()
    assert page["state"] == "open" and page["plan_allows"] is True and page["address"] == f"chesscircle@{DOMAIN}"
    from tests.test_club_mailbox import mime

    r = await post(client, mime(to=f"chesscircle@{DOMAIN}"), to=f"chesscircle@{DOMAIN}")
    assert r.json()["action"] == "accept" and r.json()["kept"] == 1
    # The automatic address may change at once; after that, once a month.
    r = await client.put(f"{BASE}/address", json={"handle": "chessclub"}, headers=a.h)
    assert r.status_code == 200 and r.json()["automatic"] is False, r.text
    assert [p["address"] for p in r.json()["previous"]] == [f"chesscircle@{DOMAIN}"]
    r = await client.put(f"{BASE}/address", json={"handle": "chess.club"}, headers=a.h)
    assert r.status_code == 429 and r.json()["error_code"] == "HANDLE_CHANGED_RECENTLY"


async def test_a_mailbox_that_holds_mail_still_lapses(client, session, clubs, plans, aws, owner_mails):  # noqa: F811
    a = await clubs("Chess Circle", "chess@example.com", plan="pro")
    await service.ensure_address(session, a.club)
    await session.commit()
    from tests.test_club_mailbox import mime

    assert (await post(client, mime(to=f"chesscircle@{DOMAIN}"), to=f"chesscircle@{DOMAIN}")).json()["kept"] == 1
    await subscriptions.assign_plan(session, club_id=a.club.id, plan=plans["growth"], cycle=BillingCycle.monthly)
    await session.commit()
    gates.reset()
    assert (await sweep.sweep(session))["lapses"] == {"lapsed": 1}
    page = (await client.get(BASE, headers=a.h)).json()
    assert page["state"] == "lapsed" and page["readable_until"] is not None


# ─── The backfill ────────────────────────────────────────────────────────


async def test_the_backfill_dry_run_then_applied_oldest_club_first(client, session, clubs, admins):
    old = await clubs("Robotics Guild", "r1@example.com")
    new = await clubs("Robotics-Guild", "r2@example.com")
    picked = await clubs("Chess Circle", "chess@example.com", plan="pro")
    gone = await clubs("Gone Club", "gone@example.com")
    await session.execute(update(Clubs).where(Clubs.id == new.club.id).values(created_at=NOW - timedelta(days=10)))
    await session.execute(update(Clubs).where(Clubs.id == old.club.id).values(created_at=NOW - timedelta(days=400)))
    await session.execute(update(Clubs).where(Clubs.id == gone.club.id).values(is_deleted=True, deleted_at=NOW))
    await session.commit()
    r = await client.put(f"{BASE}/address", json={"handle": "chess"}, headers=picked.h)
    assert r.status_code == 200, r.text

    dry = await client.post(f"{ADMIN}/backfill", headers=admins.owner)
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["dry_run"] is True and body["created"] == 2 and body["skipped"] == 1 and body["failed"] == 0
    assert body["sample"][:2] == [{"club_id": old.club.id, "handle": "roboticsguild"}, {"club_id": new.club.id, "handle": "roboticsguild2"}]
    assert await current(session, old.club.id) is None
    assert await session.scalar(select(func.count()).select_from(AdminAuditLogs).where(AdminAuditLogs.action == "club_mailbox.backfill")) == 0

    applied = (await client.post(f"{ADMIN}/backfill", json={"dry_run": False}, headers=admins.owner)).json()
    assert applied["dry_run"] is False and applied["created"] == 2 and applied["skipped"] == 1
    assert (await current(session, old.club.id)).handle == "roboticsguild"
    assert (await current(session, new.club.id)).handle == "roboticsguild2"
    assert (await current(session, new.club.id)).automatic is True
    assert (await current(session, picked.club.id)).handle == "chess"
    assert await current(session, gone.club.id) is None
    log = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_mailbox.backfill"))
    assert log.details["created"] == 2 and log.details["skipped"] == 1
    # Again: nothing new.
    again = (await client.post(f"{ADMIN}/backfill", json={"dry_run": False}, headers=admins.owner)).json()
    assert again["created"] == 0 and again["skipped"] == 3


async def test_only_an_owner_admin_runs_the_backfill(client, admins):
    for who in (admins.viewer, admins.support):
        r = await client.post(f"{ADMIN}/backfill", json={"dry_run": True}, headers=who)
        assert r.status_code == 403 and r.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"


# ─── An admin changes a club's address ───────────────────────────────────


async def test_an_admin_changes_a_clubs_address_without_the_monthly_limit(client, session, clubs, admins, aws):  # noqa: F811
    a = await clubs("GDG Cloud Trivandrum", "gdg@example.com", plan="pro")
    await service.ensure_address(session, a.club)
    await session.commit()
    r = await client.put(f"{ADMIN}/{a.club.id}/address", json={"handle": "GDGCloudTVM"}, headers=admins.support)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["address"] == f"gdgcloudtvm@{DOMAIN}" and out["handle"] == "gdgcloudtvm" and out["automatic"] is False
    assert [p["address"] for p in out["previous"]] == [f"gdgcloudtrivandrum@{DOMAIN}"] and out["previous"][0]["forward_until"]
    log = await session.scalar(select(AdminAuditLogs).where(AdminAuditLogs.action == "club_mailbox.admin_address"))
    assert log.details == {"handle": "gdgcloudtvm", "previous": "gdgcloudtrivandrum"}
    assert "gdgcloudtrivandrum@" in log.summary and "gdgcloudtvm@" in log.summary
    # The old address still delivers into the mailbox.
    from tests.test_club_mailbox import mime

    r = await post(client, mime(to=f"gdgcloudtrivandrum@{DOMAIN}"), to=f"gdgcloudtrivandrum@{DOMAIN}")
    assert r.json()["action"] == "accept" and r.json()["kept"] == 1
    # No monthly limit for the admin; the club's own change waits its month.
    r = await client.put(f"{ADMIN}/{a.club.id}/address", json={"handle": "gdgtvm"}, headers=admins.owner)
    assert r.status_code == 200 and r.json()["handle"] == "gdgtvm"
    r = await client.put(f"{BASE}/address", json={"handle": "gdg.tvm"}, headers=a.h)
    assert r.status_code == 429
    # The same address again changes nothing.
    r = await client.put(f"{ADMIN}/{a.club.id}/address", json={"handle": "gdgtvm"}, headers=admins.owner)
    assert r.status_code == 200 and len(r.json()["previous"]) == 2


async def test_the_admins_change_is_refused_in_the_clubs_words(client, session, clubs, admins):
    a = await clubs("Chess Circle", "chess@example.com")
    b = await clubs("Robotics Guild", "robotics@example.com")
    await service.ensure_address(session, b.club)
    await session.commit()
    for handle, status, code in [("ab", 422, "HANDLE_INVALID"), ("support", 422, "HANDLE_RESERVED"),
                                 ("developers", 422, "HANDLE_RESERVED"), ("roboticsguild", 409, "HANDLE_TAKEN")]:
        r = await client.put(f"{ADMIN}/{a.club.id}/address", json={"handle": handle}, headers=admins.owner)
        assert r.status_code == status and r.json()["error_code"] == code, (handle, r.text)
    r = await client.put(f"{ADMIN}/999999/address", json={"handle": "nobodys"}, headers=admins.owner)
    assert r.status_code == 404 and r.json()["error_code"] == "CLUB_NOT_FOUND"
    r = await client.put(f"{ADMIN}/{a.club.id}/address", json={"handle": "chessclub"}, headers=admins.viewer)
    assert r.status_code == 403 and r.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"
    # A club with no mailbox yet (not backfilled): the admin's change makes it, on any plan.
    r = await client.put(f"{ADMIN}/{a.club.id}/address", json={"handle": "chessclub"}, headers=admins.support)
    assert r.status_code == 200 and r.json()["set_up"] is True and r.json()["state"] == "reserved", r.text


async def test_the_admin_card_names_the_address_and_whether_it_is_automatic(client, session, clubs, admins):
    a = await clubs("Chess Circle", "chess@example.com")
    out = (await client.get(f"{ADMIN}/{a.club.id}", headers=admins.viewer)).json()
    assert out["set_up"] is False and out["handle"] is None and out["previous"] == [] and out["automatic"] is False
    await service.ensure_address(session, a.club)
    await session.commit()
    out = (await client.get(f"{ADMIN}/{a.club.id}", headers=admins.viewer)).json()
    assert out["handle"] == "chesscircle" and out["automatic"] is True and out["state"] == "reserved"


# The tests that open no database come last: a run that starts with one sets
# the suite's loop up apart from the session's engine (tests/conftest.py).

# ─── The name ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name, handle", [
    ("GDG Cloud Trivandrum", "gdgcloudtrivandrum"),
    ("IEEE SB, CET!", "ieeesbcet"),
    ("Ünïcode Clüb", "unicodeclub"),
    ("Robotics_Guild 2026", "roboticsguild2026"),
    ("A" * 45, "a" * 30),
    ("യുക്തി", "club42"),
    ("Q", "club42"),
    ("", "club42"),
    ("Support", "support2"),
    ("Info", "info2"),
    ("Post Master", "postmaster2"),
])
def test_the_address_is_the_names_letters_and_digits(name, handle):
    assert handles.from_name(name, 42) == handle
    assert handles.problem(handle) is None


def test_a_number_is_added_within_30_characters():
    base = "b" * 30
    first = list(handles.candidates(base))[:3]
    assert first == ["b" * 30, "b" * 29 + "2", "b" * 29 + "3"]
    assert all(len(h) <= 30 for h in handles.candidates(base))


def test_the_platform_inbox_addresses_are_never_a_clubs(monkeypatch):
    monkeypatch.setattr(settings, "PLATFORM_INBOX_ADDRESSES", "admin@myotherapp.in, Ops-Desk+x@MyOtherApp.in")
    assert handles.problem("ops-desk") == ("HANDLE_RESERVED", "That address is kept for the platform. Pick another.")
    monkeypatch.setattr(settings, "PLATFORM_INBOX_ADDRESSES", "")
    assert handles.problem("ops-desk") is None
