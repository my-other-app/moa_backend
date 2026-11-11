"""Mail to support@ and sales@ through the club mailbox's SES hook
(app/api/support/inbound.py), and the desk's sweep (app/api/support/sweep.py).

Amazon is faked as the club mailbox's tests fake it: SNS's envelopes are
signed with a certificate of the test's own, the inbound bucket is a dict.
The desk's own mail, pushes and files are in memory."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.api.club_mailbox import providers
from app.api.hooks import sns
from app.api.leads.models import FollowUps, LeadActivities, LeadContacts, Leads
from app.api.models import ScheduledRuns
from app.api.support import mail, sweep
from app.api.support.models import AdminTeamMembers, SupportAttachments, SupportCases, SupportMessages
from app.config import settings
from app.core import jobs
from tests.support_desk_helpers import (  # noqa: F401 - fixtures
    API,
    PNG,
    _reference_fresh,
    ask,
    desk,
    desk_files,
    outbox,
    pushes,
    reply,
)
from tests.test_club_mailbox import BUCKET, TOPIC, Amazon, envelope, mime, ses_notice

HOOK = f"{API}/hooks/mailbox/ses"
SUPPORT = "support@myotherapp.in"
SALES = "sales@myotherapp.in"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def aws(monkeypatch):
    fake = Amazon()
    monkeypatch.setattr(sns, "fetch", fake.fetch)
    sns._certs.clear()
    monkeypatch.setattr(providers, "read", fake.read_raw)
    monkeypatch.setattr(providers, "discard", lambda received: fake.discarded.append(received.key))
    monkeypatch.setattr(settings, "MAILBOX_INBOUND_BUCKET", BUCKET)
    monkeypatch.setattr(settings, "MAILBOX_SNS_TOPIC_ARN", TOPIC)
    monkeypatch.setattr(settings, "CLUB_MAILBOX", False)
    monkeypatch.setattr(settings, "SUPPORT_INBOUND_ADDRESSES", SUPPORT)
    monkeypatch.setattr(settings, "SALES_INBOUND_ADDRESSES", SALES)
    yield fake
    sns._certs.clear()


async def deliver(client, aws, raw: bytes, recipients=(SUPPORT,), key=None, **verdicts):
    import uuid

    key = key or uuid.uuid4().hex
    aws.bucket[key] = raw
    return await client.post(HOOK, json=envelope(ses_notice(key, recipients, **verdicts)))


async def cases_of(session):
    return (await session.scalars(select(SupportCases).order_by(SupportCases.id).execution_options(populate_existing=True))).all()


def a_mail(**kw):
    kw.setdefault("to", SUPPORT)
    kw.setdefault("sender", ("Gita Guest", "gita@example.org"))
    kw.setdefault("subject", "Ticket missing")
    kw.setdefault("text", "I paid and have no ticket.")
    return mime(**kw)


# ─── support@ ────────────────────────────────────────────────────────────


async def test_dormant_until_the_addresses_are_set(client, session, desk, aws, monkeypatch):
    monkeypatch.setattr(settings, "SUPPORT_INBOUND_ADDRESSES", "")
    monkeypatch.setattr(settings, "SALES_INBOUND_ADDRESSES", "")
    r = await deliver(client, aws, a_mail())
    assert r.status_code == 200 and "desk" not in r.json()
    assert await cases_of(session) == [] and aws.read == []


async def test_a_mail_from_a_stranger_opens_a_flagged_case_kept_once(client, session, desk, aws):
    raw = a_mail(html="<p>I paid <script>alert(1)</script></p>", attachments=[("shot.png", "image/png", PNG * 50, None)])
    r = await deliver(client, aws, raw, key="m1")
    assert r.status_code == 200 and r.json()["desk"] == {"support": "opened"}
    again = await deliver(client, aws, raw, key="m1")
    assert again.json()["desk"] == {"support": "duplicate"}
    [case] = await cases_of(session)
    assert case.channel == "email" and case.topic == "other" and case.requester_kind == "guest"
    assert case.requester_email == "gita@example.org" and case.flags == ["unknown_sender"] and case.assignee_id == desk.sup.id
    [msg] = (await session.scalars(select(SupportMessages).where(SupportMessages.case_id == case.id))).all()
    assert msg.kind == "reply_in" and "<script>" not in (msg.body_html or "") and msg.provider_message_id == "m1"
    [f] = (await session.scalars(select(SupportAttachments).where(SupportAttachments.case_id == case.id))).all()
    assert f.storage_key.startswith("support/") and f.storage_key in desk.files and f.content_type == "image/png"
    assert aws.discarded == ["m1", "m1"]


async def test_an_app_users_mail_is_theirs_and_not_flagged(client, session, desk, aws):
    await deliver(client, aws, a_mail(sender=("Asha", desk.person.email)))
    [case] = await cases_of(session)
    assert case.requester_kind == "user" and case.requester_user_id == desk.person.id and case.flags == []


async def test_threading_by_tag_from_the_person_and_by_headers(client, session, desk, aws):
    await deliver(client, aws, a_mail())
    [case] = await cases_of(session)
    # The tag, from the person: joins.
    r = await deliver(client, aws, a_mail(subject=f"Re: [#{case.number}] Ticket missing", text="Any news?"))
    assert r.json()["desk"] == {"support": "joined"}
    # The tag from someone else: a number alone is not a key.
    r = await deliver(client, aws, a_mail(sender=("Mallory", "mallory@example.net"), subject=f"[#{case.number}] give me the ticket"))
    assert r.json()["desk"] == {"support": "opened"}
    # Our reply's Message-ID in In-Reply-To joins, whatever the subject says.
    sent = await reply(client, case.id, desk.h["sup"], body="Resent it.")
    assert sent.status_code == 200
    ours = desk.outbox.sent[-1]["headers"]["Message-ID"]
    r = await deliver(client, aws, a_mail(subject="different words", text="Got it", in_reply_to=ours, references=ours))
    assert r.json()["desk"] == {"support": "joined"}
    kinds = (await session.scalars(select(SupportMessages.kind).where(SupportMessages.case_id == case.id).order_by(SupportMessages.id))).all()
    assert kinds == ["reply_in", "reply_in", "reply_out", "reply_in"]
    assert (await session.get(SupportCases, case.id, populate_existing=True)).status == "open"
    # Our reply answered the person's last mail.
    assert desk.outbox.sent[-1]["headers"]["In-Reply-To"].endswith("@example.org>")


async def test_a_mail_reopens_a_resolved_case_and_opens_a_new_one_after_close(client, session, desk, aws):
    await deliver(client, aws, a_mail())
    [case] = await cases_of(session)
    case.status, case.resolved_at = "resolved", NOW - timedelta(days=2)
    await session.commit()
    await deliver(client, aws, a_mail(subject=f"Re: [#{case.number}] Ticket missing", text="Not fixed"))
    assert (await session.get(SupportCases, case.id, populate_existing=True)).status == "open"
    case.status, case.closed_at = "closed", NOW
    await session.commit()
    r = await deliver(client, aws, a_mail(subject=f"Re: [#{case.number}] Ticket missing", text="Again"))
    assert r.json()["desk"] == {"support": "opened"}
    new = (await cases_of(session))[-1]
    assert new.parent_case_id == case.id and new.id != case.id


async def test_spam_is_closed_out_of_every_queue_and_a_virus_is_dropped(client, session, desk, aws):
    r = await deliver(client, aws, a_mail(subject="WIN NOW"), spam="FAIL")
    assert r.json()["desk"] == {"support": "spam"}
    [case] = await cases_of(session)
    assert case.status == "closed" and case.tags == ["spam"] and case.assignee_id is None
    listed = (await client.get(f"{API}/admin/support/cases?tab=all", headers=desk.h["sup"])).json()
    assert listed["total"] == 0 and listed["counts"]["unassigned"] == 0
    assert (await client.get(f"{API}/admin/support/cases?tab=spam", headers=desk.h["sup"])).json()["total"] == 1
    # Not spam after all: into the queue.
    r = await client.patch(f"{API}/admin/support/cases/{case.id}", json={"tags": []}, headers=desk.h["owner"])
    assert r.status_code == 200 and r.json()["status"] == "open"
    r = await deliver(client, aws, a_mail(subject="invoice"), virus="FAIL", key="v1")
    assert r.json()["desk"] == {"dropped": "virus"} and "v1" not in aws.read


async def test_our_own_address_is_never_a_case(client, session, desk, aws):
    r = await deliver(client, aws, a_mail(sender=("Support", SUPPORT)))
    assert r.json()["desk"] == {"support": "loop"} and await cases_of(session) == []


async def test_an_unreadable_bucket_is_503_and_nothing_is_kept(client, session, desk, aws):
    aws.unreachable = True
    r = await deliver(client, aws, a_mail())
    assert r.status_code == 503 and await cases_of(session) == []


# ─── sales@ ──────────────────────────────────────────────────────────────


async def test_a_bcc_to_sales_lands_on_the_contacts_lead_once(client, session, desk, aws):
    made = await client.post(f"{API}/admin/leads", json={"name": "Acme Foods", "type": "sponsor",
                                                         "contacts": [{"name": "Priya", "email": "priya@acme.example.org"}]}, headers=desk.h["sales"])
    lead_id = made.json()["id"]
    raw = mime(sender=("Sid", desk.sales.email), to="priya@acme.example.org", subject="Proposal", text="Attached our deck.")
    r = await deliver(client, aws, raw, recipients=(SALES,), key="s1")
    assert r.json()["desk"] == {"sales": "logged"}
    again = await deliver(client, aws, raw, recipients=(SALES,), key="s1")
    assert again.json()["desk"] == {"sales": "duplicate"}
    acts = (await session.scalars(select(LeadActivities).where(LeadActivities.lead_id == lead_id, LeadActivities.kind == "email"))).all()
    assert len(acts) == 1 and acts[0].details["direction"] == "out" and acts[0].body.startswith("Proposal")


async def test_mail_from_someone_new_to_sales_is_a_new_lead(client, session, desk, aws):
    raw = mime(sender=("Kiran", "kiran@hallsandco.example.com"), to=SALES, subject="Venue for your events", text="We host 500.")
    r = await deliver(client, aws, raw, recipients=(SALES,))
    assert r.json()["desk"] == {"sales": "new_lead"}
    lead = await session.scalar(select(Leads))
    assert lead.source == "inbound_email" and lead.owner_id == desk.sales.id and lead.email_domain == "hallsandco.example.com"
    assert await session.scalar(select(LeadContacts.email).where(LeadContacts.lead_id == lead.id)) == "kiran@hallsandco.example.com"
    assert await session.scalar(select(func.count()).select_from(FollowUps).where(FollowUps.lead_id == lead.id)) == 1


# ─── The sweep ───────────────────────────────────────────────────────────


def ist(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone(timedelta(hours=5, minutes=30))).astimezone(timezone.utc)


async def test_the_digest_goes_once_a_day_from_0800_ist_to_those_with_something(client, session, desk):
    await session.execute(ScheduledRuns.__table__.delete().where(ScheduledRuns.name == sweep.DIGEST))
    await session.commit()
    made = (await ask(client)).json()
    assert await sweep.digest(session, ist(12, 7, 59)) == 0
    sent_before = len(desk.outbox.sent)
    assert await sweep.digest(session, ist(12, 8, 1)) == 4  # two support, two sales members
    await jobs.settle()
    assert await sweep.digest(session, ist(12, 17, 0)) == 0
    digests = [m for m in desk.outbox.sent[sent_before:] if m["key"].startswith("support-digest:")]
    # Only Sam has something: the case is his.
    assert [m["to"] for m in digests] == [desk.sup.email] and f"#{made['number']}" in digests[0]["text"]
    assert await sweep.digest(session, ist(13, 8, 0)) == 4


async def test_follow_ups_three_days_overdue_are_flagged_to_the_owners_once(client, session, desk):
    lead = (await client.post(f"{API}/admin/leads", json={"name": "Hall One", "type": "venue"}, headers=desk.h["sales"])).json()
    await client.post(f"{API}/admin/follow-ups", json={"lead_id": lead["id"], "kind": "visit", "title": "Visit the hall",
                                                       "due_at": (NOW - timedelta(days=4)).isoformat()}, headers=desk.h["sales"])
    await client.post(f"{API}/admin/follow-ups", json={"lead_id": lead["id"], "kind": "call", "title": "Not yet late",
                                                       "due_at": (NOW - timedelta(days=1)).isoformat()}, headers=desk.h["sales"])
    before = len(desk.outbox.sent)
    assert await sweep.overdue(session) == 1
    await jobs.settle()
    notes = [m for m in desk.outbox.sent[before:] if m["key"].startswith("support-overdue:")]
    assert desk.owner.email in [m["to"] for m in notes] and "Visit the hall" in notes[0]["text"] and "Not yet late" not in notes[0]["text"]
    assert await sweep.overdue(session) == 0


async def test_waiting_cases_are_reminded_on_day_5_and_closed_on_day_7(client, session, desk):
    a = (await ask(client, email="w1@example.org", subject="One")).json()
    b = (await ask(client, email="w2@example.org", subject="Two")).json()
    for cid in (a["id"], b["id"]):
        await reply(client, cid, desk.h["sup"] if (await session.get(SupportCases, cid)).assignee_id == desk.sup.id else desk.h["sup2"])
    ca, cb = await session.get(SupportCases, a["id"]), await session.get(SupportCases, b["id"])
    ca.waiting_since, cb.waiting_since = NOW - timedelta(days=5, hours=2), NOW - timedelta(days=7, hours=1)
    await session.commit()
    before = len(desk.outbox.sent)
    out = await sweep.waiting(session)
    assert out == {"reminded": 1, "closed": 1}
    await jobs.settle()
    reminders = [m for m in desk.outbox.sent[before:] if m["key"].startswith("support-reminder:")]
    assert [m["to"] for m in reminders] == ["w1@example.org"] and reminders[0]["headers"].get("In-Reply-To")
    assert (await session.get(SupportCases, b["id"], populate_existing=True)).close_reason == "no_reply"
    assert await sweep.waiting(session) == {"reminded": 0, "closed": 0}


async def test_resolved_closes_after_7_days_and_retention_keeps_the_counts(client, session, desk):
    made = (await ask(client, email="r@example.org")).json()
    up = await client.post(f"{API}/support/uploads", files={"file": ("x.png", PNG, "image/png")})
    other = (await ask(client, email="r2@example.org", subject="With a file", attachments=[up.json()["key"]])).json()
    c1, c2 = await session.get(SupportCases, made["id"]), await session.get(SupportCases, other["id"])
    c1.status, c1.resolved_at = "resolved", NOW - timedelta(days=8)
    c2.status, c2.closed_at = "closed", NOW - timedelta(days=400)
    await session.commit()
    assert await sweep.resolved(session) == 1
    assert (await session.get(SupportCases, made["id"], populate_existing=True)).status == "closed"
    files_before = len(desk.files)
    assert await sweep.retention(session) == 1
    c2 = await session.get(SupportCases, other["id"], populate_existing=True)
    assert c2.purged_at is not None and c2.message_count == 1
    assert await session.scalar(select(func.count()).select_from(SupportMessages).where(SupportMessages.case_id == c2.id)) == 0
    assert len(desk.files) == files_before - 1
    assert await sweep.retention(session) == 0
    view = await client.post(f"{API}/admin/support/cases/{c2.id}/messages", json={"kind": "note", "body": "x"}, headers=desk.h["owner"])
    assert view.status_code == 409


async def test_loose_uploads_go_and_away_ends(client, session, desk):
    up = await client.post(f"{API}/support/uploads", files={"file": ("x.png", PNG, "image/png")})
    row = await session.scalar(select(SupportAttachments).where(SupportAttachments.upload_key == up.json()["key"]))
    row.created_at = NOW - timedelta(days=2)
    member = await session.scalar(select(AdminTeamMembers).where(AdminTeamMembers.user_id == desk.sup.id))
    member.away, member.away_until = True, NOW - timedelta(minutes=1)
    await session.commit()
    done = await sweep.sweep(session)
    assert done["uploads"] == 1 and done["back"] == 1 and "failed" not in done.values()
    assert (await session.get(AdminTeamMembers, member.id, populate_existing=True)).away is False


async def test_the_reply_job_waits_on_a_deferred_provider_and_records_a_refusal(client, session, desk):
    from app.core.email import transport

    made = (await ask(client)).json()
    desk.outbox.fail["error"] = transport.MailDeferred("later")
    r = await reply(client, made["id"], desk.h["sup"])
    msg = await session.get(SupportMessages, r.json()["id"], populate_existing=True)
    assert msg.delivery_status == "sending"
    desk.outbox.fail["error"] = RuntimeError("refused")
    await mail._reply_job({"message_id": msg.id})
    msg = await session.get(SupportMessages, msg.id, populate_existing=True)
    case = await session.get(SupportCases, made["id"], populate_existing=True)
    assert msg.delivery_status == "failed" and "not_delivered" in case.flags and case.status == "open"
    desk.outbox.fail.clear()
