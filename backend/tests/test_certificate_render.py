"""The certificate image endpoint, and the context both renders share.

The app shows a certificate beside a download button, and the two have to
be the same document. Rendering the PDF in a webview put Apple's PDF viewer
chrome on screen -- a page counter, a page frame, and the certificate shrunk
inside a sheet of white -- which no styling removes. The PNG has no viewer.

What matters here is that both endpoints build their payload the same way,
so the preview cannot drift from the download.
"""
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import auth_header


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    from app.core.email import email as email_mod

    monkeypatch.setattr(email_mod, "send_email_with_attachment", lambda **kwargs: None
    )


@pytest.fixture
async def attended(session, make_club, make_event, make_user, make_registration):
    owner, club = await make_club(name="Yukthi", email="cert@example.com")
    event = await make_event(
        club=club,
        created_by=owner,
        name="LaTeX Workshop",
        event_datetime=datetime.now(timezone.utc) - timedelta(days=3),
    )
    user = await make_user(email="attendee@example.com")
    registration = await make_registration(event=event, user=user)
    registration.is_attended = True
    await session.flush()
    return registration


async def test_the_two_renders_share_one_payload(session, attended):
    # Not a second implementation: one helper, used by both, so the preview
    # and the download cannot describe different things.
    from app.api.events.router import _certificate_render_context

    context = await _certificate_render_context(session, attended.ticket_id)

    assert context["email_payload"]["ticket_id"] == attended.ticket_id
    assert context["email_payload"]["event_name"] == "LaTeX Workshop"
    assert context["email_payload"]["organizer_name"] == "Yukthi"
    # The QR is embedded rather than linked, so the render needs no network.
    assert context["email_payload"]["qr_code"]
    assert "style" in context


async def test_a_registration_that_never_checked_in_has_no_certificate(
    session, make_club, make_event, make_user, make_registration,
):
    from app.api.events.router import _certificate_render_context
    from app.response import CustomHTTPException

    owner, club = await make_club(name="Club", email="nocert@example.com")
    event = await make_event(club=club, created_by=owner)
    user = await make_user(email="absent@example.com")
    registration = await make_registration(event=event, user=user)

    # The PDF endpoint already refused these; the image endpoint has to
    # refuse them too, or it would preview a certificate that cannot be
    # downloaded.
    with pytest.raises(CustomHTTPException):
        await _certificate_render_context(session, registration.ticket_id)


async def test_the_image_endpoint_refuses_a_non_attendee(
    client, make_club, make_event, make_user, make_registration,
):
    owner, club = await make_club(name="Club", email="img@example.com")
    event = await make_event(club=club, created_by=owner)
    user = await make_user(email="noshow@example.com")
    registration = await make_registration(event=event, user=user)

    response = await client.get(
        f"/api/v1/events/certificates/{registration.ticket_id}/image"
    )

    assert response.status_code == 403


async def test_the_pdf_endpoint_still_refuses_one_too(
    client, make_club, make_event, make_user, make_registration,
):
    # The PDF path was refactored onto the shared helper; this is the
    # behaviour that must not have changed in the process.
    owner, club = await make_club(name="Club", email="pdf@example.com")
    event = await make_event(club=club, created_by=owner)
    user = await make_user(email="noshow2@example.com")
    registration = await make_registration(event=event, user=user)

    response = await client.get(
        f"/api/v1/events/certificates/{registration.ticket_id}/pdf"
    )

    assert response.status_code == 403
