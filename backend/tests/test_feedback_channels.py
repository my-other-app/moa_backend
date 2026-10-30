"""The feedback survey's channels (app/api/live/service.send_survey).

The survey used to be email only: every guest cost the club one of its
month's emails, even the ones whose phones have the app. Now the club says
which channels it allows -- push, WhatsApp, email -- and each guest is sent
on the cheapest one of them that can reach them, once, with which channel it
was (or why nothing went) kept on their feedback_sends row.

What is checked here: the default (push, then the email only for whoever the
push cannot reach), each other choice, a muted guest falling through to the
email, push alone leaving a reason on the rows it cannot reach, WhatsApp
going to a guest who asked for it, a Free club past its monthly emails, the
split the club's report shows, and the auto-send clock still sending at the
same minute as before.

FCM is faked (tests.test_live_notify's), Meta is a stand-in
(tests.test_whatsapp's `meta`), and mail is a list.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.api.checkins.models import CheckInVia, CheckIns
from app.api.clubs.models import ClubUsersLink
from app.api.live import notify, service
from app.api.live.models import FeedbackSends, FeedbackSurveys
from app.api.message_quota import service as quota
from app.api.message_quota.models import MessageUsage
from app.api.notifications.models import Notifications, NotificationSettings
from app.api.users.models import UserDeviceTokens
from app.config import settings
from app.core import after_commit, jobs, whatsapp
from tests.conftest import auth_header
from tests.test_club_billing import make_plan
from tests.test_live_notify import fcm  # noqa: F401  (a fixture)
from tests.test_whatsapp import meta  # noqa: F401  (a fixture)

START = datetime.now(timezone.utc) + timedelta(hours=1)
ENDS = START + timedelta(hours=3)
PUSH_BODY = "How was it? Tell us in a minute"


@pytest.fixture
async def world(client, session, make_club, make_event, make_user, make_registration, mint_token, monkeypatch, fcm):  # noqa: F811
    """Signal Society's Build Night, and five people who were there:

    - Asha: a phone with the app, and an address.
    - Bhuvan: a phone with the app, follows the club and muted it.
    - Chandra: no app, an address, and asked for WhatsApp with a number.
    - Divya: no app, an address, no WhatsApp.
    - Eshan: no app, no address, no number: nothing can reach him.
    """
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    for key, value in dict(LIVE_NOTIFY=True, LIVE_NOTIFY_GAP_SECONDS=180, LIVE_NOTIFY_PER_DAY=10,
                           PUSH_TOPICS=False, INBOX_SHARED=False).items():
        monkeypatch.setattr(settings, key, value)
    owner, club = await make_club(name="Signal Society", email="fbch-owner@example.com")
    event = await make_event(club=club, created_by=owner, name="Build Night", event_datetime=START, duration=3.0)
    people, regs = {}, {}
    for key, email, phone, wa in (
        ("asha", "fbch-asha@example.com", None, False),
        ("bhuvan", "fbch-bhuvan@example.com", None, False),
        ("chandra", "fbch-chandra@example.com", "98765 43210", True),
        ("divya", "fbch-divya@example.com", None, False),
        ("eshan", "", None, False),
    ):
        people[key] = await make_user(email=email or "fbch-eshan@example.com")
        regs[key] = await make_registration(
            event=event, user=people[key], full_name=key.title(), email=email, phone=phone,
            whatsapp_opt_in=wa, whatsapp_opt_in_at=START if wa else None,
        )
    for key in regs:
        session.add(CheckIns(event_id=event.id, registration_id=regs[key].id, via=CheckInVia.gate, checked_in_at=START, door_label="Gate"))
    # The app, on two phones only.
    session.add(UserDeviceTokens(user_id=people["asha"].id, fcm_token="tok-fbch-asha", platform="android"))
    session.add(UserDeviceTokens(user_id=people["bhuvan"].id, fcm_token="tok-fbch-bhuvan", platform="android"))
    session.add(ClubUsersLink(club_id=club.id, user_id=people["bhuvan"].id, is_following=True, is_muted=True))
    await session.commit()
    return SimpleNamespace(
        event=event, event_id=event.id, club=club, club_id=club.id, owner=owner,
        people=people, ids={k: u.id for k, u in people.items()}, regs=regs,
        h=auth_header(await mint_token(owner)), base=f"/api/v1/events/{event.id}/live",
    )


@pytest.fixture
def outbox(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(service.email_mod, "send_email", lambda **kw: sent.append(kw))
    return sent


async def _send(client, world, headers=None):
    r = await client.post(f"{world.base}/feedback/send", headers=headers or world.h)
    assert r.status_code == 200, r.text
    await after_commit.drain()
    await jobs.settle()
    return r.json()


async def _rows(session, world) -> dict:
    """Each guest's row, by name: (channel, delivered, skipped_reason)."""
    survey_id = await session.scalar(select(FeedbackSurveys.id).where(FeedbackSurveys.event_id == world.event_id))
    rows = (await session.execute(
        select(FeedbackSends.registration_id, FeedbackSends.channel, FeedbackSends.delivered_at, FeedbackSends.skipped_reason, FeedbackSends.mailed_at)
        .where(FeedbackSends.survey_id == survey_id)
    )).all()
    by_reg = {str(r.id): name for name, r in world.regs.items()}
    return {by_reg[str(reg_id)]: (channel, delivered is not None, reason, mailed is not None) for reg_id, channel, delivered, reason, mailed in rows}


async def _channels(client, world, channels):
    r = await client.put(f"{world.base}/feedback", json={"channels": channels}, headers=world.h)
    assert r.status_code == 200, r.text
    return r.json()


def _surveys(outbox) -> list:
    """The survey mails alone: the club's owner also gets the plan's
    allowance warnings down the same list."""
    return [kw for kw in outbox if str(kw.get("subject") or "").startswith("How was ")]


def _addresses(outbox) -> set:
    return {kw["recipients"][0] for kw in _surveys(outbox)}


# ─── the default: the push first, the email only for the rest ──────────────


async def test_by_default_whoever_has_the_app_is_pushed_and_only_the_rest_are_mailed(client, session, world, outbox, fcm):  # noqa: F811
    fb = await client.get(f"{world.base}/feedback", headers=world.h)
    assert fb.status_code == 200 and fb.json()["channels"] == ["push", "email"]
    assert fb.json()["by_channel"] == {"push": 0, "whatsapp": 0, "email": 0, "none": 0}

    out = await _send(client, world)
    # Everyone was there; Asha had the push, Bhuvan muted the club so his
    # email carried it, and the three without the app were mailed too.
    assert out["invited"] == 5 and out["sent"] == 4
    assert out["by_channel"] == {"push": 1, "whatsapp": 0, "email": 3, "none": 1}
    assert out["push_skipped_reason"] is None

    # One push, to Asha's phone alone -- not Bhuvan's, who muted the club.
    [message] = fcm.multicasts
    assert message.tokens == ["tok-fbch-asha"]
    assert message.notification.title == "Build Night" and message.notification.body == PUSH_BODY
    assert message.data["type"] == notify.FEEDBACK_TYPE and message.data["event_id"] == str(world.event_id)
    assert message.data["what"] == "feedback" and message.data["notification_id"] == message.data["push_id"]

    # Asha's inbox line, which opens where the push does.
    lines = list((await session.scalars(
        select(Notifications).where(Notifications.event_id == world.event_id, Notifications.type == notify.FEEDBACK_TYPE)
    )).all())
    assert [line.user_id for line in lines] == [world.ids["asha"]]
    assert lines[0].from_club_id == world.club_id and lines[0].description == PUSH_BODY

    # Asha's address was never touched: that is the whole point.
    assert _addresses(outbox) == {"fbch-bhuvan@example.com", "fbch-chandra@example.com", "fbch-divya@example.com"}

    rows = await _rows(session, world)
    assert rows["asha"] == ("push", True, None, False)
    assert rows["bhuvan"] == ("email", True, None, True)
    assert rows["divya"] == ("email", True, None, True)
    channel, delivered, reason, _mailed = rows["eshan"]
    assert (channel, delivered) == (None, False)
    assert reason.startswith("Nothing could reach this guest") and "no email address" in reason

    # Sending again reaches nobody already sent it, and pushes nobody twice.
    again = await _send(client, world)
    assert again["sent"] == 0 and len(fcm.multicasts) == 1 and len(_surveys(outbox)) == 3
    assert again["push_skipped_reason"] == notify.ALREADY_SENT


async def test_push_alone_sends_nothing_to_the_people_without_the_app_and_says_so(client, session, world, outbox, fcm):  # noqa: F811
    assert (await _channels(client, world, ["push"]))["channels"] == ["push"]
    out = await _send(client, world)
    assert out["sent"] == 1 and out["by_channel"] == {"push": 1, "whatsapp": 0, "email": 0, "none": 4}
    assert _surveys(outbox) == [] and len(fcm.multicasts) == 1

    rows = await _rows(session, world)
    assert rows["asha"] == ("push", True, None, False)
    for name in ("bhuvan", "chandra", "divya", "eshan"):
        channel, delivered, reason, _mailed = rows[name]
        assert (channel, delivered) == (None, False)
        assert "the app is not on their phone, or they muted the club" in reason

    # The club adds the email afterwards: the rows nothing went out on are
    # claimed again, and only those.
    await _channels(client, world, ["push", "email"])
    second = await _send(client, world)
    assert second["sent"] == 3 and second["by_channel"] == {"push": 1, "whatsapp": 0, "email": 3, "none": 1}
    assert _addresses(outbox) == {"fbch-bhuvan@example.com", "fbch-chandra@example.com", "fbch-divya@example.com"}
    # Asha, already pushed, is left exactly as she was.
    assert (await _rows(session, world))["asha"] == ("push", True, None, False)
    assert len(fcm.multicasts) == 1  # the survey is pushed once per event, ever


async def test_email_alone_behaves_exactly_as_it_did_before_the_channels(client, session, world, outbox, fcm):  # noqa: F811
    await _channels(client, world, ["email"])
    out = await _send(client, world)
    assert out["sent"] == 4 and out["by_channel"] == {"push": 0, "whatsapp": 0, "email": 4, "none": 1}
    assert fcm.multicasts == [] and out["push_skipped_reason"] is None
    # Asha is mailed too: the club asked for nothing else.
    assert "fbch-asha@example.com" in _addresses(outbox)
    assert (await _rows(session, world))["asha"] == ("email", True, None, True)


# ─── WhatsApp ──────────────────────────────────────────────────────────────


async def test_whatsapp_carries_it_for_the_guest_who_asked_for_it(client, session, world, outbox, fcm, meta):  # noqa: F811
    await _channels(client, world, ["push", "whatsapp", "email"])
    out = await _send(client, world)
    # Asha the push, Chandra the WhatsApp she asked for, Bhuvan and Divya the email.
    assert out["by_channel"] == {"push": 1, "whatsapp": 1, "email": 2, "none": 1}
    assert _addresses(outbox) == {"fbch-bhuvan@example.com", "fbch-divya@example.com"}

    [message] = meta["messages"]
    assert message["to"] == "919876543210" and message["template"]["name"] == "event_update"
    values = [p["text"] for p in message["template"]["components"][0]["parameters"]]
    assert values[:3] == ["Chandra", "Signal Society", "Build Night"]
    assert f"/feedback/{world.event_id}/{world.regs['chandra'].id}?k=" in values[3]

    rows = await _rows(session, world)
    assert rows["chandra"] == ("whatsapp", True, None, False)  # never `mailed_at`: no mail went
    # Counted against the club's monthly WhatsApp, not its emails.
    assert await session.scalar(
        select(MessageUsage.used).where(MessageUsage.club_id == world.club_id, MessageUsage.channel == quota.WHATSAPP, MessageUsage.period == quota.period_of())
    ) == 1

    # The job run again sends nothing: the row is already delivered.
    survey_id = await session.scalar(select(FeedbackSurveys.id).where(FeedbackSurveys.event_id == world.event_id))
    assert await service.whatsapp_survey(survey_id, world.regs["chandra"].id) is False
    assert len(meta["messages"]) == 1


async def test_without_whatsapp_keys_the_channel_is_skipped_for_the_email(client, session, world, outbox, fcm, monkeypatch):  # noqa: F811
    monkeypatch.setattr(whatsapp, "configured", lambda: False)
    await _channels(client, world, ["whatsapp", "email"])
    out = await _send(client, world)
    # Nothing can go by WhatsApp, so the email carries everyone who has one.
    assert out["by_channel"] == {"push": 0, "whatsapp": 0, "email": 4, "none": 1}
    assert "fbch-chandra@example.com" in _addresses(outbox)


# ─── the Free plan's monthly messages ──────────────────────────────────────


async def test_past_a_free_clubs_monthly_emails_the_row_says_why_and_no_mail_goes(client, session, world, outbox, fcm, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT", True)
    monkeypatch.setattr(settings, "PLAN_ENFORCEMENT_FROM", datetime(2020, 1, 1, tzinfo=timezone.utc), raising=False)
    await make_plan(session, key="free", limits={"emails_per_month": 1, "whatsapp_per_month": 0})
    await session.commit()

    out = await _send(client, world)
    # The push is free and still goes; of the three emails the plan pays for one.
    assert out["by_channel"]["push"] == 1
    rows = await _rows(session, world)
    assert rows["asha"] == ("push", True, None, False)
    mailed = [name for name in ("bhuvan", "chandra", "divya") if rows[name][1]]
    held = [name for name in ("bhuvan", "chandra", "divya") if not rows[name][1]]
    assert len(mailed) == 1 and len(held) == 2 and len(_surveys(outbox)) == 1
    for name in held:
        channel, delivered, reason, _mailed = rows[name]
        # Claimed for the email, held back by the plan, with the reason kept.
        assert (channel, delivered) == ("email", False) and reason
    assert await session.scalar(
        select(MessageUsage.used).where(MessageUsage.club_id == world.club_id, MessageUsage.channel == quota.EMAIL, MessageUsage.period == quota.period_of())
    ) == 1


# ─── what the club chooses, and what the report shows ──────────────────────


async def test_the_channels_are_checked_and_kept_in_cost_order(client, session, world):
    assert (await _channels(client, world, ["email", "push"]))["channels"] == ["push", "email"]
    assert (await _channels(client, world, ["whatsapp", "whatsapp"]))["channels"] == ["whatsapp"]
    # Left out of the body, they stay as they are.
    put = await client.put(f"{world.base}/feedback", json={"delay_minutes": 15}, headers=world.h)
    assert put.status_code == 200 and put.json()["channels"] == ["whatsapp"]
    assert (await client.put(f"{world.base}/feedback", json={"channels": []}, headers=world.h)).status_code == 422
    assert (await client.put(f"{world.base}/feedback", json={"channels": ["sms"]}, headers=world.h)).status_code == 422

    # A survey whose column was never written reads as the default.
    await session.execute(FeedbackSurveys.__table__.update().values(channels=[]))
    await session.commit()
    fb = await client.get(f"{world.base}/feedback", headers=world.h)
    assert fb.json()["channels"] == ["push", "email"]


async def test_the_clubs_report_shows_the_split_and_counts_an_older_mail_as_an_email(client, session, world, outbox, fcm):  # noqa: F811
    await _send(client, world)
    fb = await client.get(f"{world.base}/feedback", headers=world.h)
    assert fb.json()["by_channel"] == {"push": 1, "whatsapp": 0, "email": 3, "none": 1}

    # A row as it was written before the channels existed: no channel, a
    # `mailed_at`. It is the email it always was.
    await session.execute(
        FeedbackSends.__table__.update()
        .where(FeedbackSends.registration_id == world.regs["divya"].id)
        .values(channel=None, delivered_at=None)
    )
    await session.commit()
    fb = await client.get(f"{world.base}/feedback", headers=world.h)
    assert fb.json()["by_channel"] == {"push": 1, "whatsapp": 0, "email": 3, "none": 1}
    # And it is not mailed again: `mailed_at` still says it went.
    survey_id = await session.scalar(select(FeedbackSurveys.id).where(FeedbackSurveys.event_id == world.event_id))
    assert await service.mail_survey(survey_id, world.regs["divya"].id) is False
    assert len(_surveys(outbox)) == 3


# ─── the clock ─────────────────────────────────────────────────────────────


async def test_the_survey_still_sends_itself_at_the_same_minute_and_on_the_channels(client, session, world, outbox, fcm):  # noqa: F811
    # The club opening the page is what makes the survey (and its due_at).
    assert (await client.get(f"{world.base}/feedback", headers=world.h)).status_code == 200
    assert await service.sweep(session, now=ENDS + timedelta(minutes=59)) == 0
    assert await service.sweep(session, now=ENDS + timedelta(minutes=61)) == 1
    await after_commit.drain()
    await jobs.settle()
    assert await service.sweep(session, now=ENDS + timedelta(minutes=62)) == 0  # once

    assert len(fcm.multicasts) == 1 and fcm.multicasts[0].tokens == ["tok-fbch-asha"]
    assert _addresses(outbox) == {"fbch-bhuvan@example.com", "fbch-chandra@example.com", "fbch-divya@example.com"}
    assert await service.sends_by_channel(session, await session.scalar(select(FeedbackSurveys.id))) == {
        "push": 1, "whatsapp": 0, "email": 3, "none": 1,
    }


async def test_the_push_is_held_by_the_live_rooms_rate_limit_and_the_email_takes_over(client, session, world, outbox, fcm, monkeypatch):  # noqa: F811
    # A quiz push a moment ago: the survey's push is inside the gap.
    told = await notify.tell_room(session, world.event, "quiz")
    # The quiz tells everyone checked in with an account, devices or not.
    assert told["sent"] == 4 and told["skipped_reason"] is None
    out = await _send(client, world)
    assert out["push_skipped_reason"] == notify.RATE_LIMITED
    # Nobody is left without the survey: Asha falls through to her email.
    assert out["by_channel"] == {"push": 0, "whatsapp": 0, "email": 4, "none": 1}
    assert "fbch-asha@example.com" in _addresses(outbox)
    assert (await _rows(session, world))["asha"] == ("email", True, None, True)


async def test_with_the_room_switched_off_the_survey_still_goes_by_email(client, session, world, outbox, fcm, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "LIVE_NOTIFY", False)
    out = await _send(client, world)
    assert out["push_skipped_reason"] == notify.DISABLED
    assert fcm.multicasts == [] and out["by_channel"] == {"push": 0, "whatsapp": 0, "email": 4, "none": 1}


async def test_a_guest_who_turned_clubs_pushes_off_gets_the_email_instead(client, session, world, outbox, fcm):  # noqa: F811
    session.add(NotificationSettings(user_id=world.ids["asha"], clubs=False))
    await session.commit()
    out = await _send(client, world)
    # Her phone is the only one with the app, and she said no to her clubs:
    # there is nobody left to push to.
    assert fcm.multicasts == [] and out["push_skipped_reason"] == notify.NOBODY
    assert out["by_channel"] == {"push": 0, "whatsapp": 0, "email": 4, "none": 1}
    assert "fbch-asha@example.com" in _addresses(outbox)


async def test_one_job_per_guest_per_channel_and_no_second_one(client, session, world, outbox, fcm, meta):  # noqa: F811
    await _channels(client, world, ["push", "whatsapp", "email"])
    await _send(client, world)
    survey_id = await session.scalar(select(FeedbackSurveys.id).where(FeedbackSurveys.event_id == world.event_id))
    for name, job in ((service.SURVEY_MAIL, service.SURVEY_MAIL), (service.SURVEY_WHATSAPP, service.SURVEY_WHATSAPP)):
        keys = list((await session.scalars(select(jobs.Jobs.dedupe_key).where(jobs.Jobs.name == job))).all())
        assert len(keys) == len(set(keys)), name
    assert await session.scalar(select(func.count()).select_from(jobs.Jobs).where(jobs.Jobs.name == service.SURVEY_WHATSAPP)) == 1
    assert await session.scalar(select(func.count()).select_from(jobs.Jobs).where(jobs.Jobs.name == service.SURVEY_MAIL)) == 2
    # Nothing is pushed as a per-guest job: the push is one fan-out.
    assert await session.scalar(select(func.count()).select_from(jobs.Jobs).where(jobs.Jobs.name == notify.PUSH_JOB)) == 1
    assert (await service.sends_by_channel(session, survey_id)) == {"push": 1, "whatsapp": 1, "email": 2, "none": 1}
