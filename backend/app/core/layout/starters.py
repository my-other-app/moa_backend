"""Complete designs to start from -- no template needed.

Ported from Muster's starters, with their fixed colours kept so a starter
looks the same everywhere; any layer can be pointed at the club's brand
colours instead. Layer ids are stable names, so the editor can speak of
"the QR tile" across saves.
"""

from app.core.layout.model import (
    PAGE_PRESETS,
    ImageLayer,
    PageSpec,
    QrLayer,
    ShapeLayer,
    TextLayer,
    TicketCanvas,
    canvas_for_page,
)

TICKET_STARTERS = {
    "invitation": {"label": "Invitation", "note": "Warm stub and body — a colour, or a picture — a swoosh, the QR on a white tile"},
    "classic": {"label": "Classic", "note": "Brand stripe, tinted stub, type pill"},
    "minimal": {"label": "Minimal", "note": "Hairline frame, nothing else"},
    "stub": {"label": "Stub", "note": "Dark ticket with a tear-off stub"},
}

CERTIFICATE_STARTERS = {
    "classic": {"label": "Classic", "note": "Centred, serif title, brand rule"},
    "modern": {"label": "Modern", "note": "Left-aligned, colour panel, big name"},
}


def _text(id, name, t, x, y, width, size, **more) -> TextLayer:
    return TextLayer(id=id, name=name, text=t, x=x, y=y, width=width, size=size, **more)


def _shape(id, name, x, y, width, height, **more) -> ShapeLayer:
    return ShapeLayer(id=id, name=name, x=x, y=y, width=width, height=height, **more)


def _qr(x, y, size) -> QrLayer:
    return QrLayer(id="qr", name="QR code", x=x, y=y, size=size)


def _image(id, name, source, x, y, width, height, **more) -> ImageLayer:
    return ImageLayer(id=id, name=name, source=source, x=x, y=y, width=width, height=height, **more)


def ticket_starter(kind: str, *, seats: bool = False) -> dict:
    """A complete ticket design: canvas, layers and the 8-inch page."""
    W, H = 1560, 690
    page = PAGE_PRESETS["ticket"]["page"]
    guest = _text("guest", "Guest name", "{{seat}}" if seats else "{{attendee_name}}", 72, 288, 780, 45, weight=500)

    if kind == "invitation":
        ORANGE, YELLOW, MAROON, CREAM = "#C07736", "#F7D348", "#3D0806", "#FBEFE2"
        STUB = 390
        BODY = STUB + 5
        BODY_W = W - BODY
        L = 445
        return {
            "page": page,
            "canvas": TicketCanvas(width=W, height=H, background="#FFFFFF"),
            "layers": [
                _shape("stub", "Stub colour", 0, 0, STUB, H, fill=ORANGE, radius=38),
                _image("stub-photo", "Stub photo", "upload", 0, 0, STUB, H, fit="cover", radius=38, hidden=True),
                _shape("chevron", "Stub chevron", 0, 0, STUB, H, fill=YELLOW, path="M 0 441 L 524 771 L 1000 332 L 1000 474 L 517 905 L 0 593 Z"),
                _shape("title-box", "Title box", 31, 31, 325, 154, fill=MAROON, radius=14),
                _text("event", "Event name", "{{event_name}}", 56, 50, 280, 52, weight=600, color="#FFFFFF", max_lines=2, line_spacing=-8),
                _shape("qr-tile", "QR tile", 37, 326, 314, 312, fill="#FFFFFF", radius=22),
                _qr(59, 347, 270),
                _text("admit", "Admit one", "ADMIT ONE", 37, 652, 314, 20, font="mono", weight=700, color=CREAM, align="center", letter_spacing=6, opacity=0.8),
                _shape("body", "Body colour", BODY, 0, BODY_W, H, fill=ORANGE, radius=38),
                _image("body-photo", "Body photo", "upload", BODY, 0, BODY_W, H, fit="cover", radius=38, hidden=True),
                _shape("scrim", "Photo scrim", BODY, 0, BODY_W, H, fill="#141614", opacity=0.45, radius=38, hidden=True),
                _shape("hairline", "Hairline frame", BODY + 20, 20, BODY_W - 40, H - 40, fill=None, stroke=CREAM, stroke_width=2, radius=28, opacity=0.3),
                _shape("swoosh", "Swoosh", 1098, 0, 462, 214, fill=YELLOW, path="M 0 0 L 162 0 C 162 287 165 478 206 605 C 236 688 280 713 324 688 L 1000 369 L 472 860 C 324 968 206 1000 133 955 C 59 904 24 764 12 573 C 3 382 0 191 0 0 Z"),
                _image("logo", "Club logo", "org_logo", 1300, 44, 190, 44, align="right", opacity=0.9),
                _shape("type-pill", "Ticket type pill", L, 58, 218, 54, fill=None, stroke=CREAM, stroke_width=2, radius=27, opacity=0.6),
                _text("type", "Ticket type", "{{ticket_type | upper}}", L, 74, 218, 22, font="mono", weight=700, color=CREAM, align="center", letter_spacing=4, opacity=0.9),
                _text("guest-label", "Guest label", "SEAT" if seats else "DELEGATE", L, 356, 420, 20, font="mono", weight=700, color=CREAM, letter_spacing=6, opacity=0.7),
                _text("guest", "Seat", "{{seat}}", L, 392, 900, 60, font="bebas", color=CREAM) if seats
                else _text("guest", "Guest name", "{{attendee_name}}", L, 392, 900, 54, weight=600, color=CREAM),
                _text("when", "Date & venue", "{{event_date | upper}} · {{event_time}}{% if venue %} · {{venue | upper}}{% endif %}", L, 474, 900, 24, font="mono", color=CREAM, letter_spacing=1, opacity=0.8),
                _text("code-label", "Invite code label", "INVITE CODE", L, 548, 420, 18, font="mono", weight=700, color=CREAM, letter_spacing=6, opacity=0.7),
                _text("code", "Invite code", "{{admission_no}}", L, 576, 700, 34, font="mono", weight=700, color=CREAM),
            ],
        }
    if kind == "stub":
        return {
            "page": page,
            "canvas": TicketCanvas(width=W, height=H, background="#141614"),
            "layers": [
                _shape("stub", "Stub panel", 1110, 0, 450, H, fill="brand.primary"),
                _shape("perforation", "Perforation", 1108, 30, 4, H - 60, fill="#FFFFFF", opacity=0.35),
                _text("client", "Club", "{{org_name | upper}}", 72, 60, 900, 27, font="mono", weight=700, color="brand.secondary", letter_spacing=3),
                _text("event", "Event name", "{{event_name}}", 72, 112, 960, 66, weight=700, color="#FFFFFF", max_lines=2),
                guest.model_copy(update={"y": 330, "color": "#FFFFFF"}),
                _text("when", "Date & time", "{{event_day}} · {{event_date}} · {{event_time}}{% if event_end_time %}–{{event_end_time}}{% endif %}", 72, 420, 960, 30, color="#C9CCC9"),
                _text("venue", "Venue", "{{venue}}", 72, 466, 960, 30, color="#C9CCC9"),
                _shape("type-pill", "Ticket type pill", 72, 556, 252, 60, fill="ticket_type", radius=30),
                _text("type", "Ticket type", "{{ticket_type | upper}}", 84, 568, 228, 36, font="bebas", color="#FFFFFF", align="center", letter_spacing=2),
                _qr(1185, 120, 300),
                _text("code", "Admission number", "{{admission_no}}", 1140, 456, 390, 27, font="mono", color="#FFFFFF", align="center"),
            ],
        }
    if kind == "minimal":
        return {
            "page": page,
            "canvas": TicketCanvas(width=W, height=H, background="#FFFFFF"),
            "layers": [
                _shape("frame", "Frame", 12, 12, W - 24, H - 24, fill=None, stroke="#E1E1DC", stroke_width=3, radius=24),
                _text("client", "Club", "{{org_name | upper}}", 72, 54, 900, 27, font="mono", weight=700, color="#8C918C", letter_spacing=2),
                _text("event", "Event name", "{{event_name}}", 72, 110, 1000, 57, weight=700, max_lines=2),
                guest,
                _text("venue", "Venue", "{{venue}}", 72, 384, 780, 30, color="#5A5F5B"),
                _text("when", "Date & time", "{{event_date | upper}} · {{event_time}}", 72, 468, 600, 30, font="mono", color="#5A5F5B"),
                _text("type", "Ticket type", "{{ticket_type}}", 72, 540, 600, 36, weight=600, color="ticket_type"),
                _text("code", "Admission number", "{{admission_no}}", 72, 600, 600, 27, font="mono", color="#8C918C"),
                _qr(1164, 150, 330),
            ],
        }
    # classic
    return {
        "page": page,
        "canvas": TicketCanvas(width=W, height=H, background="#FFFFFF"),
        "layers": [
            _shape("stripe", "Brand stripe", 0, 0, 21, H, fill="brand.primary"),
            _shape("stub", "Stub tint", 1110, 0, 450, H, fill="brand.primary", opacity=0.08),
            _shape("band", "Stub band", 1110, H - 15, 450, 15, fill="brand.secondary"),
            _shape("divider", "Divider", 1109, 36, 3, H - 72, fill="#E1E1DC"),
            _text("client", "Club", "{{org_name | upper}}", 72, 54, 900, 27, font="mono", weight=700, color="brand.primary", letter_spacing=1),
            _text("event", "Event name", "{{event_name}}", 72, 108, 960, 57, weight=700, max_lines=2),
            guest,
            _text("venue", "Venue", "{{venue}}", 72, 384, 780, 30, color="#5A5F5B"),
            _shape("type-pill", "Ticket type pill", 72, 468, 252, 60, fill="ticket_type", radius=30),
            _text("type", "Ticket type", "{{ticket_type | upper}}", 84, 484, 228, 27, font="mono", weight=700, color="#FFFFFF", align="center"),
            _text("when", "Date & time", "{{event_date | upper}} · {{event_time}}", 354, 484, 600, 30, font="mono", color="#5A5F5B"),
            _text("code", "Admission number", "{{admission_no}}", 72, 576, 600, 30, font="mono", color="#5A5F5B"),
            _qr(1164, 168, 324),
        ],
    }


def certificate_starter(kind: str = "classic") -> dict:
    """A complete certificate, A4 landscape at 150 dpi (1754 × 1240 px)."""
    page: PageSpec = PAGE_PRESETS["a4_landscape"]["page"]
    W, H = canvas_for_page(page, 150)
    canvas = TicketCanvas(width=W, height=H, background="#FFFFFF")
    body = "attended {{event_name}} on {{event_date}}{% if venue %} at {{venue}}{% endif %}."
    if kind == "modern":
        return {
            "page": page,
            "canvas": canvas,
            "layers": [
                _shape("panel", "Colour panel", 0, 0, 420, H, fill="brand.primary"),
                _image("logo", "Club logo", "org_logo", 60, 60, 300, 120),
                _text("kind", "Kind of certificate", "CERTIFICATE OF ATTENDANCE", 520, 200, W - 620, 26, font="mono", weight=700, color="brand.secondary", letter_spacing=4),
                _text("lead", "Lead line", "This is to certify that", 520, 300, W - 620, 30, color="#5A5F5B"),
                _text("guest", "Guest name", "{{attendee_name}}", 520, 360, W - 620, 110, font="dm_serif", max_lines=2, fit="shrink"),
                _text("body", "Body", body, 520, 620, W - 620, 34, max_lines=3, line_spacing=6, color="#141614"),
                _text("org", "Organiser", "{{org_name}}", 520, 900, 600, 30, weight=600),
                _text("date", "Issued", "Issued {{issued_date}}", 520, 945, 600, 24, color="#5A5F5B"),
                _text("no", "Certificate number", "{{certificate_no}}", 520, H - 110, 700, 22, font="mono", color="#8C918C"),
                _qr(W - 60 - 200, H - 60 - 200, 200),
            ],
        }
    return {
        "page": page,
        "canvas": canvas,
        "layers": [
            _shape("frame", "Frame", 60, 60, W - 120, H - 120, fill=None, stroke="brand.primary", stroke_width=4),
            _shape("inner", "Inner rule", 84, 84, W - 168, H - 168, fill=None, stroke="brand.secondary", stroke_width=1.5),
            _image("logo", "Club logo", "org_logo", (W - 260) / 2, 130, 260, 100),
            _text("kind", "Kind of certificate", "CERTIFICATE OF ATTENDANCE", 200, 270, W - 400, 26, font="mono", weight=700, color="brand.secondary", letter_spacing=6, align="center"),
            _text("lead", "Lead line", "This is to certify that", 200, 360, W - 400, 30, color="#5A5F5B", align="center"),
            _text("guest", "Guest name", "{{attendee_name}}", 160, 420, W - 320, 120, font="dm_serif", align="center", max_lines=2, fit="shrink"),
            _shape("rule", "Name rule", 460, 700, W - 920, 3, fill="brand.primary"),
            _text("body", "Body", body, 260, 740, W - 520, 34, align="center", max_lines=3, line_spacing=6),
            _text("org", "Organiser", "{{org_name}}", 200, 960, 600, 30, weight=600),
            _text("date", "Issued", "Issued {{issued_date}}", 200, 1005, 600, 24, color="#5A5F5B"),
            _text("no", "Certificate number", "{{certificate_no}}", W - 330 - 500, 1005, 500, 22, font="mono", color="#8C918C", align="right"),
            _qr(W - 120 - 170, H - 120 - 170 - 40, 170),
        ],
    }
