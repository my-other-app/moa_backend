"""Which design a document is rendered from.

Templates used to belong to a club and be chosen per event, so two events
sharing one could not differ -- editing either changed both. An event now
forks its own copy on first edit, keyed by the role it is for.

The order, most specific first: this event's design for this role, this
event's default, the club's default, the system default.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.templates_audience import RegistrationRole, TemplateAudience
from app.api.templates_resolve import fork_for_event, resolve_template
from app.api.ticket_templates.models import TicketTemplates


@pytest.fixture
async def club_and_event(session, make_club, make_event):
    owner, club = await make_club(name="Yukthi", email="tpl@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Hack Night",
        event_datetime=datetime.now(timezone.utc) + timedelta(days=5),
    )
    return club, event


async def _template(session, **kwargs):
    kwargs.setdefault("name", "T")
    kwargs.setdefault("config", {})
    template = TicketTemplates(**kwargs)
    session.add(template)
    await session.flush()
    return template


async def test_the_club_default_is_used_when_the_event_has_none(
    session, club_and_event,
):
    club, event = club_and_event
    club_default = await _template(
        session, club_id=club.id, is_default=True, name="Club default"
    )

    found = await resolve_template(
        session, TicketTemplates, event_id=event.id, club_id=club.id
    )

    assert found.id == club_default.id


async def test_an_event_design_beats_the_club_default(session, club_and_event):
    club, event = club_and_event
    await _template(session, club_id=club.id, is_default=True, name="Club")
    own = await _template(
        session, event_id=event.id, audience=TemplateAudience.event_default,
        name="Event",
    )

    found = await resolve_template(
        session, TicketTemplates, event_id=event.id, club_id=club.id
    )

    assert found.id == own.id


async def test_a_role_variant_beats_the_event_default(session, club_and_event):
    club, event = club_and_event
    await _template(
        session, event_id=event.id, audience=TemplateAudience.event_default,
        name="Event default",
    )
    speaker = await _template(
        session, event_id=event.id, audience=TemplateAudience.speaker,
        name="Speaker",
    )

    found = await resolve_template(
        session, TicketTemplates, event_id=event.id, club_id=club.id,
        role=RegistrationRole.speaker,
    )

    assert found.id == speaker.id


async def test_a_role_with_no_variant_falls_back_to_the_event_default(
    session, club_and_event,
):
    # One design for the whole event is the normal case: every role lands on
    # the single event_default row.
    club, event = club_and_event
    event_default = await _template(
        session, event_id=event.id, audience=TemplateAudience.event_default,
        name="Event default",
    )
    await _template(
        session, event_id=event.id, audience=TemplateAudience.speaker,
        name="Speaker",
    )

    found = await resolve_template(
        session, TicketTemplates, event_id=event.id, club_id=club.id,
        role=RegistrationRole.staff,
    )

    assert found.id == event_default.id


async def test_nothing_anywhere_resolves_to_none(session, club_and_event):
    club, event = club_and_event

    found = await resolve_template(
        session, TicketTemplates, event_id=event.id, club_id=club.id
    )

    assert found is None


async def test_one_event_does_not_see_another_events_design(
    session, club_and_event,
):
    club, event = club_and_event
    from app.api.events.models import Events
    second = Events(
        slug="second-event", name="Second", images=[],
        event_datetime=datetime.now(timezone.utc) + timedelta(days=9),
        duration=2.0, club_id=club.id, category_id=event.category_id,
    )
    session.add(second)
    await session.flush()

    await _template(
        session, event_id=event.id, audience=TemplateAudience.event_default,
        name="First only",
    )

    found = await resolve_template(
        session, TicketTemplates, event_id=second.id, club_id=club.id
    )

    # The point of forking: designing one event leaves the other alone.
    assert found is None


async def test_forking_copies_what_the_event_was_already_using(
    session, club_and_event,
):
    club, event = club_and_event
    await _template(
        session, club_id=club.id, is_default=True, name="Club",
        config={"canvas": {"width": 1560, "height": 690}, "layers": [{"x": 10}]},
    )

    fork = await fork_for_event(
        session, TicketTemplates, event_id=event.id, club_id=club.id,
        audience=TemplateAudience.event_default, name="Hack Night ticket",
    )

    # Editing an event starts from what was on screen, so the first edit
    # changes nothing until something is actually changed.
    assert fork.event_id == event.id
    assert fork.club_id is None
    assert fork.config == {"canvas": {"width": 1560, "height": 690},
                           "layers": [{"x": 10}]}


async def test_forking_twice_returns_the_same_row(session, club_and_event):
    club, event = club_and_event
    await _template(session, club_id=club.id, is_default=True, name="Club")

    first = await fork_for_event(
        session, TicketTemplates, event_id=event.id, club_id=club.id,
        audience=TemplateAudience.event_default, name="X",
    )
    second = await fork_for_event(
        session, TicketTemplates, event_id=event.id, club_id=club.id,
        audience=TemplateAudience.event_default, name="X",
    )

    assert first.id == second.id


async def test_editing_the_club_template_no_longer_reaches_a_forked_event(
    session, club_and_event,
):
    club, event = club_and_event
    club_template = await _template(
        session, club_id=club.id, is_default=True, name="Club",
        config={"layers": []},
    )
    fork = await fork_for_event(
        session, TicketTemplates, event_id=event.id, club_id=club.id,
        audience=TemplateAudience.event_default, name="Event",
    )

    club_template.config = {"layers": [{"x": 999}]}
    await session.flush()

    found = await resolve_template(
        session, TicketTemplates, event_id=event.id, club_id=club.id
    )

    assert found.id == fork.id
    assert found.config == {"layers": []}
