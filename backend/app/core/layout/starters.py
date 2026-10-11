import math
from typing import Optional
"""Complete designs to start from -- no template needed.

Ported from Muster's starters, with their fixed colours kept so a starter
looks the same everywhere; any layer can be pointed at the club's brand
colours instead. Layer ids are stable names, so the editor can speak of
"the QR tile" across saves.
"""

from app.core.layout.model import (
    ImageLayer,
    PAGE_PRESETS,
    PageSpec,
    QrLayer,
    ShapeLayer,
    TextLayer,
    TicketCanvas,
    canvas_for_page,
    min_qr_size,
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


def _jsround(x: float) -> int:
    """JavaScript's Math.round: half rounds up, where Python's rounds to even."""
    return int(math.floor(x + 0.5))


# ─── ID cards ───────────────────────────────────────────────────────────────

BADGE_STARTERS = {
    "classic": {"label": "Conference", "note": "Brand head, photo in a ring, first name large, type band with the QR"},
    "lanyard": {"label": "Bold", "note": "Full colour, a first name you read across the room"},
    "minimal": {"label": "Editorial", "note": "White, serif name, hairline rules, type colour at the top edge"},
    "simple": {"label": "Simple", "note": "A name, a QR and the event — nothing else. The one to reach for when the card has to be read across a room."},
    "insert": {"label": "Badge insert", "note": "Full colour on paper or card: brand head, the name large, a QR, type band in the role's colour. Drawn for the size above, upright or across."},
    "label_4x3": {"label": "Label · 4 × 3 in", "note": "Black only, for a thermal label printer: name across the badge, QR, type band. Colour comes from the roll."},
    "label_4x6": {"label": "Label · 4 × 6 in", "note": "Black only, for a lanyard label: name, a QR you can read from a scanner at arm's length, type band."},
    "label_brother": {"label": "Label · Brother roll", "note": "Black only, for a Brother QL: name stacked, a QR, type band. Drawn for the roll the size above is set to."},
}

# What a paper printer cannot reach at the head and foot of a page, in mm.
PAPER_UNPRINTABLE_MM = 5

_BROTHER_PAGES = {w: PAGE_PRESETS[f"label_{w}x90"]["page"] for w in (29, 38, 50, 54, 62)}
_INSERT_PAGES = [PAGE_PRESETS[k]["page"] for k in ("badge_87x130", "badge_89x137", "badge_130x87", "cr80_portrait", "cr80_landscape", "a6_portrait")]


def _same_page(a: PageSpec, b: Optional[PageSpec]) -> bool:
    return b is not None and abs(a.width_mm - b.width_mm) < 0.05 and (a.height_mm or -1) == (b.height_mm or -1)


def badge_starter_page(kind: str, current: Optional[PageSpec] = None) -> PageSpec:
    """The page a starter is drawn for: the card starters sit on a CR80, the
    label starters on their label, the insert and simple ones on whatever
    badge page the designer is already set to."""
    if kind in ("insert", "simple"):
        return next((p for p in _INSERT_PAGES if _same_page(p, current)), PAGE_PRESETS["badge_87x130"]["page"])
    if kind == "label_4x3":
        return PAGE_PRESETS["label_4x3"]["page"]
    if kind == "label_4x6":
        return PAGE_PRESETS["lanyard_4x6"]["page"]
    if kind == "label_brother":
        width = int(current.width_mm) if current is not None and current.height_mm == 90 else 0
        return _BROTHER_PAGES.get(width, PAGE_PRESETS["label_62x90"]["page"])
    return PAGE_PRESETS["cr80_portrait"]["page"]


def badge_starter(kind: str, *, seats: bool = False, page: Optional[PageSpec] = None) -> dict:
    """A complete ID card to start from. The card starters sit on a CR80
    portrait card at 300 dpi (638 × 1011 px); the label starters are drawn
    black on white for a thermal head. Pixel for pixel the TypeScript port."""
    page = badge_starter_page(kind, page)
    W, H = canvas_for_page(page)
    canvas = TicketCanvas(width=W, height=H, background="#FFFFFF")
    M = 44
    first = "{{seat}}" if seats else "{{first_name}}"
    last = "{{last_name}}"
    org = "{% if organization %}{{organization}}{% endif %}"
    INK = "#141614"

    if kind == "label_4x3":
        L = 60
        qr_size = 280
        qr_x = W - L - qr_size
        name_w = qr_x - 40 - L
        band_y = 740
        return {"page": page, "canvas": canvas, "layers": [
            _image("logo", "Club logo", "org_logo", L, L, 240, 84, align="left"),
            _text("event", "Event name", "{{event_name | upper}}", L + 280, L + 6, W - 2 * L - 280, 24, font="mono", weight=700, color=INK, align="right", letter_spacing=2, max_lines=2),
            _text("when", "Date", "{{event_date | upper}}{% if venue %} · {{venue | upper}}{% endif %}", L + 280, L + 74, W - 2 * L - 280, 18, font="mono", color=INK, align="right", letter_spacing=1),
            _shape("rule", "Rule", L, 168, W - 2 * L, 4, fill=INK),
            _text("first", "Seat", first, L, 200, name_w, 200, font="bebas", color=INK) if seats else _text("first", "First name", "{{first_name | upper}}", L, 200, name_w, 190, font="bebas", color=INK),
            _text("last", "Last name", "{{last_name | upper}}", L, 410, name_w, 72, weight=700, color=INK, hidden=seats),
            _text("org", "Organization", org, L, 506, name_w, 36, color=INK),
            _qr(qr_x, 200, qr_size),
            _text("code-label", "Admission label", "ADMISSION", qr_x, 500, qr_size, 16, font="mono", weight=700, color=INK, align="center", letter_spacing=3),
            _text("code", "Admission number", "{{admission_no}}", qr_x, 528, qr_size, 28, font="mono", weight=700, color=INK, align="center"),
            _shape("band", "Type band", 0, band_y, W, H - band_y, fill=INK),
            _text("type", "Ticket type", "{{ticket_type | upper}}", L, band_y + 30, 720, 96, font="bebas", color="#FFFFFF", letter_spacing=2),
            _text("client", "Club", "{{org_name | upper}}", W - L - 400, band_y + 66, 400, 24, font="mono", color="#FFFFFF", align="right", letter_spacing=2),
        ]}

    if kind == "label_4x6":
        L = 60
        qr_size = 420
        band_y = 1480
        return {"page": page, "canvas": canvas, "layers": [
            _image("logo", "Club logo", "org_logo", L, L, 260, 90, align="left"),
            _text("event", "Event name", "{{event_name | upper}}", L + 300, L + 8, W - 2 * L - 300, 26, font="mono", weight=700, color=INK, align="right", letter_spacing=2, max_lines=2),
            _text("when", "Date", "{{event_date | upper}}{% if venue %} · {{venue | upper}}{% endif %}", L + 300, L + 80, W - 2 * L - 300, 18, font="mono", color=INK, align="right", letter_spacing=1),
            _shape("rule", "Rule", L, 180, W - 2 * L, 4, fill=INK),
            _text("first", "Seat", first, L, 230, W - 2 * L, 240, font="bebas", color=INK, align="center") if seats else _text("first", "First name", "{{first_name | upper}}", L, 230, W - 2 * L, 230, font="bebas", color=INK, align="center"),
            _text("last", "Last name", "{{last_name | upper}}", L, 490, W - 2 * L, 84, weight=700, color=INK, align="center", hidden=seats),
            _text("org", "Organization", org, L, 610, W - 2 * L, 40, color=INK, align="center"),
            _qr((W - qr_size) / 2, 740, qr_size),
            _text("code-label", "Admission label", "ADMISSION", L, 1200, W - 2 * L, 18, font="mono", weight=700, color=INK, align="center", letter_spacing=4),
            _text("code", "Admission number", "{{admission_no}}", L, 1232, W - 2 * L, 40, font="mono", weight=700, color=INK, align="center"),
            _shape("band", "Type band", 0, band_y, W, H - band_y, fill=INK),
            _text("type", "Ticket type", "{{ticket_type | upper}}", L, band_y + 44, W - 2 * L, 150, font="bebas", color="#FFFFFF", align="center", letter_spacing=3),
            _text("client", "Club", "{{org_name | upper}}", L, band_y + 236, W - 2 * L, 26, font="mono", color="#FFFFFF", align="center", letter_spacing=3),
        ]}

    if kind in ("insert", "simple"):
        across = W > H
        px = lambda share, mn=1: max(mn, _jsround(W * share))  # noqa: E731
        py = lambda share, mn=1: max(mn, _jsround(H * share))  # noqa: E731
        foot_safe = max(1, math.ceil((PAPER_UNPRINTABLE_MM / page.height_mm) * H)) if page.height_mm else py(0.02)

        def foot_stack(type_size, client_size, wanted):
            client = H - foot_safe - math.ceil(client_size * 1.2)
            typ = client - math.ceil(type_size * 1.2)
            return typ, client, min(wanted, typ - py(0.02))

        L = max(26, px(0.05 if across else 0.07))
        if kind == "simple":
            cw = W - 2 * L
            qr_size = max(min_qr_size(canvas, page), _jsround(min(cw, H * 0.34)))
            type_size = px(0.072, 20)
            f_type, _f_client, band_y = foot_stack(type_size, 1, py(0.86))
            return {"page": page, "canvas": canvas, "layers": [
                _text("event", "Event name", "{{event_name | upper}}", L, py(0.06), cw, px(0.03, 13), font="mono", weight=700, color="#5A5F5B", letter_spacing=3, align="center", max_lines=2),
                _text("first", "Seat" if seats else "First name", "{{seat}}" if seats else "{{first_name | upper}}", L, py(0.16), cw, px(0.155, 40), font="bebas", color=INK, align="center", max_lines=2),
                _text("last", "Last name", "{{last_name | upper}}", L, py(0.34), cw, px(0.062, 18), weight=700, color=INK, align="center", hidden=seats, max_lines=2),
                _text("org", "Organization", org, L, py(0.42), cw, px(0.04, 14), color="#5A5F5B", align="center", max_lines=2),
                _qr(_jsround((W - qr_size) / 2), py(0.5), qr_size),
                _shape("band", "Type band", 0, band_y, W, H - band_y, fill="ticket_type"),
                _text("type", "Ticket type", "{{ticket_type | upper}}", L, f_type, cw, type_size, font="bebas", color="#FFFFFF", align="center", letter_spacing=2),
            ]}
        if not across:
            cw = W - 2 * L
            qr_size = px(0.292, min_qr_size(canvas, page))
            col = L + qr_size + px(0.035, 14)
            col_w = W - L - col
            type_size = px(0.107, 26)
            client_size = px(0.021, 10)
            f_type, f_client, band_y = foot_stack(type_size, client_size, py(0.84))
            return {"page": page, "canvas": canvas, "layers": [
                _shape("head", "Brand head", 0, 0, W, py(0.011, 3), fill="brand.primary"),
                _image("logo", "Club logo", "org_logo", L, py(0.055), px(0.292), py(0.062, 26), align="left"),
                _text("event", "Event name", "{{event_name | upper}}", L, py(0.133), cw, px(0.027, 13), font="mono", weight=700, color=INK, letter_spacing=2, max_lines=2),
                _text("when", "Date", "{{event_date | upper}}{% if venue %} · {{venue | upper}}{% endif %}", L, py(0.162), cw, px(0.0195, 11), font="mono", color=INK, letter_spacing=1, max_lines=2),
                _shape("rule", "Rule", L, py(0.215), cw, max(2, py(0.0026)), fill="brand.primary"),
                _text("first", "Seat", first, L, py(0.248), cw, px(0.195, 40), font="bebas", color=INK) if seats else _text("first", "First name", "{{first_name | upper}}", L, py(0.248), cw, px(0.195, 40), font="bebas", color=INK, max_lines=2),
                _text("last", "Last name", "{{last_name | upper}}", L, py(0.395), cw, px(0.076, 20), weight=700, color=INK, hidden=seats, max_lines=2),
                _text("org", "Organization", org, L, py(0.46), cw, px(0.039, 14), color=INK, max_lines=2),
                _qr(L, py(0.541), qr_size),
                _text("code-label", "Admission label", "ADMISSION", col, py(0.559), col_w, px(0.0175, 10), font="mono", weight=700, color=INK, letter_spacing=2),
                _text("code", "Admission number", "{{admission_no}}", col, py(0.581), col_w, px(0.029, 13), font="mono", weight=700, color=INK, max_lines=2),
                _shape("band", "Type band", 0, band_y, W, H - band_y, fill="ticket_type"),
                _text("type", "Ticket type", "{{ticket_type | upper}}", L, f_type, cw, type_size, font="bebas", color="#FFFFFF", letter_spacing=2),
                _text("client", "Club", "{{org_name | upper}}", L, f_client, cw, client_size, font="mono", color="#FFFFFF", letter_spacing=2),
            ]}
        type_size = px(0.06, 20)
        client_size = px(0.014, 9)
        f_type, f_client, band_y = foot_stack(type_size, client_size, py(0.83))
        qr_size = max(min_qr_size(canvas, page), py(0.4))
        qr_x = W - L - qr_size
        name_w = qr_x - L - px(0.03, 12)
        return {"page": page, "canvas": canvas, "layers": [
            _shape("head", "Brand head", 0, 0, W, py(0.017, 3), fill="brand.primary"),
            _image("logo", "Club logo", "org_logo", L, py(0.075), px(0.19), py(0.1, 26), align="left"),
            _text("event", "Event name", "{{event_name | upper}}", L + px(0.22), py(0.085), W - L - px(0.22) - L, px(0.021, 12), font="mono", weight=700, color=INK, letter_spacing=2, max_lines=2),
            _text("when", "Date", "{{event_date | upper}}{% if venue %} · {{venue | upper}}{% endif %}", L + px(0.22), py(0.145), W - L - px(0.22) - L, px(0.0155, 10), font="mono", color=INK, letter_spacing=1, max_lines=2),
            _shape("rule", "Rule", L, py(0.3), W - 2 * L, max(2, py(0.004)), fill="brand.primary"),
            _text("first", "Seat", first, L, py(0.35), name_w, px(0.1, 34), font="bebas", color=INK) if seats else _text("first", "First name", "{{first_name | upper}}", L, py(0.35), name_w, px(0.1, 34), font="bebas", color=INK, max_lines=2),
            _text("last", "Last name", "{{last_name | upper}}", L, py(0.55), name_w, px(0.042, 16), weight=700, color=INK, hidden=seats, max_lines=2),
            _text("org", "Organization", org, L, py(0.65), name_w, px(0.024, 12), color=INK, max_lines=2),
            _qr(qr_x, py(0.32), qr_size),
            _text("code-label", "Admission label", "ADMISSION", qr_x, py(0.32) + qr_size + py(0.02), qr_size, px(0.013, 9), font="mono", weight=700, color=INK, letter_spacing=2, align="center"),
            _text("code", "Admission number", "{{admission_no}}", qr_x, py(0.32) + qr_size + py(0.055), qr_size, px(0.02, 11), font="mono", weight=700, color=INK, align="center", max_lines=2),
            _shape("band", "Type band", 0, band_y, W, H - band_y, fill="ticket_type"),
            _text("type", "Ticket type", "{{ticket_type | upper}}", L, f_type, name_w, type_size, font="bebas", color="#FFFFFF", letter_spacing=2),
            _text("client", "Club", "{{org_name | upper}}", L, f_client, name_w, client_size, font="mono", color="#FFFFFF", letter_spacing=2),
        ]}

    if kind == "label_brother":
        k = W / 732
        px = lambda n, mn=1: max(mn, _jsround(n * k))  # noqa: E731
        L = max(26, px(60))
        cw = W - 2 * L
        qr_size = min(260, max(min_qr_size(canvas, page), _jsround(cw * 0.62)))
        beside = cw - qr_size - px(24) >= 190
        band_y = 900
        head = [
            _image("logo", "Club logo", "org_logo", L, L, min(cw, px(220)), px(70, 30), align="left"),
            _text("event", "Event name", "{{event_name | upper}}", L, L + px(90, 44), cw, px(20, 13), font="mono", weight=700, color=INK, letter_spacing=2, max_lines=2),
            _shape("rule", "Rule", L, 210, cw, 4, fill=INK),
        ]
        foot = [
            _shape("band", "Type band", 0, band_y, W, H - band_y, fill=INK),
            _text("type", "Ticket type", "{{ticket_type | upper}}", L, band_y + 30, cw, px(80, 40), font="bebas", color="#FFFFFF", letter_spacing=2),
            _text("client", "Club", "{{org_name | upper}}", L, band_y + 120, cw, px(18, 12), font="mono", color="#FFFFFF", letter_spacing=2),
        ]

        def name(y, size, last_size, org_size):
            return [
                _text("first", "Seat", first, L, y, cw, size, font="bebas", color=INK) if seats else _text("first", "First name", "{{first_name | upper}}", L, y, cw, size, font="bebas", color=INK, max_lines=2),
                _text("last", "Last name", "{{last_name | upper}}", L, y + _jsround(size * 1.14), cw, last_size, weight=700, color=INK, hidden=seats, max_lines=2),
                _text("org", "Organization", org, L, y + _jsround(size * 1.64), cw, org_size, color=INK, max_lines=2),
            ]

        if beside:
            col = L + qr_size + px(24, 14)
            col_w = W - L - col
            return {"page": page, "canvas": canvas, "layers": [
                *head,
                *name(240, px(140, 54), px(54, 26), px(30, 16)),
                _qr(L, 560, qr_size),
                _text("code-label", "Admission label", "ADMISSION", col, 580, col_w, px(14, 11), font="mono", weight=700, color=INK, letter_spacing=2),
                _text("code", "Admission number", "{{admission_no}}", col, 606, col_w, px(22, 14), font="mono", weight=700, color=INK, max_lines=2),
                _text("when", "Date", "{{event_date | upper}}", col, 700, col_w, px(16, 12), font="mono", color=INK, letter_spacing=1, max_lines=2),
                *foot,
            ]}
        qr_x = _jsround((W - qr_size) / 2)
        qr_y = 560 - _jsround(qr_size * 0.12)
        return {"page": page, "canvas": canvas, "layers": [
            *head,
            *name(238, px(150, 46), px(60, 22), px(34, 15)),
            _qr(qr_x, qr_y, qr_size),
            _text("code-label", "Admission label", "ADMISSION", L, qr_y + qr_size + 16, cw, px(14, 10), font="mono", weight=700, color=INK, letter_spacing=2, align="center"),
            _text("code", "Admission number", "{{admission_no}}", L, qr_y + qr_size + 40, cw, px(22, 14), font="mono", weight=700, color=INK, align="center", max_lines=2),
            *foot,
        ]}

    if kind == "lanyard":
        panel_y, panel_h, qr_size = 640, 335, 190
        qr_y = panel_y + (panel_h - qr_size) / 2
        return {"page": page, "canvas": canvas, "layers": [
            _shape("background", "Card colour", 0, 0, W, H, fill="brand.primary"),
            _image("logo", "Club logo", "org_logo", M, 40, 170, 70, align="left"),
            _text("event", "Event name", "{{event_name | upper}}", M, 128, W - 2 * M, 24, font="mono", weight=700, color="brand.secondary", letter_spacing=3, max_lines=2),
            _text("first", "First name", first if seats else "{{first_name | upper}}", M, 222, W - 2 * M, 190, font="bebas", color="#FFFFFF"),
            _text("last", "Last name", "{{last_name | upper}}", M, 440, W - 2 * M, 84, font="bebas", color="brand.secondary", hidden=seats),
            _text("org", "Organization", org, M, 548, W - 2 * M, 28, color="#FFFFFF", opacity=0.82),
            _shape("panel", "White panel", 24, panel_y, W - 48, panel_h, fill="#FFFFFF", radius=28),
            _shape("pill", "Type pill", 56, qr_y + 6, 260, 60, fill="ticket_type", radius=30),
            _text("type", "Ticket type", "{{ticket_type | upper}}", 64, qr_y + 20, 244, 26, font="mono", weight=700, color="#FFFFFF", align="center", letter_spacing=2),
            _text("code-label", "Admission label", "ADMISSION", 56, qr_y + 100, 260, 16, font="mono", weight=700, color="#8C918C", letter_spacing=3),
            _text("code", "Admission number", "{{admission_no}}", 56, qr_y + 124, 260, 26, font="mono", weight=700, color="#141614"),
            _text("when", "Date", "{{event_date | upper}}", 56, qr_y + 166, 260, 20, font="mono", color="#5A5F5B", letter_spacing=1),
            _qr(W - 24 - 32 - qr_size, qr_y, qr_size),
            _shape("strip", "Type strip", 0, H - 22, W, 22, fill="ticket_type"),
        ]}

    if kind == "minimal":
        return {"page": page, "canvas": canvas, "layers": [
            _shape("bar", "Type bar", 0, 0, W, 26, fill="ticket_type"),
            _text("event", "Event name", "{{event_name | upper}}", M, 104, W - 2 * M, 22, font="mono", weight=700, letter_spacing=3, max_lines=2),
            _text("when", "Date", "{{event_date | upper}}{% if venue %} · {{venue | upper}}{% endif %}", M, 146, W - 2 * M, 18, font="mono", color="#8C918C", letter_spacing=1),
            _shape("rule-top", "Top rule", M, 204, W - 2 * M, 2, fill="#141614"),
            _text("first", "First name", first, M, 286, W - 2 * M, 104, font="dm_serif"),
            _text("last", "Last name", last, M, 402, W - 2 * M, 104, font="dm_serif", color="#5A5F5B", hidden=seats),
            _text("org", "Organization", org, M, 556, W - 2 * M, 28, color="#5A5F5B"),
            _text("type", "Ticket type", "{{ticket_type | upper}}", M, 606, W - 2 * M, 26, font="mono", weight=700, color="ticket_type", letter_spacing=3),
            _shape("rule-bottom", "Bottom rule", M, 700, W - 2 * M, 2, fill="#E1E1DC"),
            _qr(M, 748, 190),
            _text("code-label", "Admission label", "ADMISSION", M + 218, 760, W - M - (M + 218), 16, font="mono", weight=700, color="#8C918C", letter_spacing=3),
            _text("code", "Admission number", "{{admission_no}}", M + 218, 786, W - M - (M + 218), 24, font="mono", weight=700),
            _image("logo", "Club logo", "org_logo", W - M - 160, 868, 160, 70, align="right"),
        ]}

    # classic
    head_h, ring, photo, band_y, tile = 400, 300, 270, 800, 176
    return {"page": page, "canvas": canvas, "layers": [
        _shape("head", "Brand head", 0, 0, W, head_h, fill="brand.primary"),
        _image("logo", "Club logo", "org_logo", M, 40, 170, 70, align="left"),
        _text("event", "Event name", "{{event_name | upper}}", M, 120, W - 2 * M, 28, font="mono", weight=700, color="#FFFFFF", letter_spacing=2, max_lines=2),
        _text("when", "Date", "{{event_date | upper}}", M, 192, 300, 22, font="mono", color="brand.secondary", letter_spacing=2),
        _shape("accent", "Accent rule", M, 236, 64, 6, fill="brand.secondary"),
        _shape("photo-ring", "Photo ring", (W - ring) / 2, head_h - ring / 2, ring, ring, fill="#FFFFFF", radius=ring / 2, hidden=seats),
        _image("photo", "Guest photo", "guest_photo", (W - photo) / 2, head_h - photo / 2, photo, photo, fit="cover", radius=photo / 2, hidden=seats),
        _text("first", "Seat", first, M, 470, W - 2 * M, 140, font="bebas", align="center") if seats else _text("first", "First name", first, M, 574, W - 2 * M, 84, weight=700, align="center"),
        _text("last", "Last name", last, M, 676, W - 2 * M, 40, align="center", color="#3C413D", hidden=seats),
        _text("org", "Organization", org, M, 736, W - 2 * M, 26, align="center", color="#6B716C"),
        _shape("foot", "Role band", 0, band_y, W, H - band_y, fill="ticket_type"),
        _text("type", "Ticket type", "{{ticket_type | upper}}", M, band_y + 36, 330, 64, font="bebas", color="#FFFFFF", letter_spacing=2),
        _text("code", "Admission number", "{{admission_no}}", M, band_y + 130, 330, 22, font="mono", color="#FFFFFF", opacity=0.85),
        _shape("qr-tile", "QR tile", W - M - tile, band_y + 18, tile, tile, fill="#FFFFFF", radius=20),
        _qr(W - M - tile + (tile - 150) / 2, band_y + 18 + (tile - 150) / 2, 150),
    ]}


# ─── Agenda / programme ──────────────────────────────────────────────────

from app.core.layout.model import AgendaLayer  # noqa: E402

AGENDA_STARTERS = {
    "screen": {"label": "Kiosk screen", "note": "Dark, 16:9 — brand panel beside the sessions, big type"},
    "screen_light": {"label": "Screen, light", "note": "White, 16:9 — brand header, two columns"},
    "portrait": {"label": "Kiosk portrait", "note": "Dark, 9:16 — for a tablet standing upright"},
    "programme": {"label": "Printed programme", "note": "A4 portrait — cover block, one readable column"},
}

_KICKER = dict(font="mono", weight=700, color="brand.secondary", letter_spacing=4)
_WHEN = "{{event_day | upper}} · {{event_date | upper}}"


def _agenda(id, x, y, width, height, **more) -> AgendaLayer:
    return AgendaLayer(id=id, name="Run of show", x=x, y=y, width=width, height=height, **more)


def _canvas(w: int, h: int, background: str) -> TicketCanvas:
    return TicketCanvas(width=w, height=h, background=background, template_key=None, template_preprinted=False, area=None)


def agenda_starter(kind: str = "screen") -> dict:
    """A complete programme to start from: a 16:9 screen at 1920 × 1080, a
    9:16 screen at 1080 × 1920, or an A4 page at 150 dpi. Screens are dark
    (the kiosk around them is), set the sessions at a size read from two
    metres, and give the event its own block in the brand colour; the page
    is light and keeps the sessions to one column, which reads better on
    paper than two narrow ones."""
    if kind == "programme":
        page = PAGE_PRESETS["a4_portrait"]["page"]
        W, H = canvas_for_page(page, 150)
        M = 100
        cover = 660
        return {
            "page": page,
            "canvas": _canvas(W, H, "#FFFFFF"),
            "layers": [
                _shape("cover", "Cover block", 0, 0, W, cover, fill="brand.primary"),
                _image("logo", "Client logo", "org_logo", M, 90, 260, 100, align="left"),
                _text("kicker", "Kicker", "PROGRAMME", M, 262, 800, 24, **_KICKER),
                _text("title", "Event name", "{{event_name}}", M, 304, W - 2 * M, 92, font="dm_serif", color="#FFFFFF", max_lines=2),
                _text("when", "Date", _WHEN, M, 548, W - 2 * M, 26, font="mono", weight=700, color="#FFFFFF", letter_spacing=2),
                _text("venue", "Venue", "{{venue}}", M, 592, W - 2 * M, 28, color="#FFFFFF", opacity=0.8),
                _shape("accent", "Accent strip", 0, cover, W, 10, fill="brand.secondary"),
                _agenda("agenda", M, cover + 90, W - 2 * M, H - cover - 90 - 150, size=30, row_gap=26, accent="brand.primary"),
                _shape("foot-rule", "Footer rule", M, H - 110, W - 2 * M, 2, fill="#E1E1DC"),
                _text("foot", "Footer", "{{org_name}}", M, H - 84, 640, 20, font="mono", color="#5A5F5B"),
                _text("note", "Footnote", "Times may change on the day", W - M - 520, H - 84, 520, 20, font="mono", color="#8C918C", align="right"),
            ],
        }

    if kind == "portrait":
        page = PAGE_PRESETS["screen_portrait"]["page"]
        W, H = 1080, 1920
        M = 80
        block = 760
        return {
            "page": page,
            "canvas": _canvas(W, H, "#0B140F"),
            "layers": [
                _shape("block", "Brand block", 0, 0, W, block, fill="brand.primary"),
                _image("logo", "Client logo", "org_logo", M, 90, 260, 100, align="left"),
                _text("kicker", "Kicker", "TODAY’S PROGRAMME", M, 300, W - 2 * M, 26, **_KICKER),
                _text("title", "Event name", "{{event_name}}", M, 350, W - 2 * M, 96, weight=700, color="#FFFFFF", max_lines=2),
                _shape("accent", "Accent rule", M, 610, 96, 8, fill="brand.secondary"),
                _text("when", "Date", _WHEN, M, 650, W - 2 * M, 30, font="mono", weight=700, color="#FFFFFF", letter_spacing=2),
                _text("venue", "Venue", "{{venue}}", M, 698, W - 2 * M, 30, color="#FFFFFF", opacity=0.78),
                _agenda("agenda", M, block + 90, W - 2 * M, H - block - 90 - 160, size=44, row_gap=40, color="#F2F5F1", muted="#9AA59C", accent="brand.secondary"),
                _text("foot", "Footer", "{{org_name | upper}}", M, H - 90, W - 2 * M, 18, font="mono", color="#FFFFFF", opacity=0.5, letter_spacing=3),
            ],
        }

    page = PAGE_PRESETS["screen_landscape"]["page"]
    W, H = 1920, 1080

    if kind == "screen_light":
        M = 100
        band = 300
        top = band + 130
        width = W - 2 * M
        size = 36
        # The hairline sits in the gap between the two columns the layer lays out.
        col_width = (width - size * 1.5) / 2
        return {
            "page": page,
            "canvas": _canvas(W, H, "#F7F7F4"),
            "layers": [
                _shape("band", "Header band", 0, 0, W, band, fill="brand.primary"),
                _image("logo", "Client logo", "org_logo", M, 60, 240, 90, align="left"),
                _text("when", "Date", _WHEN, W - M - 900, 74, 900, 26, font="mono", weight=700, color="#FFFFFF", align="right", letter_spacing=2),
                _text("venue", "Venue", "{{venue}}", W - M - 900, 116, 900, 26, color="#FFFFFF", opacity=0.8, align="right"),
                _text("title", "Event name", "{{event_name}}", M, 180, width, 76, weight=700, color="#FFFFFF"),
                _shape("accent", "Accent strip", 0, band, W, 10, fill="brand.secondary"),
                _text("kicker", "Kicker", "PROGRAMME", M, band + 64, 600, 22, **{**_KICKER, "color": "brand.primary"}),
                _agenda("agenda", M, top, width, H - top - 110, size=size, columns=2, row_gap=30, accent="brand.primary"),
                _shape("divider", "Column divider", M + col_width + (size * 1.5) / 2 - 1, top, 2, H - top - 130, fill="#DCDDD6"),
                _text("foot", "Footer", "{{org_name | upper}}", M, H - 64, 900, 18, font="mono", color="#8C918C", letter_spacing=3),
                _text("note", "Footnote", "Times may change on the day", W - M - 700, H - 64, 700, 18, font="mono", color="#8C918C", align="right"),
            ],
        }

    M = 80
    panel = 660
    right = panel + 100
    return {
        "page": page,
        "canvas": _canvas(W, H, "#0B140F"),
        "layers": [
            _shape("panel", "Brand panel", 0, 0, panel, H, fill="brand.primary"),
            _image("logo", "Client logo", "org_logo", M, 80, 260, 100, align="left"),
            _text("kicker", "Kicker", "TODAY’S PROGRAMME", M, 330, panel - 2 * M, 24, **_KICKER),
            _text("title", "Event name", "{{event_name}}", M, 380, panel - 2 * M, 78, weight=700, color="#FFFFFF", max_lines=4),
            _shape("accent", "Accent rule", M, 800, 96, 8, fill="brand.secondary"),
            _text("when", "Date", _WHEN, M, 840, panel - 2 * M, 26, font="mono", weight=700, color="#FFFFFF", letter_spacing=2),
            _text("venue", "Venue", "{{venue}}", M, 888, panel - 2 * M, 30, color="#FFFFFF", opacity=0.78, max_lines=2),
            _text("foot", "Footer", "{{org_name | upper}}", M, H - 80, panel - 2 * M, 18, font="mono", color="#FFFFFF", opacity=0.55, letter_spacing=3),
            _text("section", "Section label", "RUN OF SHOW", right, 96, 800, 20, **{**_KICKER, "color": "#6F7B72"}),
            _shape("rule", "Header rule", right, 138, W - right - M, 2, fill="#1F2A23"),
            _agenda("agenda", right, 184, W - right - M, H - 184 - 100, size=44, row_gap=36, color="#F2F5F1", muted="#9AA59C", accent="brand.secondary"),
        ],
    }
