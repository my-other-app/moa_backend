"""Who may do what, tightened after the November 2026 review.

* Door staff download no list: every export, spreadsheet, archive, bundle
  or batch is refused to them by a rule on the path (club_access.policy
  `_TAKEAWAY`), so an export written later is covered too -- the walk below
  fails on any download route the rule does not reach. They still let
  people in.
* Discount codes are money: owners and admins only.
* An event's retention and its report's recipients are the host's owner's
  and admins' alone, and the schedule never deletes within a day of the end.
* An admin changing a club's records needs MANAGE_CONTENT, on every
  admin-only write route; device tokens are BROADCAST's.
* A draft is not public: 404 unless the caller hosts or co-hosts it, or is
  an admin.
* A failed payment order says nothing of its cause.
"""
import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from app.api.club_access import policy
from app.api.closeout import service as closeout
from app.api.closeout.models import EventStatus, RetentionJobs, RetentionMode
from app.api.cohosts.models import ACCEPTED, EventCohosts
from app.api.users.models import AdminRoles
from app.core.rate_limit import limiter
from tests.club_ops_helpers import member_header
from tests.conftest import auth_header

API = "/api/v1"


# ─── The app's routes ───────────────────────────────────────────────────────


def _walk(routes):
    from fastapi.routing import _IncludedRouter

    for r in routes:
        if isinstance(r, _IncludedRouter):
            yield from _walk(r.effective_candidates())
        else:
            yield r


def _deps(dependant, into: list) -> list:
    for d in dependant.dependencies:
        into.append(d.call)
        _deps(d, into)
    return into


def _roles(calls) -> set:
    out: set = set()
    for call in calls:
        if "role_checker" in getattr(call, "__qualname__", ""):
            for cell in call.__closure__ or ():
                if isinstance(cell.cell_contents, list):
                    out.update(cell.cell_contents)
    return out


def _routes():
    """(method, path, summary, the dependencies' callables) for every route."""
    from doth import application

    for ctx in _walk(application.routes):
        route = getattr(ctx, "original_route", None)
        if route is None or not hasattr(route, "dependant"):
            continue
        calls = _deps(route.dependant, [])
        for method in sorted((route.methods or set()) - {"HEAD"}):
            yield method, ctx.path, (getattr(route, "summary", "") or ""), calls


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "7", path)


# ─── 1. Door staff take no list away ────────────────────────────────────────

# First in the file: a run that starts with a test opening no database sets
# the suite's loop up apart from the session's engine (tests/conftest.py).


@pytest.fixture
async def club_world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch):
    monkeypatch.setattr(limiter, "enabled", False)
    owner, club = await make_club(name="Gatehouse", email="gatehouse@example.com")
    event = await make_event(club=club, created_by=owner, name="Gate Night", event_datetime=datetime.now(timezone.utc) - timedelta(hours=30), duration=2.0)
    guest = await make_user(email="guest-hardening@example.com")
    reg = await make_registration(event=event, user=guest, full_name="Asha Guest", phone="9876543210")
    await session.commit()
    return dict(owner=owner, club=club, event=event, reg=reg, h=auth_header(await mint_token(owner)))


async def test_door_staff_are_refused_the_guest_list_file_and_still_check_people_in(client, session, club_world):
    e, reg = club_world["event"], club_world["reg"]
    door = await member_header(client, session, club_world["club"], "door")
    for url in (
        f"{API}/events/registration/{e.id}/export",
        f"{API}/events/{e.id}/ticketing/waitlist/export",
        f"{API}/clubs/followers/export.csv",
        f"{API}/events/registration/{e.id}/requests/export",
        f"{API}/clubs/discount-codes",
    ):
        r = await client.get(url, headers=door)
        assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN", (url, r.status_code, r.text[:200])
    # The gate's work goes on.
    assert (await client.get(f"{API}/events/registration/{e.id}/attendees", headers=door)).status_code == 200
    came = await client.post(f"{API}/events/registration/{e.id}/attendance/{reg.id}", json={"is_attended": True}, headers=door)
    assert came.status_code == 200, came.text
    assert (await client.get(f"{API}/clubs/access/me", headers=door)).json()["can"]["exports"] is False
    # The owner and an editor still download it.
    assert (await client.get(f"{API}/events/registration/{e.id}/export", headers=club_world["h"])).status_code == 200
    editor = await member_header(client, session, club_world["club"], "editor")
    assert (await client.get(f"{API}/events/registration/{e.id}/export", headers=editor)).status_code == 200


#: Words in a route's path or summary that say it hands over a file of many.
LOOKS_LIKE_A_LIST = re.compile(r"export|\.csv|\.xlsx|\.zip|\.mbox|bundle|batch|spreadsheet|as a file|every .*(pdf|ticket|certificate|card)", re.I)
#: Downloads door staff keep, each one document about one guest or the event
#: itself -- never a list of people.
DOOR_KEEPS = {
    "POST /api/v1/events/{event_id}/gate/scan-batch": "the door's own offline scans, sent up: a change it makes, not a download",
}


def _club_reads():
    for method, path, summary, calls in _routes():
        if "club" in _roles(calls):
            yield method, path, summary


def test_every_download_of_a_list_is_refused_to_door_staff():
    found = []
    for method, path, summary in _club_reads():
        if not (LOOKS_LIKE_A_LIST.search(path) or LOOKS_LIKE_A_LIST.search(summary)):
            continue
        key = f"{method} {path}"
        if key in DOOR_KEEPS:
            continue
        found.append(key)
        assert policy.allows("door", method, _concrete(path)) is False, f"door staff may download {key} ({summary})"
    # The walk saw the ones the review named (it is not looking at nothing).
    for named in (
        "GET /api/v1/events/registration/{event_id}/export",
        "GET /api/v1/events/{event_id}/ticketing/waitlist/export",
        "GET /api/v1/clubs/followers/export.csv",
        "GET /api/v1/events/registration/{event_id}/requests/export",
        "GET /api/v1/events/{event_id}/tickets/bundle.zip",
        "GET /api/v1/events/{event_id}/certificates/bundle.zip",
        "GET /api/v1/events/{event_id}/idcards/batch.pdf",
        "GET /api/v1/clubs/contacts/export.csv",
    ):
        assert named in found, named


@pytest.mark.parametrize("role, method, path, allowed", [
    # Door staff: no list, whatever its shape; the gate and one guest's documents still.
    ("door", "GET", "/api/v1/events/registration/7/export", False),
    ("door", "GET", "/api/v1/events/registration/7/export?is_paid=true", False),
    ("door", "GET", "/api/v1/events/7/ticketing/waitlist/export", False),
    ("door", "GET", "/api/v1/clubs/followers/export.csv", False),
    ("door", "GET", "/api/v1/events/registration/7/requests/export", False),
    ("door", "GET", "/api/v1/events/7/tickets/bundle.zip", False),
    ("door", "GET", "/api/v1/events/7/certificates/bundle.zip", False),
    ("door", "GET", "/api/v1/events/7/idcards/batch.pdf", False),
    ("door", "GET", "/api/v1/clubs/mailbox/export", False),
    ("door", "GET", "/api/v1/events/7/someday/export.xlsx", False),
    ("door", "GET", "/api/v1/events/registration/7/attendees", True),
    ("door", "GET", "/api/v1/events/registration/7/list", True),
    ("door", "GET", "/api/v1/events/7/idcards/0b7f6a1e-9c40-4f0e-9d3c-2b7f6a1e9c40.pdf", True),
    ("door", "POST", "/api/v1/events/7/gate/scan-batch", True),
    ("door", "POST", "/api/v1/events/7/gate/scan", True),
    ("door", "POST", "/api/v1/events/registration/7/attendance/44", True),
    # Editors and admins still export.
    ("editor", "GET", "/api/v1/events/registration/7/export", True),
    ("editor", "GET", "/api/v1/events/7/ticketing/waitlist/export", True),
    ("admin", "GET", "/api/v1/events/7/tickets/bundle.zip", True),
    # Discount codes: money.
    ("door", "GET", "/api/v1/clubs/discount-codes", False),
    ("door", "GET", "/api/v1/events/7/ticketing/codes", False),
    ("editor", "GET", "/api/v1/clubs/discount-codes", False),
    ("editor", "POST", "/api/v1/clubs/discount-codes", False),
    ("editor", "PATCH", "/api/v1/clubs/discount-codes/3", False),
    ("editor", "DELETE", "/api/v1/clubs/discount-codes/3", False),
    ("editor", "GET", "/api/v1/events/7/ticketing/codes", False),
    ("admin", "GET", "/api/v1/clubs/discount-codes", True),
    ("admin", "POST", "/api/v1/clubs/discount-codes", True),
    ("admin", "DELETE", "/api/v1/clubs/discount-codes/3", True),
    # Not the codes: a guest's price check is no member's request, and ticket types are the event's setup.
    ("editor", "PATCH", "/api/v1/events/7/ticketing/types/3", True),
])
def test_the_new_role_rules(role, method, path, allowed):
    assert policy.allows(role, method, path.split("?")[0]) is allowed


def test_what_the_dashboard_offers_matches_the_new_rules():
    for role in ("owner", "admin", "editor", "door"):
        can = policy.can(role)
        if role != "owner":
            assert can["exports"] == policy.allows(role, "GET", "/api/v1/events/registration/1/export")
            assert can["discount_codes"] == policy.allows(role, "GET", "/api/v1/clubs/discount-codes")
        assert can["retention_settings"] is (role in ("owner", "admin"))
    assert policy.can("door")["exports"] is False and policy.can("editor")["exports"] is True
    assert policy.can("editor")["discount_codes"] is False and policy.can("admin")["discount_codes"] is True


async def test_editors_are_refused_discount_codes_and_admins_keep_them(client, session, club_world):
    editor = await member_header(client, session, club_world["club"], "editor")
    admin = await member_header(client, session, club_world["club"], "admin")
    code = {"code": "FREEPASS", "kind": "percent", "value": 100}
    refused = await client.post(f"{API}/clubs/discount-codes", json=code, headers=editor)
    assert refused.status_code == 403 and refused.json()["error_code"] == "ROLE_FORBIDDEN"
    assert (await client.get(f"{API}/clubs/discount-codes", headers=editor)).status_code == 403
    made = await client.post(f"{API}/clubs/discount-codes", json=code, headers=admin)
    assert made.status_code in (200, 201), made.text
    assert (await client.get(f"{API}/clubs/discount-codes", headers=club_world["h"])).status_code == 200


# ─── 2. Retention and the report's recipients ───────────────────────────────


@pytest.fixture
async def cohosted(club_world, session, make_club, mint_token):
    co_owner, co_club = await make_club(name="Partner Society", email="partner-society@example.com")
    now = datetime.now(timezone.utc)
    session.add(EventCohosts(event_id=club_world["event"].id, club_id=co_club.id, status=ACCEPTED, invited_at=now, expires_at=now + timedelta(days=3), responded_at=now))
    await session.commit()
    return dict(club_world, co_club=co_club, co_h=auth_header(await mint_token(co_owner)))


def _settings(world):
    return f"{API}/events/{world['event'].id}/closeout/settings"


async def test_a_cohost_cannot_schedule_the_wipe_or_redirect_the_report(client, session, cohosted):
    url = _settings(cohosted)
    keeps = (await client.get(url, headers=cohosted["h"])).json()["keep_contacts"]
    for body in (
        {"retention_mode": "delete"},
        {"retention_hours": 48},
        {"report_recipients": ["someone@example.com"]},
        {"keep_contacts": not keeps},
        {"status": "closed", "retention_mode": "delete"},
    ):
        r = await client.patch(url, json=body, headers=cohosted["co_h"])
        assert r.status_code == 403 and r.json()["error_code"] == "COHOST_HOST_ONLY", (body, r.text)
    # Nothing changed.
    await session.refresh(cohosted["event"])
    assert (cohosted["event"].retention_mode or RetentionMode.keep) is RetentionMode.keep and not cohosted["event"].report_recipients
    # The status is still the co-host's to move, and the form sent back as it was is no change.
    shown = (await client.get(url, headers=cohosted["co_h"])).json()
    form = {k: shown[k] for k in ("retention_mode", "retention_hours", "report_recipients", "keep_contacts")}
    moved = await client.patch(url, json={**form, "status": "closed"}, headers=cohosted["co_h"])
    assert moved.status_code == 200 and moved.json()["status"] == "closed", moved.text


async def test_an_editor_cannot_change_retention_and_the_owner_and_admins_can(client, session, club_world):
    url = _settings(club_world)
    editor = await member_header(client, session, club_world["club"], "editor")
    r = await client.patch(url, json={"retention_mode": "delete", "retention_hours": 24}, headers=editor)
    assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN", r.text
    r = await client.patch(url, json={"report_recipients": ["elsewhere@example.com"]}, headers=editor)
    assert r.status_code == 403 and r.json()["error_code"] == "ROLE_FORBIDDEN", r.text
    # An editor still publishes and closes (the MCP's "publish_event" is this request).
    assert (await client.patch(url, json={"status": "closed"}, headers=editor)).status_code == 200
    admin = await member_header(client, session, club_world["club"], "admin")
    ok = await client.patch(url, json={"retention_mode": "delete", "retention_hours": 36}, headers=admin)
    assert ok.status_code == 200 and ok.json()["retention_mode"] == "delete" and ok.json()["retention_hours"] == 36, ok.text
    ok = await client.patch(url, json={"report_recipients": ["Ops@example.com"]}, headers=club_world["h"])
    assert ok.status_code == 200 and ok.json()["report_recipients"] == ["ops@example.com"], ok.text


async def test_retention_is_never_sooner_than_a_day_after_the_end(client, session, club_world):
    url = _settings(club_world)
    for hours in (1, 23):
        r = await client.patch(url, json={"retention_mode": "delete", "retention_hours": hours}, headers=club_world["h"])
        assert r.status_code == 422, r.text
    assert (await client.patch(url, json={"retention_mode": "delete", "retention_hours": 24}, headers=club_world["h"])).status_code == 200
    # A design saved before the floor: one hour after an event that ended six hours ago.
    event = club_world["event"]
    event.event_datetime = datetime.now(timezone.utc) - timedelta(hours=8)
    event.retention_hours = 1
    await session.commit()
    assert closeout.settings_of(event)["retention_hours"] == 24
    job = await closeout.ensure_job(session, event)
    job.run_at = datetime.now(timezone.utc) - timedelta(hours=5)  # as it was saved
    await session.commit()
    ran = await closeout.run_retention(session, event, manual=False)
    assert ran["refusal"] == "NOT_DUE" and ran["state"] == "pending"
    await session.refresh(job)
    assert job.run_at > datetime.now(timezone.utc) + timedelta(hours=15)
    await session.refresh(club_world["reg"])
    assert club_world["reg"].phone == "9876543210"
    # And the sweep leaves it until then.
    assert (await closeout.sweep(session))["queued"] == 0
    assert (await session.get(RetentionJobs, job.id)).state.value == "pending"


# ─── 3. Admin capabilities ──────────────────────────────────────────────────

#: Admin-only writes that ask for no capability, and why that is right.
ANY_ADMIN_WRITES = {
    "POST /api/v1/admin/club-domains/{domain_id}/recheck": "reads Cloudflare again and moves the domain on as the sweep would: no change of an admin's making",
}
#: The team desk's writes (app/api/support, app/api/leads): what an admin may
#: do there is decided by the team they are in and whose case or lead it is,
#: not by their role -- a viewer-role admin in the support team answers cases
#: (feature-register/support-leads-contract.md, "Teams and rights") -- in each
#: handler, through app.api.support.teams.Rights; tests/test_support_desk.py and
#: tests/test_leads.py hold every refusal. The desk's settings (teams, topics,
#: saved replies, help answers, lost reasons) are owner-only and do ask (MANAGE_ADMINS).
TEAM_DESK_WRITES = ("/api/v1/admin/support/cases", "/api/v1/admin/leads", "/api/v1/admin/follow-ups", "/api/v1/admin/teams/me")


def test_every_admin_only_write_asks_for_a_capability():
    """`AdminAuth` admits a read-only viewer. A route only admins call that
    changes anything must also ask for what the change needs (requires(...),
    or a club route's own MANAGE_CONTENT / DESTROY)."""
    missing = []
    for method, path, _summary, calls in _routes():
        if method not in ("POST", "PUT", "PATCH", "DELETE") or _roles(calls) != {"admin"}:
            continue
        if any("requires.<locals>.checker" in getattr(c, "__qualname__", "") for c in calls):
            continue
        key = f"{method} {path}"
        if key not in ANY_ADMIN_WRITES and not path.startswith(TEAM_DESK_WRITES):
            missing.append(key)
    assert missing == []


@pytest.fixture
async def admins(make_admin, mint_token):
    viewer = await make_admin(email="viewer-hardening@example.com", role=AdminRoles.viewer)
    support = await make_admin(email="support-hardening@example.com", role=AdminRoles.support)
    owner = await make_admin(email="owner-hardening@example.com", role=AdminRoles.owner)
    return {k: auth_header(await mint_token(v)) for k, v in (("viewer", viewer), ("support", support), ("owner", owner))}


async def test_a_viewer_admin_cannot_bulk_import_registrations(client, session, club_world, admins, monkeypatch):
    from app.api.events.registration import service as registration_service

    queued = []

    async def enqueue(session, event_id, df, log, *args, **kwargs):
        queued.append((event_id, len(df)))

    monkeypatch.setattr(registration_service, "enqueue_import", enqueue)
    e = club_world["event"]

    def upload():
        return {"file": ("guests.csv", io.BytesIO(b"full_name,email\nRiya,riya@example.com\n"), "text/csv")}

    for who in ("viewer", "support"):
        r = await client.post(f"{API}/events/registration/{e.id}/bulk-import", files=upload(), headers=admins[who])
        assert r.status_code == 403 and r.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE", (who, r.text)
    assert queued == []
    r = await client.post(f"{API}/events/registration/{e.id}/bulk-import", files=upload(), headers=admins["owner"])
    assert r.status_code == 200, r.text
    assert queued == [(e.id, 1)]


@pytest.mark.parametrize("path", ["geo-backfill", "geocode-backfill", "recompute-club-places", "reconcile-counters"])
async def test_the_backfills_need_manage_content(client, admins, path):
    r = await client.post(f"{API}/admin/discover/{path}", headers=admins["viewer"])
    assert r.status_code == 403 and r.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE", r.text


async def test_an_owner_admin_still_runs_a_backfill(client, admins):
    assert (await client.post(f"{API}/admin/discover/reconcile-counters", headers=admins["owner"])).status_code == 200


async def test_device_tokens_are_for_those_who_may_push_to_them(client, admins):
    for who in ("viewer", "support"):
        r = await client.get(f"{API}/notifications/test/tokens", headers=admins[who])
        assert r.status_code == 403 and r.json()["error_code"] == "INSUFFICIENT_ADMIN_ROLE"
    assert (await client.get(f"{API}/notifications/test/tokens", headers=admins["owner"])).status_code == 200


# ─── 4. Drafts are not public ───────────────────────────────────────────────


async def test_a_draft_is_found_only_by_its_hosts_and_admins(client, session, cohosted, make_club, make_user, mint_token, admins):
    event = cohosted["event"]
    event.lifecycle = EventStatus.draft
    event.contact_email = "organiser@example.com"
    await session.commit()
    stranger_owner, _ = await make_club(name="Stranger Club", email="stranger-club@example.com")
    stranger = auth_header(await mint_token(stranger_owner))
    app_user = auth_header(await mint_token(await make_user(email="curious@example.com")))
    for ref in (event.id, event.slug):
        for who in (None, app_user, stranger):
            r = await client.get(f"{API}/events/info/{ref}", headers=who or {})
            assert r.status_code == 404 and "organiser@example.com" not in r.text, (ref, r.status_code)
    door = await member_header(client, session, cohosted["club"], "door")
    for who in (cohosted["h"], door, cohosted["co_h"], admins["viewer"]):
        r = await client.get(f"{API}/events/info/{event.id}", headers=who)
        assert r.status_code == 200 and r.json()["status"] == "draft", r.text
    # Published, it is everyone's again.
    event.lifecycle = EventStatus.published
    await session.commit()
    assert (await client.get(f"{API}/events/info/{event.slug}")).status_code == 200
    assert (await client.get(f"{API}/discover/events/{event.slug}")).status_code == 200


async def test_a_draft_is_in_no_public_feed(client, session, club_world):
    event = club_world["event"]
    event.lifecycle = EventStatus.draft
    event.event_datetime = datetime.now(timezone.utc) + timedelta(days=3)
    await session.commit()
    assert (await client.get(f"{API}/discover/events/{event.slug}")).status_code == 404
    assert (await client.get(f"{API}/discover/events/{event.id}")).status_code == 404
    sitemap = await client.get(f"{API}/discover/sitemap")
    assert sitemap.status_code == 200 and event.slug not in sitemap.text


# ─── 5. A failed order says nothing of its cause ────────────────────────────


async def test_a_failed_payment_order_hides_its_cause(client, session, club_world, make_user, mint_token, monkeypatch):
    from app.api.payments import service as payments

    async def boom(**kwargs):
        raise RuntimeError("connection to db-internal:5432 refused, key rzp_secret_123")

    monkeypatch.setattr(payments, "create_razorpay_order", boom)
    h = auth_header(await mint_token(await make_user(email="payer-hardening@example.com")))
    r = await client.post(f"{API}/payments/orders", json={"source": "event_registration", "payload": {"registration_id": str(club_world["reg"].id)}}, headers=h)
    assert r.status_code == 500, r.text
    assert r.json()["message"] == "Failed to create order" and "rzp_secret" not in r.text and "5432" not in r.text
