"""The feedback survey's link on the event page (GET /events/info/{id},
`feedback_url`), for the app's own survey screen: the guest's own link,
the very one the email sends, once they were checked in and the survey is
open -- and read in the statement that already reads their registration."""
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from app.api.checkins.models import CheckInVia, CheckIns
from app.api.closeout.models import EventStatus
from app.api.live import service as live
from app.config import settings
from tests.conftest import auth_header

API = "/api/v1"


async def _check_in(session, event, reg):
    """As the door admits a guest (checkins.service.admit): the row, and the registration's mirror of it."""
    session.add(CheckIns(event_id=event.id, registration_id=reg.id, via=CheckInVia.gate, checked_in_at=event.event_datetime, door_label="Gate"))
    reg.is_attended = True
    reg.attended_on = event.event_datetime


@pytest.fixture
async def world(session, make_club, make_event, make_user, make_registration, mint_token):
    owner, club = await make_club(name="Feedback Society", email="fb-club@example.com")
    # The clock as this test starts, not as the module was imported: the
    # tests below count in minutes from it ("due in half an hour"), and a
    # module-wide time went stale half an hour into the full suite.
    now = datetime.now(timezone.utc)
    # Over two hours ago: past the end and the survey's default hour after it.
    event = await make_event(club=club, created_by=owner, name="Demo Night", event_datetime=now - timedelta(hours=5), duration=3.0)
    was, other, missed, stranger = [await make_user(email=f"fb-{n}@example.com") for n in ("was", "other", "missed", "stranger")]
    r_was = await make_registration(event=event, user=was)
    r_other = await make_registration(event=event, user=other)
    r_missed = await make_registration(event=event, user=missed)
    for reg in (r_was, r_other):
        await _check_in(session, event, reg)
    await session.commit()
    h = {u: auth_header(await mint_token(user)) for u, user in (("was", was), ("other", other), ("missed", missed), ("stranger", stranger), ("owner", owner))}
    return dict(event=event, club=club, owner=owner, was=was, r_was=r_was, r_other=r_other, r_missed=r_missed, h=h, now=now)


async def _link(client, event, headers):
    r = await client.get(f"{API}/events/info/{event.id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["feedback_url"]


def _answer_url(link: str) -> str:
    """The API's side of a survey link: /feedback/{event}/{registration}?k= on the site."""
    parts = urlsplit(link)
    _, _, event_id, reg_id = parts.path.split("/")
    return f"{API}/events/{event_id}/feedback/{reg_id}?k={parse_qs(parts.query)['k'][0]}"


async def test_whoever_was_there_gets_their_own_link_and_it_answers_the_survey(client, session, world):
    event = world["event"]
    await live.survey_of(session, event)

    link = await _link(client, event, world["h"]["was"])
    # The email's own link, by the function that makes it.
    assert link == live.survey_link(event, world["r_was"])
    assert link.startswith(f"{settings.PUBLIC_SITE_URL.rstrip('/')}/feedback/{event.id}/{world['r_was'].id}?k=")
    # Each guest's is theirs alone.
    theirs = await _link(client, event, world["h"]["other"])
    assert theirs == live.survey_link(event, world["r_other"]) and theirs != link

    # It verifies with the survey's own endpoint: the form, then an answer.
    url = _answer_url(link)
    form = await client.get(url)
    assert form.status_code == 200 and form.json()["submitted"] is False and form.json()["event_name"] == "Demo Night"
    sent = await client.post(url, json={"answers": {"overall": 5, "recommend": 9, "change": "More demos."}})
    assert sent.status_code == 200 and sent.json() == {"submitted": True}, sent.text
    assert (await client.get(url)).json()["submitted"] is True
    # Answered or not, the link stays (the emailed one never expires either).
    assert await _link(client, event, world["h"]["was"]) == link


async def test_no_link_for_anyone_but_a_guest_who_was_there(client, session, world):
    event = world["event"]
    await live.survey_of(session, event)
    # Registered but never checked in; signed out; not registered; the club itself.
    for headers in (world["h"]["missed"], {}, world["h"]["stranger"], world["h"]["owner"]):
        assert await _link(client, event, headers) is None
    # A token that does not verify is a visitor's page.
    assert await _link(client, event, auth_header("not-a-real-token")) is None


async def test_no_link_without_a_survey_or_before_it_opens(client, session, world, make_event, make_registration):
    event, h = world["event"], world["h"]["was"]
    # The event has no survey: the club never set one up, so none goes out.
    assert await _link(client, event, h) is None

    # Set up, but switched off and never sent.
    survey = await live.survey_of(session, event)
    survey.auto_send = False
    await session.commit()
    assert await _link(client, event, h) is None
    # Switched on: it opens at the end plus the delay, as the sweep sends it.
    survey.auto_send, survey.delay_minutes = True, 150
    await session.commit()
    assert await _link(client, event, h) is None  # ended 2 hours ago; due in half an hour
    survey.delay_minutes = 90
    await session.commit()
    assert await _link(client, event, h) == live.survey_link(event, world["r_was"])

    # Still on: none during the event, until the club sends it by hand --
    # then the guests checked in hold the emailed link, and the page shows it.
    tonight = await make_event(club=world["club"], created_by=world["owner"], name="Tonight", event_datetime=world["now"] - timedelta(hours=1), duration=3.0)
    mine = await make_registration(event=tonight, user=world["was"])
    await _check_in(session, tonight, mine)
    await session.commit()
    await live.survey_of(session, tonight)
    assert await _link(client, tonight, h) is None
    await live.send_survey(session, tonight)
    assert await _link(client, tonight, h) == live.survey_link(tonight, mine)

    # A draft's survey page is a 404, so a draft's link is not handed out.
    event.lifecycle = EventStatus.draft
    await session.commit()
    assert await _link(client, event, h) is None


async def test_the_link_costs_no_query_of_its_own(client, session, world, statements):
    """The survey is read beside the registration, in the statement the page
    already makes: one statement mentions feedback_surveys, with or without
    a survey, and none at all for a visitor signed out."""
    event, h = world["event"], world["h"]["was"]

    def survey_reads():
        return [s for s in statements if "feedback_surveys" in s]

    def registration_reads():
        return [s for s in statements if "FROM event_registrations_link" in s]

    await _link(client, event, h)
    without = len(registration_reads())
    assert len(survey_reads()) == 1 and survey_reads()[0] in registration_reads()

    await live.survey_of(session, event)
    statements.clear()
    assert await _link(client, event, h) is not None
    assert len(survey_reads()) == 1 and survey_reads()[0] in registration_reads()
    assert len(registration_reads()) == without

    statements.clear()
    assert await _link(client, event, {}) is None
    assert survey_reads() == []


@pytest.fixture
def statements(session):
    from sqlalchemy import event as sa_event

    seen: list[str] = []
    conn = session.bind.sync_connection

    def before(_conn, _cursor, statement, *_args):
        seen.append(statement)

    sa_event.listen(conn, "before_cursor_execute", before)
    yield seen
    sa_event.remove(conn, "before_cursor_execute", before)
