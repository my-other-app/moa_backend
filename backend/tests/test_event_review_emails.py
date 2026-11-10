"""An event's own registration review emails, and sending one again
(app/api/emails/review.py, feature-register/event-review-emails-contract.md):

- An event's copy of a review email wins over the club's library one, for
  the emails notify sends and for the confirmed ticket; dropping it goes back
  to the club's words. Event copies are never listed with the journey, sent
  to an audience, copied or saved to the library.
- `eligible` counts, per stage, the people the email is true for now.
- A resend mails each person once, counted as a club email; the same
  clientRef again mails nobody; someone whose registration moved on between
  the press and their turn is skipped.
- NOT_RESENDABLE, NOBODY, NOT_REVIEWED; a stranger is refused.
"""
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import func, select, update

from app.api.approvals.models import RegistrationRequests as R
from app.api.emails import review
from app.api.emails.models import EmailStage, ReviewResendRecipients, ReviewResends
from app.api.events.models import Events
from app.api.message_quota.models import MessageUsageRefs
from app.core import jobs
from tests.conftest import auth_header
from tests.test_approvals import _req, _shop, approve, ask, decline, mail, pushed  # noqa: F401
from tests.test_review_wording import _fake_document

API = "/api/v1"
FIELDS = ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")


@pytest_asyncio.fixture
async def shop(session, make_club, make_event, mint_token, mail, pushed, monkeypatch):  # noqa: F811
    from app.api.deliveries import service as deliveries
    from app.api.emails import service as emails

    # Tickets go through the outbox (tested there): left queued here, drawn
    # cheaply when a resend attaches one.
    async def nothing(*_a, **_k):
        return 0

    monkeypatch.setattr(deliveries, "_run_due", nothing)
    monkeypatch.setattr(emails, "ticket_document", _fake_document())

    # A resend's workers take their short transactions on the test's session.
    @asynccontextmanager
    async def scope():
        yield session

    monkeypatch.setattr(deliveries, "session_scope", scope)
    w = await _shop(session, make_club, make_event, mint_token, tag="evrev")
    w.mail = mail
    return w


def _base(w) -> str:
    return f"{API}/events/{w.event.id}/emails/review"


async def _review(client, w) -> dict:
    r = await client.get(_base(w), headers=w.h)
    assert r.status_code == 200, r.text
    return {row["stage"]: row for row in r.json()}


def _body(t: dict, **changes) -> dict:
    return {**{k: t[k] for k in FIELDS}, **changes}


def _to(mail, address: str) -> list:
    return [m for m in mail if m["recipients"] == [address]]


async def _resend(client, w, stage: str, ref=None):
    return await client.post(f"{_base(w)}/{stage}/resend", json={"clientRef": str(ref or uuid.uuid4())}, headers=w.h)


# ─── Event copies ────────────────────────────────────────────────────────


async def test_the_events_copy_wins_over_the_clubs_and_dropping_it_goes_back(client, session, shop, make_event):
    rows = await _review(client, shop)
    assert list(rows) == [s.value for s in review.STAGES]
    received = rows["REQUEST_RECEIVED"]
    assert (received["source"], received["resendable"], received["eligible"], received["lastResend"]) == ("default", True, 0, None)
    assert received["template"]["eventId"] is None and received["clubTemplateId"] == received["template"]["id"]
    assert rows["REQUEST_UNANSWERED"]["resendable"] is False

    # The club's library words, then this event's own.
    club_tpl = received["template"]
    lib = await client.put(f"{API}/clubs/me/emails/{club_tpl['id']}", json=_body(club_tpl, subject="Club words: {{event_name}}"), headers=shop.h)
    assert lib.status_code == 200, lib.text
    assert (await _review(client, shop))["REQUEST_RECEIVED"]["source"] == "club"
    own = await client.put(
        f"{_base(shop)}/REQUEST_RECEIVED",
        json=_body(club_tpl, subject="Event words: {{event_name}}", enabled=False, trigger={"mode": "BEFORE_START", "minutes": 30}),
        headers=shop.h,
    )
    assert own.status_code == 200, own.text
    copy = own.json()
    assert copy["eventId"] == shop.event.id and copy["stage"] == "REQUEST_RECEIVED" and copy["version"] == 1
    # Words only, as the library's: always on, no timer.
    assert (copy["enabled"], copy["trigger"]["mode"], copy["sourceId"]) == (True, "MANUAL", club_tpl["id"])
    again = await client.put(f"{_base(shop)}/request_received", json=_body(copy, subject="Event words again: {{event_name}}"), headers=shop.h)
    assert again.status_code == 200 and again.json()["id"] == copy["id"] and again.json()["version"] == 2
    row = (await _review(client, shop))["REQUEST_RECEIVED"]
    assert (row["source"], row["template"]["id"], row["clubTemplateId"]) == ("event", copy["id"], club_tpl["id"])

    # Someone registers: the event's words. At another event of the club: the club's.
    await ask(client, shop.event, 1)
    assert [m["subject"] for m in _to(shop.mail, "asker1@example.com")] == ["Event words again: Evrev Night"]
    other = await make_event(club=shop.club, created_by=shop.owner, name="Other Night", event_datetime=shop.event.event_datetime, requires_approval=True)
    await session.commit()
    await ask(client, other, 2)
    assert [m["subject"] for m in _to(shop.mail, "asker2@example.com")] == ["Club words: Other Night"]

    # The confirmed ticket email reads the event's copy too.
    from app.api.emails import service as emails

    ticket_tpl = rows["REQUEST_APPROVED"]["template"]
    ticket = await client.put(f"{_base(shop)}/REQUEST_APPROVED", json=_body(ticket_tpl, subject="Your seat: {{event_name}}"), headers=shop.h)
    assert ticket.status_code == 200 and ticket.json()["attachments"]["ticketPdf"] is True
    event = await session.get(Events, shop.event.id)
    assert (await emails.request_ticket_for(session, event))["subject"] == "Your seat: {{event_name}}"

    # Never with the journey, sent to an audience, copied or saved to the library.
    journey = (await client.get(f"{API}/events/{shop.event.id}/emails", headers=shop.h)).json()
    assert [s["stage"] for s in journey["journey"]] == ["INVITE", "PRE_EVENT", "REMINDER", "POST_EVENT"] and journey["custom"] == []
    for action in ("send", "duplicate", "save-to-library"):
        refused = await client.post(f"{API}/events/{shop.event.id}/emails/{copy['id']}/{action}", headers=shop.h)
        assert refused.status_code == 422, (action, refused.text)
    # The event's preview fills in the review tokens with samples.
    preview = await client.post(
        f"{API}/events/{shop.event.id}/emails/preview", json={"subject": "Note: {{decline_note}}", "design": copy["design"]}, headers=shop.h,
    )
    assert preview.status_code == 200 and preview.json()["subject"] == "Note: We had many more registrations than seats this time."
    # Its tokens are held to its stage, as the library's are.
    wrong = await client.put(f"{_base(shop)}/REQUEST_RECEIVED", json=_body(copy, subject="Pay {{pay_url}}"), headers=shop.h)
    assert wrong.status_code == 422 and "pay_url" in wrong.json()["message"]
    assert (await client.put(f"{_base(shop)}/INVITE", json=_body(copy), headers=shop.h)).status_code == 404

    # Dropped: the club's words again, for the next person; dropping nothing is no error.
    dropped = await client.delete(f"{_base(shop)}/REQUEST_RECEIVED", headers=shop.h)
    assert dropped.status_code == 200, dropped.text
    assert (dropped.json()["id"], dropped.json()["eventId"], dropped.json()["subject"]) == (club_tpl["id"], None, "Club words: {{event_name}}")
    assert (await client.delete(f"{_base(shop)}/REQUEST_RECEIVED", headers=shop.h)).json()["id"] == club_tpl["id"]
    assert (await _review(client, shop))["REQUEST_RECEIVED"]["source"] == "club"
    await ask(client, shop.event, 3)
    assert [m["subject"] for m in _to(shop.mail, "asker3@example.com")] == ["Club words: Evrev Night"]


# ─── Who a resend reaches ────────────────────────────────────────────────


async def test_eligible_counts_each_stage(client, session, shop, make_club, make_event, mint_token, mail):  # noqa: F811
    ids = [(await ask(client, shop.event, 10 + i)).json()["approval"]["id"] for i in range(6)]
    # 10, 11 pending; 12 confirmed (a free ticket); 13 declined; 14 withdrawn; 15 lapsed.
    assert (await approve(client, shop, ids=[ids[2]])).json()["approved"]
    assert (await decline(client, shop, ids=[ids[3]], notify=False)).json()["declined"] == [ids[3]]
    await session.execute(update(R).where(R.id == uuid.UUID(ids[4])).values(status="withdrawn"))
    await session.execute(update(R).where(R.id == uuid.UUID(ids[5])).values(status="lapsed"))
    await session.commit()

    rows = await _review(client, shop)
    expected = {
        "REQUEST_RECEIVED": 2, "REQUEST_APPROVED": 1, "REQUEST_APPROVED_PAY": 0, "REQUEST_DECLINED": 1, "REQUEST_LAPSED": 1,
        "REQUEST_UNANSWERED": 0,
    }
    assert {s: r["eligible"] for s, r in rows.items()} == expected
    for stage, n in expected.items():
        counted = await client.post(f"{_base(shop)}/{stage}/resend/count", headers=shop.h)
        assert counted.status_code == 200 and counted.json() == {"eligible": n, "withoutEmail": 0}, (stage, counted.text)

    # A paid event: confirmed and owing the payment, while the hold stands.
    paid = await _shop(session, make_club, make_event, mint_token, tag="evrevpaid", has_fee=True, reg_fee=Decimal("99.00"))
    pid = [(await ask(client, paid.event, 20 + i)).json()["approval"]["id"] for i in range(2)]
    assert len((await approve(client, paid, ids=pid)).json()["approved"]) == 2
    counts = {s: r["eligible"] for s, r in (await _review(client, paid)).items()}
    assert (counts["REQUEST_APPROVED_PAY"], counts["REQUEST_APPROVED"], counts["REQUEST_RECEIVED"]) == (2, 0, 0)
    from app.api.ticketing.models import WaitlistEntries

    held = await _req(session, pid[0])
    await session.execute(update(WaitlistEntries).where(WaitlistEntries.id == held.waitlist_entry_id).values(offer_expires_at=paid.event.event_datetime - timedelta(days=30)))
    await session.commit()
    assert (await client.post(f"{_base(paid)}/REQUEST_APPROVED_PAY/resend/count", headers=paid.h)).json()["eligible"] == 1


# ─── Sending again ───────────────────────────────────────────────────────


async def test_a_resend_mails_each_person_once_and_the_same_ref_mails_nobody(client, session, shop):
    for i in range(3):
        await ask(client, shop.event, 30 + i)
    lib = (await _review(client, shop))["REQUEST_RECEIVED"]["template"]
    saved = await client.put(f"{_base(shop)}/REQUEST_RECEIVED", json=_body(lib, subject="New words for {{first_name}}"), headers=shop.h)
    assert saved.status_code == 200
    shop.mail.clear()

    ref = uuid.uuid4()
    r = await _resend(client, shop, "REQUEST_RECEIVED", ref)
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["stage"], out["recipients"], out["createdBy"]["id"]) == ("REQUEST_RECEIVED", 3, shop.owner.id)
    assert set(out) == {"id", "stage", "createdAt", "createdBy", "recipients", "sent", "failed", "skipped", "status"}
    await jobs.settle()
    assert sorted(m["recipients"][0] for m in shop.mail) == ["asker30@example.com", "asker31@example.com", "asker32@example.com"]
    assert {m["subject"] for m in shop.mail} == {"New words for Asker"}
    keys = {m["idempotency_key"] for m in shop.mail}
    assert len(keys) == 3 and all(k.startswith(f"resend:{out['id']}:") for k in keys)

    # The same press again: the same resend, nobody mailed.
    shop.mail.clear()
    again = await _resend(client, shop, "REQUEST_RECEIVED", ref)
    assert again.status_code == 200 and again.json()["id"] == out["id"]
    await jobs.settle()
    assert shop.mail == []
    listed = (await client.get(f"{_base(shop)}/resends", headers=shop.h)).json()
    assert [(x["id"], x["sent"], x["failed"], x["skipped"], x["status"]) for x in listed] == [(out["id"], 3, 0, 0, "done")]
    assert (await _review(client, shop))["REQUEST_RECEIVED"]["lastResend"]["id"] == out["id"]
    assert await session.scalar(select(func.count()).select_from(ReviewResends).where(ReviewResends.event_id == shop.event.id)) == 1
    # Counted against the club's emails, once a person.
    counted = await session.scalar(select(func.count()).select_from(MessageUsageRefs).where(MessageUsageRefs.ref.like(f"%resend:{out['id']}:%")))
    assert counted == 3

    # Another press is another resend: each once more.
    second = await _resend(client, shop, "REQUEST_RECEIVED")
    assert second.status_code == 200 and second.json()["id"] != out["id"]
    await jobs.settle()
    assert len(shop.mail) == 3
    assert [x["id"] for x in (await client.get(f"{_base(shop)}/resends", headers=shop.h)).json()] == [second.json()["id"], out["id"]]


async def test_someone_who_moved_on_before_their_turn_is_skipped(client, session, shop):
    ids = [(await ask(client, shop.event, 40 + i)).json()["approval"]["id"] for i in range(3)]
    event = await session.get(Events, shop.event.id)
    # Pressed (the rows are written), then one is confirmed and one withdraws before the job runs.
    resend = await review.start(session, event, EmailStage.REQUEST_RECEIVED, client_ref=uuid.uuid4(), created_by=shop.owner.id)
    assert resend.recipients == 3
    await session.execute(update(R).where(R.id == uuid.UUID(ids[0])).values(status="approved"))
    await session.execute(update(R).where(R.id == uuid.UUID(ids[1])).values(status="withdrawn"))
    await session.commit()
    shop.mail.clear()
    await jobs.settle()
    assert [m["recipients"] for m in shop.mail] == [["asker42@example.com"]]
    done = await session.get(ReviewResends, resend.id, populate_existing=True)
    assert (done.status, done.sent_count, done.skipped_count, done.failed_count) == ("done", 1, 2, 0)
    skipped = (await session.scalars(
        select(ReviewResendRecipients.error).where(ReviewResendRecipients.resend_id == resend.id, ReviewResendRecipients.status == "skipped")
    )).all()
    assert skipped == [review.MOVED_ON, review.MOVED_ON]


async def test_the_confirmed_ticket_goes_again_with_the_ticket(client, session, shop):
    rid = (await ask(client, shop.event, 50)).json()["approval"]["id"]
    assert (await approve(client, shop, ids=[rid])).json()["approved"]
    shop.mail.clear()
    r = await _resend(client, shop, "REQUEST_APPROVED")
    assert r.status_code == 200 and r.json()["recipients"] == 1, r.text
    await jobs.settle()
    got = _to(shop.mail, "asker50@example.com")
    assert len(got) == 1 and got[0]["subject"] == "Your ticket for Evrev Night" and got[0]["attachments"]


# ─── Refusals ────────────────────────────────────────────────────────────


async def test_what_cant_be_sent_again_is_refused_and_strangers_see_nothing(client, session, shop, make_club, make_event, mint_token, monkeypatch):
    event_id = shop.event.id
    unanswered = await _resend(client, shop, "REQUEST_UNANSWERED")
    assert unanswered.status_code == 422 and unanswered.json()["error_code"] == "NOT_RESENDABLE"
    nobody = await _resend(client, shop, "REQUEST_DECLINED")
    assert nobody.status_code == 422 and nobody.json()["error_code"] == "NOBODY"
    assert await session.scalar(select(func.count()).select_from(ReviewResends).where(ReviewResends.event_id == event_id)) == 0
    assert (await _resend(client, shop, "INVITE")).status_code == 404

    # The club's emails left this month (plan and credits) can't cover everyone: refused whole.
    from app.api.message_quota import service as quota
    from app.config import settings

    for n in (60, 61, 62):
        await ask(client, shop.event, n)
    shop.mail.clear()

    async def five_a_month(_session, club_id):
        return quota.Terms(
            club_id=club_id, plan_key="free", plan_name="Free", email_limit=5, whatsapp_limit=0,
            email_rate_paise=0, whatsapp_rate_paise=0, billing=quota.NONE,
        )

    monkeypatch.setattr(quota, "terms_for", five_a_month)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    short = await _resend(client, shop, "REQUEST_RECEIVED")
    assert short.status_code == 402 and short.json()["error_code"] == review.QUOTA_CODE, short.text
    # The three "thanks for registering" emails used three of the month's five.
    assert short.json()["errors"] == {"needed": 3, "available": 2}
    assert shop.mail == [] and await session.scalar(select(func.count()).select_from(ReviewResends).where(ReviewResends.event_id == event_id)) == 0
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", False)

    # An event that doesn't review registrations.
    await session.execute(update(Events).where(Events.id == event_id).values(requires_approval=False))
    await session.commit()
    off = await _resend(client, shop, "REQUEST_RECEIVED")
    assert off.status_code == 422 and off.json()["error_code"] == "NOT_REVIEWED"

    # Another club's owner: nothing to read, save, drop or send.
    stranger, _club = await make_club(name="Stranger Club", email="stranger-evrev@example.com")
    await session.commit()
    sh = auth_header(await mint_token(stranger))
    tpl = (await _review(client, shop))["REQUEST_RECEIVED"]["template"]
    for method, path, body in (
        ("GET", _base(shop), None),
        ("GET", f"{_base(shop)}/resends", None),
        ("PUT", f"{_base(shop)}/REQUEST_RECEIVED", _body(tpl)),
        ("DELETE", f"{_base(shop)}/REQUEST_RECEIVED", None),
        ("POST", f"{_base(shop)}/REQUEST_RECEIVED/resend/count", None),
        ("POST", f"{_base(shop)}/REQUEST_RECEIVED/resend", {"clientRef": str(uuid.uuid4())}),
    ):
        r = await client.request(method, path, json=body, headers=sh)
        assert r.status_code in (403, 404), (method, path, r.status_code, r.text)
