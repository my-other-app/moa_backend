from typing import Optional
"""Designs to start a custom design from -- no template needed.

The designer's designs (app/api/doc_presets, app/core/doc_designs) replaced
the ticket, certificate and badge starters that were here. What is left:

- `card_cr80`, the one credit-card (CR80) ID card, for card printers: the
  old "Conference" card, kept as the owner asked;
- `blank_layout`, the canvas a custom ticket or certificate starts on;
- the programme (agenda) starters, unchanged.

Fixed colours are kept so a starter looks the same everywhere; any layer can
be pointed at the club's brand colours instead. Layer ids are stable names,
so the editor can speak of "the QR tile" across saves.
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
)

#: The designer's designs are the ticket and certificate starting points now
#: (doc_presets); a custom one starts on `blank_layout`.
TICKET_STARTERS: dict = {}
CERTIFICATE_STARTERS: dict = {}


def _text(id, name, t, x, y, width, size, **more) -> TextLayer:
    return TextLayer(id=id, name=name, text=t, x=x, y=y, width=width, size=size, **more)


def _shape(id, name, x, y, width, height, **more) -> ShapeLayer:
    return ShapeLayer(id=id, name=name, x=x, y=y, width=width, height=height, **more)


def _qr(x, y, size) -> QrLayer:
    return QrLayer(id="qr", name="QR code", x=x, y=y, size=size)


def _image(id, name, source, x, y, width, height, **more) -> ImageLayer:
    return ImageLayer(id=id, name=name, source=source, x=x, y=y, width=width, height=height, **more)


def blank_layout(kind: str) -> dict:
    """The canvas a custom ticket or certificate starts on: its page, the
    guest's name, and the QR with its code under it (a ticket must carry its
    QR; a certificate's opens its public check)."""
    if kind == "certificate":
        page: PageSpec = PAGE_PRESETS["a4_landscape"]["page"]
        W, H = canvas_for_page(page, 150)
        return {
            "page": page,
            "canvas": TicketCanvas(width=W, height=H, background="#FFFFFF"),
            "layers": [
                _text("guest", "Guest name", "{{attendee_name}}", 160, 480, W - 320, 110, font="dm_serif", align="center", max_lines=2, fit="shrink"),
                _text("no", "Certificate number", "{{certificate_no}}", 120, H - 110, 700, 22, font="mono", color="#8C918C"),
                _qr(W - 120 - 170, H - 120 - 170, 170),
            ],
        }
    W, H = 1560, 690
    return {
        "page": PAGE_PRESETS["ticket"]["page"],
        "canvas": TicketCanvas(width=W, height=H, background="#FFFFFF"),
        "layers": [
            _text("event", "Event name", "{{event_name}}", 72, 108, 960, 57, weight=700, max_lines=2),
            _text("guest", "Guest name", "{{attendee_name}}", 72, 288, 780, 45, weight=500),
            _text("code", "Ticket code", "{{ticket_code}}", 1140, 520, 390, 27, font="mono", color="#5A5F5B", align="center"),
            _qr(1185, 168, 300),
        ],
    }


# ─── ID cards ───────────────────────────────────────────────────────────────

BADGE_STARTERS = {
    "card_cr80": {"label": "Card (CR80)", "note": "For card printers: brand head, photo in a ring, first name large, type band with the QR"},
}


def badge_starter_page(kind: str = "card_cr80", current: Optional[PageSpec] = None) -> PageSpec:
    """The page the card is drawn for: a CR80 card, upright."""
    return PAGE_PRESETS["cr80_portrait"]["page"]


def badge_starter(kind: str = "card_cr80", *, seats: bool = False, page: Optional[PageSpec] = None) -> dict:
    """The CR80 ID card, upright at 300 dpi (638 × 1011 px). Pixel for pixel
    the TypeScript port (moa_web packages/layout/src/starters.ts)."""
    if kind not in BADGE_STARTERS:
        raise KeyError(kind)
    page = badge_starter_page(kind, page)
    W, H = canvas_for_page(page)
    canvas = TicketCanvas(width=W, height=H, background="#FFFFFF")
    M = 44
    first = "{{seat}}" if seats else "{{first_name}}"
    last = "{{last_name}}"
    org = "{% if organization %}{{organization}}{% endif %}"

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
        _text("code", "Ticket code", "{{ticket_code}}", M, band_y + 130, 330, 22, font="mono", color="#FFFFFF", opacity=0.85),
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
