"""A demo club's ticket and certificate, in its own brand, built with the
platform's layered designs: the same canvas, layers and page the clubs
dashboard's Templates page saves (app/core/layout/model.py), saved through
the same function (`club_designs.router.save_layout`), so every ticket and
certificate of every event of the club is drawn in it.

The ticket (1560 x 690, eight inches wide) is a boarding-pass: the brand's
artwork across the body, the club's mark and name, the event in the
display font, the guest, when and where, the admission number, and a
torn-off stub on paper with the QR and the ticket type. The certificate (A4
landscape) is paper with a band of the brand down the left carrying the
mark, the title in the display font, the guest's name large, and two of the
club's people signing.

Colours are written as hex rather than `brand.primary`: the certificate
renderer resolves those from the template, not the club, and the ticket
reads the same either way.
"""

from __future__ import annotations

from typing import Callable

from scripts.demo import images

FONT_IDS = {"bebas": "bebas", "dm-serif": "dm_serif", "inter": "inter"}

TICKET_W, TICKET_H = 1560, 690
CERT_W, CERT_H = 1754, 1240


def _hex(c) -> str:
    return "#{:02X}{:02X}{:02X}".format(*c[:3])


def _display(brand: images.Brand) -> tuple[str, int]:
    """The layout font id and its one weight for the brand's display face."""
    font = FONT_IDS.get(brand.display_font, "inter")
    return font, (700 if font == "inter" else 400)


def ticket_layout(brand, *, art_key: str, mark_key: str) -> dict:
    """The ticket's wire form, ready for save_layout."""
    b = images.Brand.of(brand)
    W, H = TICKET_W, TICKET_H
    font, weight = _display(b)
    deep = images.mix(b.p, (0, 0, 0), 0.25)
    light = images.on(deep, b.w, (255, 255, 255))
    soft = images.mix(light, deep, 0.3)
    accent = b.a if images.contrast(deep, b.a) >= 2.2 else light
    paper = b.w if images.luminance(b.w) > 0.8 else (255, 255, 255)
    stub_ink = images.on(paper, b.i, (20, 20, 20))
    STUB = 1140
    event_size = {"bebas": 96, "dm_serif": 66, "inter": 58}[font]

    def text(id, name, t, x, y, width, size, **more):
        return {"kind": "text", "id": id, "name": name, "text": t, "x": x, "y": y, "width": width, "size": size, **more}

    def shape(id, name, x, y, width, height, **more):
        return {"kind": "shape", "id": id, "name": name, "x": x, "y": y, "width": width, "height": height, **more}

    layers = [
        # The stub: paper, a band of the brand at its foot, a dotted tear line.
        shape("stub", "Stub", STUB, 0, W - STUB, H, fill=_hex(paper)),
        shape("stub-band", "Stub band", STUB, H - 18, W - STUB, 18, fill=_hex(b.p)),
        *[shape(f"tear-{i}", "Tear line", STUB - 3, 34 + i * 32, 6, 16, fill=_hex(paper), radius=3, opacity=0.55) for i in range(19)],
        # The head: the club's mark and name.
        {"kind": "image", "id": "mark", "name": "Club mark", "x": 72, "y": 52, "width": 92, "height": 92,
         "source": "upload", "imageKey": mark_key, "fit": "contain"},
        text("club", "Club", "{{org_name | upper}}", 186, 84, 820, 26, font="mono", weight=700, color=_hex(accent), letterSpacing=3),
        # The event, the guest, when and where.
        text("event", "Event name", "{{event_name | upper}}" if font == "bebas" else "{{event_name}}", 72, 172, 1000,
             event_size, font=font, weight=weight, color=_hex(light), maxLines=2, lineSpacing=-6 if font == "bebas" else 0),
        text("guest-label", "Guest label", "ADMIT ONE", 72, 400, 400, 20, font="mono", weight=700, color=_hex(soft), letterSpacing=6),
        text("guest", "Guest name", "{{attendee_name}}", 72, 428, 1000, 48, font="google_sans", weight=600, color=_hex(light)),
        text("when", "Date & time", "{{event_day | upper}} · {{event_date | upper}} · {{event_time}}", 72, 512, 1000, 26,
             font="mono", weight=400, color=_hex(light), letterSpacing=1),
        text("venue", "Venue", "{% if venue %}{{venue}}{% else %}Online{% endif %}", 72, 552, 1000, 26, font="google_sans",
             weight=400, color=_hex(soft)),
        text("adm-label", "Admission label", "NO.", 72, 616, 80, 20, font="mono", weight=700, color=_hex(accent), letterSpacing=4),
        text("adm", "Admission number", "{{admission_no}}", 130, 610, 600, 28, font="mono", weight=700, color=_hex(light)),
        # The stub's content: the QR on paper, the ticket type, the code.
        text("type", "Ticket type", "{{ticket_type | upper}}", STUB + 40, 56, W - STUB - 80, 24, font="mono", weight=700,
             color=_hex(b.p if images.contrast(paper, b.p) > 3 else stub_ink), align="center", letterSpacing=5),
        {"kind": "qr", "id": "qr", "name": "QR code", "x": STUB + (W - STUB - 300) / 2, "y": 120, "size": 300,
         "dark": _hex(images.mix(stub_ink, (0, 0, 0), 0.5)), "light": _hex(paper)},
        text("code", "Ticket code", "{{ticket_code}}", STUB + 30, 450, W - STUB - 60, 24, font="mono", weight=700,
             color=_hex(stub_ink), align="center"),
        text("scan", "Scan line", "Show this at the door", STUB + 30, 492, W - STUB - 60, 20, font="google_sans",
             weight=400, color=_hex(images.mix(stub_ink, paper, 0.4)), align="center"),
    ]
    return {
        "canvas": {"width": W, "height": H, "background": _hex(deep), "templateKey": art_key},
        "page": {"widthMm": 203.2, "heightMm": None},
        "layers": layers,
    }


def certificate_layout(brand, *, art_key: str, mark_key: str, signers: list[tuple[str, str]], kind: str = "PARTICIPATION") -> dict:
    """The certificate's wire form: `signers` are (name, role), one or two."""
    b = images.Brand.of(brand)
    W, H = CERT_W, CERT_H
    font, weight = _display(b)
    paper = b.w if images.luminance(b.w) > 0.7 else (250, 248, 243)
    ink = images.on(paper, b.i, (20, 20, 20))
    grey = images.mix(ink, paper, 0.42)
    primary = b.p if images.contrast(paper, b.p) >= 3 else ink
    accent = b.a if images.contrast(paper, b.a) >= 2 else primary
    band = int(W * 0.16)
    x0 = band + 120
    width = W - x0 - 140
    on_band = images.on(b.p, b.w, (255, 255, 255))
    title_size = {"bebas": 150, "dm_serif": 104, "inter": 92}[font]

    def text(id, name, t, x, y, w, size, **more):
        return {"kind": "text", "id": id, "name": name, "text": t, "x": x, "y": y, "width": w, "size": size, **more}

    layers: list[dict] = [
        {"kind": "image", "id": "mark", "name": "Club mark", "x": (band - 170) / 2, "y": 110, "width": 170, "height": 170,
         "source": "upload", "imageKey": mark_key, "fit": "contain"},
        text("club", "Club", "{{org_name | upper}}", 24, 310, band - 48, 22, font="mono", weight=700, color=_hex(on_band),
             align="center", maxLines=4, letterSpacing=2, lineSpacing=8),
        text("title", "Title", "Certificate" if font != "bebas" else "CERTIFICATE", x0, 150, width, title_size, font=font,
             weight=weight, color=_hex(primary)),
        text("kind", "Kind", f"OF {kind}", x0 + 6, 150 + int(title_size * 1.12), width, 28, font="mono", weight=700,
             color=_hex(accent), letterSpacing=10),
        text("lead", "Lead line", "This is proudly presented to", x0, 430, width, 32, font="google_sans", weight=400, color=_hex(grey)),
        text("guest", "Guest name", "{{attendee_name}}", x0, 480, width, 112, font="dm_serif", weight=400, color=_hex(ink),
             maxLines=1, fit="shrink"),
        {"kind": "shape", "id": "rule", "name": "Name rule", "x": x0, "y": 640, "width": 560, "height": 5, "fill": _hex(accent), "radius": 2},
        text("body", "Body", "for taking part in {{event_name}} on {{event_date}}{% if venue %}, at {{venue}}{% endif %}, "
             "organised by {{org_name}}.", x0, 690, width - 120, 34, font="google_sans", weight=400, color=_hex(ink), maxLines=3, lineSpacing=10),
    ]
    sx = x0
    for i, (name, role) in enumerate(signers[:2]):
        layers += [
            {"kind": "shape", "id": f"sign-line-{i}", "name": "Signature line", "x": sx, "y": 990, "width": 420, "height": 2, "fill": _hex(grey)},
            text(f"sign-name-{i}", "Signer", name, sx, 1004, 420, 28, font="google_sans", weight=600, color=_hex(ink)),
            text(f"sign-role-{i}", "Signer's role", role, sx, 1044, 420, 22, font="google_sans", weight=400, color=_hex(grey)),
            text(f"sign-{i}", "Signature", name.split()[0], sx + 10, 905, 400, 64, font="dm_serif", weight=400,
                 color=_hex(images.mix(primary, paper, 0.15)), opacity=0.9),
        ]
        sx += 500
    layers += [
        text("no", "Certificate number", "No. {{certificate_no}} · Issued {{issued_date}}", x0, H - 120, 900, 20, font="mono",
             weight=400, color=_hex(grey), letterSpacing=1),
        {"kind": "qr", "id": "qr", "name": "QR code", "x": W - 140 - 170, "y": H - 120 - 170 - 10, "size": 170,
         "dark": _hex(images.mix(ink, (0, 0, 0), 0.4)), "light": _hex(paper)},
    ]
    return {
        "canvas": {"width": W, "height": H, "background": _hex(paper), "templateKey": art_key},
        "page": {"widthMm": 297, "heightMm": 210},
        "layers": layers,
    }


def artwork(brand, slug: str, name: str) -> dict[str, bytes]:
    """The pictures the two designs carry: the ticket's and the
    certificate's backgrounds and the club's mark (a circle, transparent)."""
    import io

    b = images.Brand.of(brand)
    mark = images.mark(b, name, slug, 512)
    out = io.BytesIO()
    mark.save(out, format="PNG", optimize=True)
    return {
        "ticket": images.ticket_art(b, slug, size=(TICKET_W, TICKET_H)),
        "certificate": images.certificate_art(b, slug, size=(CERT_W, CERT_H)),
        "mark": out.getvalue(),
    }


async def save_designs(session, *, club, owner, brand, signers: list[tuple[str, str]], store: Callable[[bytes, str, str], dict]) -> dict:
    """Upload the artwork under the club's designs folder and save both
    designs as the club's defaults, through the dashboard's own save."""
    from app.api.club_designs import router as club_designs
    from app.api.event_designs.router import DesignKind, _club_layout_prefix

    prefix = _club_layout_prefix(club.id)
    art = artwork(brand, club.slug, club.name)
    keys = {
        "ticket": store(art["ticket"], prefix, "template")["key"],
        "certificate": store(art["certificate"], prefix, "template")["key"],
        "mark": store(art["mark"], prefix, "image")["key"],
    }
    ticket = await club_designs.save_layout(
        session=session, user=owner, kind=DesignKind.ticket,
        body=ticket_layout(brand, art_key=keys["ticket"], mark_key=keys["mark"]),
    )
    certificate = await club_designs.save_layout(
        session=session, user=owner, kind=DesignKind.certificate,
        body=certificate_layout(brand, art_key=keys["certificate"], mark_key=keys["mark"], signers=signers),
    )
    return {"ticket": ticket, "certificate": certificate, "keys": keys}
