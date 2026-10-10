"""The renderer draws what the layout says, and the PNG is the PDF.

The assertion that matters is the last one: the attendee's name is in the
PDF's text. wkhtmltopdf never let a test say that; pdfium does.
"""
import pytest

from app.core.layout.engine import layout_problems
from app.core.layout.fonts import measure_for
from app.core.layout.model import (
    ImageLayer,
    PageSpec,
    QrLayer,
    ShapeLayer,
    TextLayer,
    TicketCanvas,
    TicketLayoutFields,
    min_qr_size,
)
from app.core.layout.renderer import RenderInput, pdf_text, pdf_to_png, render_pdf

CANVAS = TicketCanvas(width=1560, height=690, background="#101014")
PAGE = PageSpec(width_mm=203.2, height_mm=None)
COLORS = {"primary": "#1db9a0", "secondary": "#aaaaaa", "ticketType": "#ff5a1f"}


def _layers():
    return [
        ShapeLayer(id="band", name="Band", x=0, y=600, width=1560, height=90, fill="brand.primary"),
        TextLayer(id="name", name="Guest name", x=80, y=120, text="{{attendee_name}}", width=900, size=72, weight=700, color="#FFFFFF"),
        TextLayer(id="when", name="When", x=80, y=240, text="{{event_date}} · {{venue}}", width=900, size=32, color="#DDDDDD"),
        TextLayer(id="code", name="Code", x=80, y=520, text="{{admission_no | upper}}", width=600, size=28, font="mono", weight=700, color="#FFFFFF"),
        QrLayer(id="qr", name="QR", x=1200, y=160, size=320),
        ImageLayer(id="photo", name="Photo", x=1060, y=520, width=100, height=100, source="guest_photo", radius=50),
    ]


def _input(**over):
    base = dict(
        title="Ticket", canvas=CANVAS, layers=_layers(), page=PAGE,
        values={"attendee_name": "Lakshminarayanan Venkataraman", "event_date": "20 Nov 2026",
                "venue": "Innovation Hall", "admission_no": "moa-0041-kf26"},
        qr_value="MOA-0041-KF26", colors=COLORS,
    )
    base.update(over)
    return RenderInput(**base)


def test_renders_a_pdf_the_size_of_the_ticket():
    pdf = render_pdf(_input())

    assert pdf.startswith(b"%PDF")
    # 8 inches wide; the height follows the canvas's shape.
    import pypdfium2 as pdfium
    page = pdfium.PdfDocument(pdf)[0]
    assert page.get_width() == pytest.approx(576, abs=0.5)
    assert page.get_height() == pytest.approx(576 * 690 / 1560, abs=0.5)


def test_the_name_is_actually_in_the_pdf():
    text = pdf_text(render_pdf(_input()))

    assert "Lakshminarayanan Venkataraman" in text
    assert "20 Nov 2026 · Innovation Hall" in text
    # The filter ran: upper-cased on the way in.
    assert "MOA-0041-KF26" in text


def test_a_hidden_layer_does_not_print():
    layers = _layers()
    layers[1].hidden = True

    assert "Lakshminarayanan" not in pdf_text(render_pdf(_input(layers=layers)))


def test_the_png_is_the_pdf_rendered():
    png = pdf_to_png(render_pdf(_input()), width_px=780)

    assert png.startswith(b"\x89PNG")
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(png))
    assert im.width == 780
    assert im.height == pytest.approx(780 * 690 / 1560, abs=1)


def test_a_fixed_page_is_filled_and_an_a4_certificate_comes_out_a4():
    canvas = TicketCanvas(width=1754, height=1240, background="#FFFFFF")
    layers = [TextLayer(id="t", name="t", x=100, y=100, text="{{attendee_name}}", width=1500, size=60)]
    pdf = render_pdf(_input(canvas=canvas, layers=layers, page=PageSpec(width_mm=297, height_mm=210)))

    import pypdfium2 as pdfium
    page = pdfium.PdfDocument(pdf)[0]
    assert page.get_width() == pytest.approx(297 / 25.4 * 72, abs=0.5)
    assert page.get_height() == pytest.approx(210 / 25.4 * 72, abs=0.5)


def test_a_missing_font_glyph_falls_back_to_inter_rather_than_failing():
    # Bebas has no Malayalam; the name must still print.
    layers = [TextLayer(id="t", name="t", x=100, y=100, text="{{attendee_name}}", width=1200, size=60, font="bebas")]
    text = pdf_text(render_pdf(_input(layers=layers, values={"attendee_name": "അനന്യ Iyer"})))

    assert "Iyer" in text


def test_the_embedded_font_measures_like_the_editor_expects():
    """Plain advances, no kerning: 'AV' is the sum of 'A' and 'V'."""
    m = measure_for("inter-400")

    assert m("AV", 100) == pytest.approx(m("A", 100) + m("V", 100), abs=0.01)
    assert m("", 100) == 0


# --- what a design may not be ------------------------------------------------


def test_a_ticket_without_a_qr_is_refused_but_a_certificate_is_not():
    layers = [TextLayer(id="t", name="t", x=10, y=10, text="{{name}}", width=500, size=40)]

    assert any("QR" in p.message for p in layout_problems(CANVAS, layers, PAGE))
    assert layout_problems(CANVAS, layers, PAGE, qr_required=False) == []


def test_an_unknown_token_names_the_layer_and_suggests():
    layers = [QrLayer(id="q", name="QR", x=10, y=10, size=400),
              TextLayer(id="t", name="Guest", x=10, y=10, text="{{nmae}}", width=500, size=40)]

    messages = [p.message for p in layout_problems(CANVAS, layers, PAGE)]
    assert any(m.startswith("Guest:") and "Did you mean {{name}}" in m for m in messages)


def test_a_qr_too_small_for_a_phone_is_refused():
    layers = [QrLayer(id="q", name="QR", x=10, y=10, size=40)]

    messages = [p.message for p in layout_problems(CANVAS, layers, PAGE)]
    assert any(f"at least {min_qr_size(CANVAS, PAGE)} px" in m for m in messages)


def test_a_fixed_page_refuses_a_canvas_of_another_shape():
    problems = layout_problems(CANVAS, [QrLayer(id="q", name="QR", x=10, y=10, size=400)], PageSpec(width_mm=210, height_mm=297))

    assert any("not the shape of a 210 × 297 mm page" in p.message for p in problems)


def test_the_wire_shape_round_trips_in_camel_case():
    saved = TicketLayoutFields(canvas=CANVAS, layers=_layers(), page=PAGE)
    wire = saved.model_dump(by_alias=True)

    assert "maxLines" in wire["layers"][1]
    assert "quietZone" in wire["layers"][4]
    assert wire["page"] == {"widthMm": 203.2, "heightMm": None}
    assert TicketLayoutFields.model_validate(wire).layers[1].max_lines == 1
