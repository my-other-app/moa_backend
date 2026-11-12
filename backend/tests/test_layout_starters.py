"""What a custom design starts from, and the designs clubs saved from the old
starters: every one valid as it comes, and rendering with a name on it.

The designer's designs replaced the ticket, certificate and badge starters
(app/api/doc_presets). What the app still offers is the CR80 card
(card_cr80), a blank canvas for a custom ticket or certificate, and the
programme starters. The old ticket and certificate starters live on in
tests/legacy_layouts.py: they are what every custom design saved from them
is, and those must go on rendering as they did."""
import pytest

from app.core.layout.engine import layout_problems
from app.core.layout.renderer import RenderInput, pdf_text, render_pdf
from app.core.layout.starters import BADGE_STARTERS, CERTIFICATE_STARTERS, TICKET_STARTERS, blank_layout
from tests.legacy_layouts import CERTIFICATE_STARTERS as SAVED_CERTIFICATES
from tests.legacy_layouts import TICKET_STARTERS as SAVED_TICKETS
from tests.legacy_layouts import certificate_starter, ticket_starter

COLORS = {"primary": "#1db9a0", "secondary": "#aaaaaa", "ticketType": "#ff5a1f"}
VALUES = {"attendee_name": "Ananya Iyer", "event_name": "Builders Day", "event_date": "16 Sept 2026",
          "event_time": "09:00", "venue": "KTPO", "org_name": "Yukthi", "ticket_type": "VIP",
          "admission_no": "MOA-0041-KF26", "ticket_code": "MOA-7K3QF-X9PWM",
          "certificate_no": "CERT-0041-KF26", "issued_date": "18 Sept 2026",
          "event_day": "Wednesday", "event_end_time": "18:00", "seat": "",
          "first_name": "Ananya", "last_name": "Iyer", "organization": ""}


def test_the_old_starters_are_gone_but_the_cr80_card():
    assert TICKET_STARTERS == {} and CERTIFICATE_STARTERS == {}
    assert list(BADGE_STARTERS) == ["card_cr80"] and BADGE_STARTERS["card_cr80"]["label"] == "Card (CR80)"


@pytest.mark.parametrize("kind", ["ticket", "certificate"])
def test_the_blank_canvas_is_valid_and_prints_the_name_and_code(kind):
    s = blank_layout(kind)
    assert layout_problems(s["canvas"], s["layers"], s["page"], qr_required=kind == "ticket") == []
    pdf = render_pdf(RenderInput(title="b", canvas=s["canvas"], layers=s["layers"], page=s["page"],
                                 values=VALUES, qr_value="MOA-7K3QF-X9PWM", colors=COLORS))
    text = " ".join(pdf_text(pdf).split())
    assert "Ananya Iyer" in text
    assert ("MOA-7K3QF-X9PWM" if kind == "ticket" else "CERT-0041-KF26") in text


@pytest.mark.parametrize("kind", list(SAVED_TICKETS))
def test_every_saved_ticket_design_is_valid_as_it_was(kind):
    s = ticket_starter(kind)
    assert layout_problems(s["canvas"], s["layers"], s["page"]) == []


@pytest.mark.parametrize("kind", list(SAVED_CERTIFICATES))
def test_every_saved_certificate_design_is_valid_as_it_was(kind):
    s = certificate_starter(kind)
    assert layout_problems(s["canvas"], s["layers"], s["page"], qr_required=False) == []


@pytest.mark.parametrize("kind", list(SAVED_TICKETS))
def test_every_saved_ticket_design_prints_the_guests_name(kind):
    s = ticket_starter(kind)
    pdf = render_pdf(RenderInput(title="t", canvas=s["canvas"], layers=s["layers"], page=s["page"],
                                 values=VALUES, qr_value="MOA-7K3QF-X9PWM", colors=COLORS))
    text = pdf_text(pdf)
    assert "Ananya Iyer" in text
    # The code under the QR is the ticket code the gate takes, not the admission number.
    assert "MOA-7K3QF-X9PWM" in text and "MOA-0041-KF26" not in text


def test_the_seats_variant_shows_the_seat_instead_of_a_name():
    s = ticket_starter("classic", seats=True)
    pdf = render_pdf(RenderInput(title="t", canvas=s["canvas"], layers=s["layers"], page=s["page"],
                                 values={**VALUES, "seat": "A-014", "attendee_name": ""},
                                 qr_value="x", colors=COLORS))
    assert "A-014" in pdf_text(pdf)


def test_a_saved_certificate_design_prints_the_number_and_the_name():
    s = certificate_starter("classic")
    pdf = render_pdf(RenderInput(title="c", canvas=s["canvas"], layers=s["layers"], page=s["page"],
                                 values=VALUES, qr_value="certificate:CERT-0041-KF26", colors=COLORS))
    text = pdf_text(pdf)
    assert "Ananya Iyer" in text
    assert "CERT-0041-KF26" in text


# ─── ID cards ───────────────────────────────────────────────────────────────

from app.core.layout.model import PAGE_PRESETS, TicketLayoutFields  # noqa: E402, F811
from app.core.layout.starters import badge_starter, badge_starter_page  # noqa: E402


def test_the_cr80_card_is_valid_on_its_card_whatever_page_is_asked():
    for page in (None, PAGE_PRESETS["cr80_landscape"]["page"], PAGE_PRESETS["label_38x90"]["page"]):
        s = badge_starter("card_cr80", page=page)
        fields = TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"])
        assert fields.page == PAGE_PRESETS["cr80_portrait"]["page"]
        assert layout_problems(fields.canvas, fields.layers, fields.page, qr_required=True) == []
        assert any(l.kind == "qr" for l in fields.layers)
        assert any(l.kind == "image" and l.source == "guest_photo" for l in fields.layers)
        assert any(l.kind == "text" and "first_name" in l.text for l in fields.layers)
    assert badge_starter_page("card_cr80") == PAGE_PRESETS["cr80_portrait"]["page"]


def test_a_removed_card_starter_is_no_starter():
    for gone in ("classic", "lanyard", "minimal", "simple", "insert", "label_4x3", "label_4x6", "label_brother"):
        with pytest.raises(KeyError):
            badge_starter(gone)


from app.core.layout.starters import AGENDA_STARTERS, agenda_starter  # noqa: E402


@pytest.mark.parametrize("kind", list(AGENDA_STARTERS))
def test_every_agenda_starter_is_valid_and_lists_the_run_of_show_once(kind):
    s = agenda_starter(kind)
    fields = TicketLayoutFields(canvas=s["canvas"], layers=s["layers"], page=s["page"])
    assert layout_problems(fields.canvas, fields.layers, fields.page, qr_required=False) == []
    assert sum(1 for l in fields.layers if l.kind == "agenda") == 1
    assert any(l.kind == "text" and "event_name" in l.text for l in fields.layers)
    assert s["page"] == PAGE_PRESETS["a4_portrait"]["page"] if kind == "programme" else s["page"].height_mm is not None
