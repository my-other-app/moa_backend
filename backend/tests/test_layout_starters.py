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


# ─── ID cards ───────────────────────────────────────────────────────────────

from app.core.layout.model import PAGE_PRESETS, TicketLayoutFields  # noqa: E402, F811
from app.core.layout.starters import BADGE_STARTERS, badge_starter, badge_starter_page  # noqa: E402


@pytest.mark.parametrize("kind", list(BADGE_STARTERS))
def test_every_badge_starter_is_valid_as_it_comes(kind):
    for page in (None, PAGE_PRESETS["cr80_landscape"]["page"], PAGE_PRESETS["label_38x90"]["page"]):
        s = badge_starter(kind, page=page)
        fields = TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"])
        assert layout_problems(fields.canvas, fields.layers, fields.page, qr_required=True) == []
        assert any(l.kind == "qr" for l in fields.layers)
        assert any(l.kind == "text" and "first_name" in l.text for l in fields.layers)


def test_badge_starters_follow_the_page_the_designer_is_on():
    assert badge_starter_page("classic") == PAGE_PRESETS["cr80_portrait"]["page"]
    assert badge_starter_page("insert", PAGE_PRESETS["cr80_landscape"]["page"]) == PAGE_PRESETS["cr80_landscape"]["page"]
    assert badge_starter_page("insert", PAGE_PRESETS["ticket"]["page"]) == PAGE_PRESETS["badge_87x130"]["page"]
    assert badge_starter_page("label_brother", PAGE_PRESETS["label_29x90"]["page"]) == PAGE_PRESETS["label_29x90"]["page"]
    assert badge_starter_page("label_brother", None) == PAGE_PRESETS["label_62x90"]["page"]
    assert badge_starter_page("label_4x6") == PAGE_PRESETS["lanyard_4x6"]["page"]


from app.core.layout.starters import AGENDA_STARTERS, agenda_starter  # noqa: E402


@pytest.mark.parametrize("kind", list(AGENDA_STARTERS))
def test_every_agenda_starter_is_valid_and_lists_the_run_of_show_once(kind):
    s = agenda_starter(kind)
    fields = TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"])
    assert layout_problems(fields.canvas, fields.layers, fields.page, qr_required=False) == []
    assert sum(1 for l in fields.layers if l.kind == "agenda") == 1
    assert any(l.kind == "text" and "event_name" in l.text for l in fields.layers)
    assert s["page"] == PAGE_PRESETS["a4_portrait"]["page"] if kind == "programme" else s["page"].height_mm is not None
