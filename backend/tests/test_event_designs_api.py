"""Designing a ticket or certificate on the event itself.

The club library is what an event starts from; the event's own page is
where it is designed. The behaviour that matters is the fork: reading a
design shows the club's, and saving one gives the event a copy, so the
club template can then be edited without reaching back into events whose
tickets are already out.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.api.certificate_templates.models import CertificateTemplates
from app.api.templates_audience import TemplateAudience
from app.api.ticket_templates.models import TicketTemplates
from tests.conftest import auth_header


@pytest.fixture
async def designing(session, make_club, make_event, mint_token):
    """A club owner, their event, and the headers to act as them."""
    owner, club = await make_club(name="Yukthi", email="designs@example.com")
    event = await make_event(
        club=club, created_by=owner, name="Hack Night",
        event_datetime=datetime.now(timezone.utc) + timedelta(days=9),
    )
    token = await mint_token(owner)
    await session.commit()
    return owner, club, event, auth_header(token)


async def _club_default(session, club_id, *, model=TicketTemplates, **config):
    template = model(
        club_id=club_id, is_default=True, name="Club default",
        config=config or {"bg_color": "#101014"},
    )
    session.add(template)
    await session.commit()
    return template


async def test_an_event_with_no_design_shows_the_club_default(
    client, session, designing
):
    _, club, event, headers = designing
    await _club_default(session, club.id, bg_color="#abcdef")

    response = await client.get(
        f"/api/v1/events/{event.id}/designs/ticket", headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["config"]["bg_color"] == "#abcdef"
    assert body["source"] == "club_default"
    # Nothing to reset yet, and the editor should not offer it.
    assert body["is_own"] is False


async def test_an_event_with_no_club_default_opens_on_the_system_design(
    client, designing
):
    _, _, event, headers = designing

    response = await client.get(
        f"/api/v1/events/{event.id}/designs/certificate", headers=headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "system"
    # A starting design, not an empty canvas.
    assert body["config"]


async def test_saving_forks_the_design_onto_the_event(
    client, session, designing
):
    _, club, event, headers = designing
    club_template = await _club_default(session, club.id, bg_color="#101014")

    saved = await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=headers,
        json={"config": {"bg_color": "#ff5a1f"}},
    )

    assert saved.status_code == 200
    assert saved.json()["source"] == "event_default"
    assert saved.json()["is_own"] is True

    # The club template is untouched: this is the whole point of forking.
    await session.refresh(club_template)
    assert club_template.config["bg_color"] == "#101014"


async def test_saving_twice_edits_the_same_copy(client, session, designing):
    """A second save must not fork again, or an event grows a design per edit."""
    _, club, event, headers = designing
    await _club_default(session, club.id)

    first = await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=headers, json={"config": {"bg_color": "#111111"}},
    )
    second = await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=headers, json={"config": {"bg_color": "#222222"}},
    )

    assert first.json()["template_id"] == second.json()["template_id"]
    assert second.json()["config"]["bg_color"] == "#222222"


async def test_the_fork_starts_as_what_was_on_screen(client, session, designing):
    """Opening an event and saving without changing anything changes nothing.

    The copy is taken from the design the organiser was already looking
    at, so "save" on an untouched editor is a no-op rather than a reset
    to the system default.
    """
    _, club, event, headers = designing
    await _club_default(session, club.id, bg_color="#abcdef", layers=[])

    shown = await client.get(
        f"/api/v1/events/{event.id}/designs/ticket", headers=headers
    )
    saved = await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=headers, json={"config": shown.json()["config"]},
    )

    assert saved.json()["config"] == shown.json()["config"]


async def test_a_role_variant_is_separate_from_the_event_default(
    client, session, designing
):
    _, club, event, headers = designing
    await _club_default(session, club.id)

    await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=headers, json={"config": {"bg_color": "#default0"}},
    )
    await client.put(
        f"/api/v1/events/{event.id}/designs/ticket?audience=speaker",
        headers=headers, json={"config": {"bg_color": "#speaker0"}},
    )

    default = await client.get(
        f"/api/v1/events/{event.id}/designs/ticket", headers=headers
    )
    speaker = await client.get(
        f"/api/v1/events/{event.id}/designs/ticket?audience=speaker",
        headers=headers,
    )

    assert default.json()["config"]["bg_color"] == "#default0"
    assert speaker.json()["config"]["bg_color"] == "#speaker0"
    assert speaker.json()["source"] == "event_role"


async def test_a_role_with_no_variant_falls_back_to_the_event_default(
    client, session, designing
):
    """One design for the whole event is the common case, and must stay easy."""
    _, club, event, headers = designing
    await _club_default(session, club.id)
    await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=headers, json={"config": {"bg_color": "#default0"}},
    )

    staff = await client.get(
        f"/api/v1/events/{event.id}/designs/ticket?audience=staff",
        headers=headers,
    )

    assert staff.json()["config"]["bg_color"] == "#default0"
    assert staff.json()["source"] == "event_default"
    # It is the event's design, but not the staff variant, so there is no
    # staff design to reset.
    assert staff.json()["is_own"] is False


async def test_resetting_returns_the_event_to_the_club_default(
    client, session, designing
):
    _, club, event, headers = designing
    await _club_default(session, club.id, bg_color="#abcdef")
    await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=headers, json={"config": {"bg_color": "#ff5a1f"}},
    )

    reset = await client.delete(
        f"/api/v1/events/{event.id}/designs/ticket", headers=headers
    )

    assert reset.status_code == 200
    assert reset.json()["config"]["bg_color"] == "#abcdef"
    assert reset.json()["source"] == "club_default"
    assert reset.json()["is_own"] is False


async def test_resetting_a_design_the_event_never_had_is_a_404(
    client, session, designing
):
    _, club, event, headers = designing
    await _club_default(session, club.id)

    reset = await client.delete(
        f"/api/v1/events/{event.id}/designs/ticket", headers=headers
    )

    assert reset.status_code == 404


async def test_tickets_and_certificates_are_designed_separately(
    client, session, designing
):
    _, club, event, headers = designing
    await _club_default(session, club.id, model=TicketTemplates)
    await _club_default(session, club.id, model=CertificateTemplates)

    await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=headers, json={"config": {"bg_color": "#ticket00"}},
    )

    certificate = await client.get(
        f"/api/v1/events/{event.id}/designs/certificate", headers=headers
    )

    assert certificate.json()["source"] == "club_default"
    assert certificate.json()["config"].get("bg_color") != "#ticket00"


async def test_another_club_cannot_read_this_events_design(
    client, session, designing, make_club, mint_token
):
    _, club, event, headers = designing
    await _club_default(session, club.id)
    other_owner, _ = await make_club(name="Other", email="other@example.com")
    other_token = await mint_token(other_owner)
    await session.commit()

    response = await client.get(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=auth_header(other_token),
    )

    assert response.status_code == 403


async def test_another_club_cannot_save_over_this_events_design(
    client, session, designing, make_club, mint_token
):
    _, club, event, headers = designing
    await _club_default(session, club.id)
    other_owner, _ = await make_club(name="Other", email="other2@example.com")
    other_token = await mint_token(other_owner)
    await session.commit()

    response = await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=auth_header(other_token),
        json={"config": {"bg_color": "#000000"}},
    )

    assert response.status_code == 403
    assert await session.scalar(
        TicketTemplates.__table__.select().where(
            TicketTemplates.event_id == event.id
        ).exists().select()
    ) is False


async def test_a_missing_event_is_a_404(client, designing):
    *_, headers = designing

    response = await client.get(
        "/api/v1/events/99999999/designs/ticket", headers=headers
    )

    assert response.status_code == 404


async def test_an_unknown_kind_is_rejected(client, designing):
    """ticket and certificate are the only two, and a typo must not 500."""
    *_, headers = designing

    response = await client.get(
        "/api/v1/events/1/designs/poster", headers=headers
    )

    assert response.status_code == 422


async def test_the_fork_is_not_offered_in_the_club_library(
    client, session, designing
):
    """An event's own design is not a club template and must not be listed.

    Otherwise a club with twenty events ends up with a library of twenty
    near-identical forks.
    """
    _, club, event, headers = designing
    await _club_default(session, club.id)
    await client.put(
        f"/api/v1/events/{event.id}/designs/ticket",
        headers=headers, json={"config": {"bg_color": "#ff5a1f"}},
    )

    library = await client.get("/api/v1/ticket-templates/list", headers=headers)

    assert library.status_code == 200
    assert [t["name"] for t in library.json()] == ["Club default"]
