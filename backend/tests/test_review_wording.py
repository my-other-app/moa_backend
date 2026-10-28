"""Review registrations (docs/APPROVALS.md §8-§9, contract review-wording):

- The six registration review emails are the club's library templates: listed
  with their key, edited (words only: always on, no timer, no audience),
  their tokens held to their stages, previewed, reset to MyOtherApp's default,
  never made on an event, copied or deleted.
- They go out in the club's words; what people type (a name, a decline
  note) is a value, escaped and never parsed as template text. An empty
  conditional paragraph leaves no gap.
- A confirmed person's ticket goes in the club's "Confirmed · ticket" email;
  anyone else's in the event's own invite.
- Pushes say "registration", never "place".
- Switching review on moves the waiting list into review: each person once,
  in their turn, told once; nobody twice; phone-only entries and standing
  offers left alone; the backfill does the same for events switched on
  before, and a second run does nothing.
- A join racing the switch is refused once the switch is written.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.api.approvals import service as approvals
from app.api.approvals.models import RegistrationRequests as R
from app.api.emails.models import EmailTemplates
from app.api.events.models import EventRegistrationsLink, Events
from app.api.ticketing.models import WaitlistEntries
from app.core import jobs
from app.core.mail.render import RenderInput, render_email
from app.core.mail.schema import EmailDesign
from tests.conftest import auth_header
from tests.test_approvals import _req, approve, ask, decline, pushed  # noqa: F401
from tests.test_ticketing import register

API = "/api/v1"
NOW = datetime.now(timezone.utc)
REVIEW_KEYS = {
    "request_received", "request_approved", "request_approved_pay", "request_declined", "request_lapsed", "request_unanswered",
}
FIELDS = ("name", "subject", "design", "attachments", "audience", "trigger", "channels", "enabled")


@pytest.fixture
def mail(monkeypatch):
    from app.core.email import email as email_mod

    sent = []
    monkeypatch.setattr(email_mod, "send_email", lambda **kw: sent.append(kw))
    return sent


async def _club_event(session, make_club, make_event, mint_token, *, tag: str, **values):
    owner, club = await make_club(name=f"{tag.title()} Club", email=f"{tag}-owner@example.com")
    values = {"event_datetime": NOW + timedelta(days=5), "requires_approval": True, **values}
    event = await make_event(club=club, created_by=owner, name=f"{tag.title()} Night", **values)
    await session.commit()
    return SimpleNamespace(owner=owner, club=club, event=event, h=auth_header(await mint_token(owner)))


@pytest_asyncio.fixture
async def shop(session, make_club, make_event, mint_token, mail):
    w = await _club_event(session, make_club, make_event, mint_token, tag="wording")
    w.mail = mail
    return w


async def _library(client, w) -> dict:
    r = await client.get(f"{API}/clubs/me/emails/overview", headers=w.h)
    assert r.status_code == 200, r.text
    return {t["stage"]: t for t in r.json()["templates"]}


def _body(t: dict, **changes) -> dict:
    return {**{k: t[k] for k in FIELDS}, **changes}


def _with_paragraph(t: dict, text: str) -> dict:
    design = dict(t["design"])
    design["blocks"] = [*design["blocks"], {"kind": "paragraph", "id": "p-extra", "text": text, "style": {}}]
    return design


async def _save(client, w, t: dict, **changes):
    return await client.put(f"{API}/clubs/me/emails/{t['id']}", json=_body(t, **changes), headers=w.h)


# ─── The library ─────────────────────────────────────────────────────────


async def test_the_library_holds_the_six_review_emails_for_the_club_to_edit(client, session, shop, make_club, mint_token):
    lib = await _library(client, shop)
    review = {s: t for s, t in lib.items() if s.startswith("REQUEST_")}
    assert {t["key"] for t in review.values()} == REVIEW_KEYS
    assert all(t["group"] == "Registration review" and t["when"] and t["eventId"] is None for t in review.values())
    received = review["REQUEST_RECEIVED"]
    assert (received["label"], received["subject"]) == ("Thanks for registering", "Thanks for registering for {{event_name}}")
    assert review["REQUEST_APPROVED"]["subject"] == "Your ticket for {{event_name}}"
    assert review["REQUEST_APPROVED"]["attachments"]["ticketPdf"] is True
    assert review["REQUEST_DECLINED"]["subject"] == "Your registration for {{event_name}}"
    assert lib["INVITE"].get("key") is None
    overview = (await client.get(f"{API}/clubs/me/emails/overview", headers=shop.h)).json()
    assert overview["automatic"] == [] and "REQUEST_RECEIVED" not in overview["attention"]["disabled_stages"]
    # Asked again: the same rows, once each.
    assert {t["id"] for t in (await _library(client, shop)).values()} == {t["id"] for t in lib.values()}
    rows = await session.scalar(
        select(func.count()).select_from(EmailTemplates).where(EmailTemplates.club_id == shop.club.id, EmailTemplates.event_id.is_(None))
    )
    assert rows == 10

    # The editor's tokens: the review ones say where they may go.
    ctx = (await client.get(f"{API}/clubs/me/emails/context", headers=shop.h)).json()
    tokens = {t["key"]: t for t in ctx["tokens"]}
    assert tokens["pay_url"]["stages"] == ["REQUEST_APPROVED_PAY"] and tokens["pay_url"]["group"] == "Registration review"
    assert "REQUEST_RECEIVED" in tokens["status_url"]["stages"] and "stages" not in tokens["event_name"]
    assert ctx["stages"]["REQUEST_UNANSWERED"]["label"] == "Closed before review"
    assert ctx["sampleValues"]["pay_amount"] == "₹499"

    # Saved: the words change; the rest stays as these emails need it.
    saved = await _save(
        client, shop, received, subject="Hello from {{org_name}}: {{event_name}}", enabled=False,
        trigger={"mode": "BEFORE_START", "minutes": 30}, attachments={"ticketPdf": True, "certificatePdf": True},
        audience={"who": "CHECKED_IN", "roles": []},
        design=_with_paragraph(received, "Cancel any time: {{cancel_url}}"),
    )
    assert saved.status_code == 200, saved.text
    out = saved.json()
    assert out["subject"] == "Hello from {{org_name}}: {{event_name}}" and out["version"] == 2
    assert (out["enabled"], out["trigger"]["mode"], out["audience"]["who"]) == (True, "MANUAL", "ALL")
    assert out["attachments"] == {"ticketPdf": False, "certificatePdf": False}
    again = (await client.get(f"{API}/clubs/me/emails/{received['id']}", headers=shop.h)).json()
    assert again["subject"] == out["subject"] and again["design"]["blocks"][-1]["text"] == "Cancel any time: {{cancel_url}}"

    # A token where it means nothing, or no token at all: refused, as any email's mistake is.
    wrong = await _save(client, shop, received, design=_with_paragraph(received, "Pay here: {{pay_url}}"))
    assert wrong.status_code == 422 and "pay_url" in wrong.json()["message"]
    invite_wrong = await _save(client, shop, lib["INVITE"], design=_with_paragraph(lib["INVITE"], "{{status_url}}"))
    assert invite_wrong.status_code == 422 and "status_url" in invite_wrong.json()["message"]
    unknown = await _save(client, shop, received, subject="Hi {{nonsense}}")
    assert unknown.status_code == 422

    # The preview fills in the review tokens with samples.
    preview = await client.post(
        f"{API}/clubs/me/emails/preview",
        json={"subject": "Pay {{pay_amount}}", "design": _with_paragraph(review["REQUEST_APPROVED_PAY"], "By {{pay_by}}")},
        headers=shop.h,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["subject"] == "Pay ₹499" and "By 3 Nov, 6:00 PM IST" in preview.json()["html"]

    # Reset: MyOtherApp's words again, a new version.
    reset = await client.post(f"{API}/clubs/me/emails/{received['id']}/reset", headers=shop.h)
    assert reset.status_code == 200, reset.text
    assert reset.json()["subject"] == "Thanks for registering for {{event_name}}" and reset.json()["version"] == 3
    custom = await client.post(f"{API}/clubs/me/emails", json=_body(lib["POST_EVENT"], name="Mine"), headers=shop.h)
    assert custom.status_code == 200, custom.text
    no_default = await client.post(f"{API}/clubs/me/emails/{custom.json()['id']}/reset", headers=shop.h)
    assert no_default.status_code == 422 and "no default" in no_default.json()["message"]

    # Library only, one each: never made on an event or in the library by hand, copied or deleted.
    on_event = await client.post(
        f"{API}/events/{shop.event.id}/emails", json={**_body(received), "stage": "REQUEST_RECEIVED"}, headers=shop.h,
    )
    assert on_event.status_code == 422 and "library" in on_event.json()["message"]
    by_hand = await client.post(f"{API}/clubs/me/emails", json={**_body(received), "stage": "REQUEST_DECLINED"}, headers=shop.h)
    assert by_hand.status_code == 422
    assert (await client.post(f"{API}/clubs/me/emails/{received['id']}/duplicate", headers=shop.h)).status_code == 422
    assert (await client.delete(f"{API}/clubs/me/emails/{received['id']}", headers=shop.h)).status_code == 422
    journey = (await client.get(f"{API}/events/{shop.event.id}/emails", headers=shop.h)).json()
    assert [s["stage"] for s in journey["journey"]] == ["INVITE", "PRE_EVENT", "REMINDER", "POST_EVENT"] and journey["custom"] == []

    # Another club's email is not that club's to read, save or reset.
    other, _club = await make_club(name="Other Wording Club", email="other-wording-owner@example.com")
    await session.commit()
    oh = auth_header(await mint_token(other))
    assert (await client.post(f"{API}/clubs/me/emails/{received['id']}/reset", headers=oh)).status_code == 404
    assert (await client.put(f"{API}/clubs/me/emails/{received['id']}", json=_body(received), headers=oh)).status_code == 404


# ─── Sending in the club's words ─────────────────────────────────────────


async def test_the_review_emails_go_out_in_the_clubs_words_and_escape_what_people_type(
    client, session, shop, make_user, mint_token, pushed,
):
    lib = await _library(client, shop)
    received = lib["REQUEST_RECEIVED"]
    edited = await _save(
        client, shop, received, subject="We have you, {{first_name}}",
        design=_with_paragraph(received, "Hello {{attendee_name}}: {{org_name}} will look at it."),
    )
    assert edited.status_code == 200, edited.text

    a = (await ask(client, shop.event, 801, full_name="<b>Bold</b> Asker")).json()["approval"]
    await jobs.settle()
    got = [m for m in shop.mail if m["recipients"] == ["asker801@example.com"]]
    assert len(got) == 1 and got[0]["subject"] == "We have you, <b>Bold</b>"
    html = got[0]["html"]
    assert "Hello &lt;b&gt;Bold&lt;/b&gt; Asker: Wording Club will look at it." in html and "<b>Bold</b>" not in html
    assert f"/requests/{a['token']}?withdraw=1" in html and "registered for Wording Night" in html

    # The decline note is the club's words as typed, never a template.
    user = await make_user(email="wording-app@example.com", username="wordingapp")
    await session.commit()
    told = (await ask(client, shop.event, 802, email="wording-app@example.com", headers=auth_header(await mint_token(user)))).json()["approval"]
    shop.mail.clear()
    note = "{{event_name}} <script>x</script>"
    assert (await decline(client, shop, ids=[told["id"]], note=note)).json()["declined"] == [told["id"]]
    await jobs.settle()
    sent = [m for m in shop.mail if m["recipients"] == ["wording-app@example.com"]]
    assert len(sent) == 1 and sent[0]["subject"] == "Your registration for Wording Night"
    assert "couldn't confirm your registration this time" in sent[0]["html"]
    assert "{{event_name}} &lt;script&gt;x&lt;/script&gt;" in sent[0]["html"] and "<script>" not in sent[0]["html"]
    assert [(t, d["type"]) for _u, t, d in pushed] == [("Your registration for Wording Night", "request_declined")]

    # Declined with no note: the note's paragraph leaves no trace.
    quiet = (await ask(client, shop.event, 803)).json()["approval"]
    shop.mail.clear()
    await decline(client, shop, ids=[quiet["id"]])
    await jobs.settle()
    html = [m for m in shop.mail if m["recipients"] == ["asker803@example.com"]][0]["html"]
    assert "A note from" not in html

    # Confirmed: the push says so in the new words.
    pushed.clear()
    again = await make_user(email="wording-yes@example.com", username="wordingyes")
    await session.commit()
    yes = (await ask(client, shop.event, 804, email="wording-yes@example.com", headers=auth_header(await mint_token(again)))).json()["approval"]
    assert (await approve(client, shop, ids=[yes["id"]])).json()["approved"]
    await jobs.settle()
    assert ("Wording Club confirmed your registration", "request_approved") in [(t, d["type"]) for _u, t, d in pushed]


def test_an_empty_conditional_paragraph_leaves_no_gap():
    from app.core.mail.starters import email_starter

    design = email_starter("REQUEST_DECLINED").design
    values = {"org_name": "Club", "event_name": "Night", "first_name": "A", "decline_note": ""}
    bare = render_email(RenderInput(subject="s", design=design, values=values, brand_primary="#123456")).html
    noted = render_email(RenderInput(subject="s", design=design, values={**values, "decline_note": "Full"}, brand_primary="#123456")).html
    assert "A note from" not in bare and "A note from Club: “Full”" in noted
    assert noted.count("<tr><td") == bare.count("<tr><td") + 1
    # Any other empty heading or paragraph is left out the same way.
    blank = EmailDesign.model_validate({"blocks": [{"kind": "paragraph", "id": "p1", "text": "{% if x %}x{% endif %}"}]})
    assert render_email(RenderInput(subject="s", design=blank, values={}, brand_primary="#123456")).html.count("font-size:16px") == 0


# ─── A confirmed person's ticket ─────────────────────────────────────────


def _fake_document():
    from app.api.events.tickets import Document

    async def document(session, reg):
        return Document(content=b"%PDF-1.4 fake", filename=f"ticket-{reg.ticket_id}.pdf", design_version=0)

    return document


async def test_a_confirmed_ticket_goes_in_the_clubs_confirmed_email(client, session, make_club, make_event, mint_token, monkeypatch, mail, pushed):
    from app.api.deliveries import service as deliveries
    from app.api.deliveries.models import Deliveries
    from app.api.emails import service as emails

    async def nothing(*_a, **_k):
        return 0

    monkeypatch.setattr(deliveries, "_run_due", nothing)
    monkeypatch.setattr(deliveries, "ticket_document", _fake_document())
    monkeypatch.setattr(emails, "ticket_document", _fake_document())
    w = await _club_event(session, make_club, make_event, mint_token, tag="ticketed")
    lib = await _library(client, w)
    edited = await _save(client, w, lib["REQUEST_APPROVED"], subject="Confirmed: {{event_name}}")
    assert edited.status_code == 200, edited.text

    rid = (await ask(client, w.event, 820)).json()["approval"]["id"]
    assert (await approve(client, w, ids=[rid])).json()["approved"]
    reg_id = (await _req(session, rid)).registration_id
    d = await session.scalar(select(Deliveries).where(Deliveries.registration_id == reg_id))
    mail.clear()
    await deliveries.deliver(session, d.id)
    got = [m for m in mail if m["recipients"] == ["asker820@example.com"]]
    assert len(got) == 1 and got[0]["subject"] == "Confirmed: Ticketed Night"
    assert "Your registration is confirmed." in got[0]["html"] and got[0]["attachments"]

    # Someone registered directly (an event of the same club without review) gets the event's invite.
    plain = await make_event(club=w.club, created_by=w.owner, name="Open Night", event_datetime=NOW + timedelta(days=6))
    await session.commit()
    assert (await register(client, plain, 821)).status_code == 200
    reg = await session.scalar(select(EventRegistrationsLink).where(EventRegistrationsLink.event_id == plain.id))
    d2 = await session.scalar(select(Deliveries).where(Deliveries.registration_id == reg.id))
    mail.clear()
    await deliveries.deliver(session, d2.id)
    got = [m for m in mail if m["recipients"] == ["guest821@example.com"]]
    assert len(got) == 1 and got[0]["subject"] == "Your ticket for Open Night" and "Your registration is confirmed." not in got[0]["html"]


# ─── The waiting list, once review goes on ───────────────────────────────


def _form(event) -> dict:
    return {
        "name": event.name, "event_datetime": event.event_datetime.isoformat(), "duration": "2",
        "has_fee": "false", "status": "published", "category_id": str(event.category_id), "max_participants": "1",
    }


async def _line(session, event_id) -> dict:
    rows = (await session.scalars(
        select(WaitlistEntries).where(WaitlistEntries.event_id == event_id).execution_options(populate_existing=True)
    )).all()
    return {(e.email or e.phone): e for e in rows}


async def _requests(session, event_id) -> dict:
    rows = (await session.scalars(select(R).where(R.event_id == event_id).execution_options(populate_existing=True))).all()
    return {r.email: r for r in rows}


async def test_switching_review_on_moves_the_waiting_list_into_review(client, session, make_club, make_event, mint_token, make_user, mail):
    w = await _club_event(session, make_club, make_event, mint_token, tag="lineup", requires_approval=False, max_participants=1)
    on = await client.put(f"{API}/events/{w.event.id}/ticketing/waitlist/settings", json={"enabled": True}, headers=w.h)
    assert on.status_code == 200, on.text
    assert (await register(client, w.event, 830)).status_code == 200
    # In line, in this order: a guest, an account, a phone only.
    first = await client.post(f"{API}/events/{w.event.id}/waitlist", json={"full_name": "Line Guest", "email": "line-guest@example.com"})
    assert first.status_code == 200, first.text
    member = await make_user(email="line-member@example.com", username="linemember")
    await session.commit()
    mh = auth_header(await mint_token(member))
    second = await client.post(f"{API}/events/{w.event.id}/waitlist", json={"full_name": "Line Member", "phone": "9876500001"}, headers=mh)
    assert second.status_code == 200, second.text
    phoned = await client.post(f"{API}/events/{w.event.id}/waitlist", json={"full_name": "Phone Only", "phone": "9876500002"})
    assert phoned.status_code == 200, phoned.text
    type_id = (await session.get(WaitlistEntries, uuid.UUID(first.json()["id"]))).ticket_type_id
    # And three the switch must not make a second registration of, or touch.
    session.add_all([
        WaitlistEntries(event_id=w.event.id, ticket_type_id=type_id, full_name="Has Ticket", email="guest830@example.com", status="waiting"),
        WaitlistEntries(event_id=w.event.id, ticket_type_id=type_id, full_name="Has Request", email="has-request@example.com", status="waiting"),
        WaitlistEntries(
            event_id=w.event.id, ticket_type_id=type_id, full_name="Offered One", email="offered@example.com", status="offered",
            offer_token=uuid.uuid4().hex, offered_at=NOW, offer_expires_at=NOW + timedelta(hours=12), offers_made=1,
        ),
        R(event_id=w.event.id, full_name="Has Request", email="has-request@example.com", ticket_type_id=type_id, status="pending",
          token=approvals.new_token(), submitted_at=NOW),
    ])
    await session.commit()
    joined_at = {e.email: e.created_at for e in (await _line(session, w.event.id)).values() if e.email}
    mail.clear()

    r = await client.put(f"{API}/events/update/{w.event.id}", data={**_form(w.event), "requires_approval": "true"}, headers=w.h)
    assert r.status_code == 200, r.text
    await jobs.settle()

    reqs = await _requests(session, w.event.id)
    line = await _line(session, w.event.id)
    # The guest and the account: under review, in their turn, as they were in line.
    for email in ("line-guest@example.com", "line-member@example.com"):
        req = reqs[email]
        assert (req.status, req.ticket_type_id) == ("pending", type_id)
    assert reqs["line-guest@example.com"].submitted_at == joined_at["line-guest@example.com"]
    assert reqs["line-member@example.com"].user_id == member.id and reqs["line-member@example.com"].phone == "9876500001"
    assert reqs["line-guest@example.com"].submitted_at < reqs["line-member@example.com"].submitted_at
    # Nobody twice: the ticket holder and the one under review already just leave the line.
    assert set(reqs) == {"line-guest@example.com", "line-member@example.com", "has-request@example.com"}
    assert {k: line[k].status for k in ("line-guest@example.com", "line-member@example.com", "guest830@example.com", "has-request@example.com")} == {
        k: "removed" for k in ("line-guest@example.com", "line-member@example.com", "guest830@example.com", "has-request@example.com")
    }
    # Left alone: the phone-only entry (no address to write to), and a standing offer.
    assert line["9876500002"].status == "waiting" and line["offered@example.com"].status == "offered"
    # Told once each, in the new words; nobody else.
    told = sorted((m["recipients"][0], m["subject"]) for m in mail)
    assert told == [
        ("line-guest@example.com", "Thanks for registering for Lineup Night"),
        ("line-member@example.com", "Thanks for registering for Lineup Night"),
    ]
    # The account sees it on the event page; the line shows nothing of theirs.
    info = (await client.get(f"{API}/events/info/{w.event.id}", headers=mh)).json()
    assert info["ticketing"]["viewer_request"]["status"] == "pending" and info["ticketing"]["viewer_waitlist"] == []
    queue = (await client.get(f"{API}/events/registration/{w.event.id}/requests", headers=w.h)).json()
    assert queue["counts"]["pending"] == 3

    # Off and on again: nothing new, nobody told twice.
    off = await client.put(f"{API}/events/update/{w.event.id}", data={**_form(w.event), "requires_approval": "false"}, headers=w.h)
    assert off.status_code == 200, off.text
    again = await client.put(f"{API}/events/update/{w.event.id}", data={**_form(w.event), "requires_approval": "true"}, headers=w.h)
    assert again.status_code == 200, again.text
    await jobs.settle()
    assert len(await _requests(session, w.event.id)) == 3 and len(mail) == 2


async def test_the_backfill_moves_lines_of_events_switched_on_before_once(session, make_club, make_event, mint_token, mail):
    import importlib

    script = importlib.import_module("scripts.waitlist_to_review")
    w = await _club_event(session, make_club, make_event, mint_token, tag="backfill", max_participants=1)
    over = await _club_event(session, make_club, make_event, mint_token, tag="overdone", event_datetime=NOW - timedelta(days=2))
    # (A dry run rolls back, which expires what the session loaded: ids, kept as plain numbers.)
    event_id, over_id = w.event.id, over.event.id
    types = {}
    for ev in (w.event, over.event):
        from app.api.ticketing import types as types_mod

        types[ev.id] = (await types_mod.ensure(session, ev))[0].id
    # Review was on before the release: their lines never moved.
    session.add_all([
        WaitlistEntries(event_id=event_id, ticket_type_id=types[event_id], full_name="Old One", email="old-one@example.com", status="waiting"),
        WaitlistEntries(event_id=event_id, ticket_type_id=types[event_id], full_name="Old Two", email="old-two@example.com", status="waiting"),
        WaitlistEntries(event_id=over_id, ticket_type_id=types[over_id], full_name="Too Late", email="too-late@example.com", status="waiting"),
    ])
    await session.commit()
    ids = await approvals.waiting_list_events(session)
    assert event_id in ids and over_id not in ids

    # The dry run says what would happen and changes nothing.
    dry = await script.one(session, event_id, apply=False)
    assert (dry["moved"], dry["kept_no_email"]) == (2, 0)
    assert await _requests(session, event_id) == {} and mail == []
    assert {e.status for e in (await _line(session, event_id)).values()} == {"waiting"}

    done = await script.one(session, event_id, apply=True)
    await jobs.settle()
    assert done["moved"] == 2 and set(await _requests(session, event_id)) == {"old-one@example.com", "old-two@example.com"}
    assert sorted(m["recipients"][0] for m in mail) == ["old-one@example.com", "old-two@example.com"]
    # Again: nothing left in line, nobody told twice.
    assert event_id not in await approvals.waiting_list_events(session)
    twice = await script.one(session, event_id, apply=True)
    await jobs.settle()
    assert twice["moved"] == 0 and len(mail) == 2
    # An event already over is left as it is.
    assert (await _line(session, over_id))["too-late@example.com"].status == "waiting"


async def test_joining_the_line_is_refused_once_review_is_on(client, session, make_club, make_event, mint_token, mail):
    w = await _club_event(session, make_club, make_event, mint_token, tag="racer", requires_approval=False, max_participants=1)
    assert (await client.put(f"{API}/events/{w.event.id}/ticketing/waitlist/settings", json={"enabled": True}, headers=w.h)).status_code == 200
    assert (await register(client, w.event, 840)).status_code == 200
    from app.api.ticketing import waitlist

    # The join read the event before the switch was written (it says review is off)...
    stale = await session.get(Events, w.event.id)
    from sqlalchemy import update

    # Written behind the loaded row's back, as another request's switch would be.
    await session.execute(
        update(Events).where(Events.id == w.event.id).values(requires_approval=True).execution_options(synchronize_session=False)
    )
    await session.commit()
    assert stale.requires_approval is False
    # ...and is refused when it reads it again, under the lock, before joining.
    with pytest.raises(Exception) as refused:
        await waitlist.join(session, stale, ticket_type_id=None, full_name="Racer", email="racer@example.com", phone=None)
    assert getattr(refused.value, "error_code", None) == "WAITLIST_OFF_ON_APPROVAL_EVENTS"
    assert "review every registration" in refused.value.message
    await session.rollback()
