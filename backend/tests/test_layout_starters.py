"""Every starter is valid as it comes, and renders with a name on it."""
import pytest

from app.core.layout.engine import layout_problems
from app.core.layout.renderer import RenderInput, pdf_text, render_pdf
from app.core.layout.starters import CERTIFICATE_STARTERS, TICKET_STARTERS, certificate_starter, ticket_starter

COLORS = {"primary": "#1db9a0", "secondary": "#aaaaaa", "ticketType": "#ff5a1f"}
VALUES = {"attendee_name": "Ananya Iyer", "event_name": "Builders Day", "event_date": "16 Sept 2026",
          "event_time": "09:00", "venue": "KTPO", "org_name": "Yukthi", "ticket_type": "VIP",
          "admission_no": "MOA-0041-KF26", "certificate_no": "CERT-0041-KF26", "issued_date": "18 Sept 2026",
          "event_day": "Wednesday", "event_end_time": "18:00", "seat": ""}


@pytest.mark.parametrize("kind", list(TICKET_STARTERS))
def test_every_ticket_starter_is_valid_as_it_comes(kind):
    s = ticket_starter(kind)
    assert layout_problems(s["canvas"], s["layers"], s["page"]) == []


@pytest.mark.parametrize("kind", list(CERTIFICATE_STARTERS))
def test_every_certificate_starter_is_valid_as_it_comes(kind):
    s = certificate_starter(kind)
    assert layout_problems(s["canvas"], s["layers"], s["page"], qr_required=False) == []


@pytest.mark.parametrize("kind", list(TICKET_STARTERS))
def test_every_ticket_starter_prints_the_guests_name(kind):
    s = ticket_starter(kind)
    pdf = render_pdf(RenderInput(title="t", canvas=s["canvas"], layers=s["layers"], page=s["page"],
                                 values=VALUES, qr_value="MOA-0041-KF26", colors=COLORS))
    text = pdf_text(pdf)
    assert "Ananya Iyer" in text
    assert "MOA-0041-KF26" in text


def test_the_seats_variant_shows_the_seat_instead_of_a_name():
    s = ticket_starter("classic", seats=True)
    pdf = render_pdf(RenderInput(title="t", canvas=s["canvas"], layers=s["layers"], page=s["page"],
                                 values={**VALUES, "seat": "A-014", "attendee_name": ""},
                                 qr_value="x", colors=COLORS))
    assert "A-014" in pdf_text(pdf)


def test_a_certificate_starter_prints_the_number_and_the_name():
    s = certificate_starter("classic")
    pdf = render_pdf(RenderInput(title="c", canvas=s["canvas"], layers=s["layers"], page=s["page"],
                                 values=VALUES, qr_value="certificate:CERT-0041-KF26", colors=COLORS))
    text = pdf_text(pdf)
    assert "Ananya Iyer" in text
    assert "CERT-0041-KF26" in text
