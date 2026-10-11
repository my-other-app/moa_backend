"""One renderer for the editor's preview and the send, so what the designer
sees is what the guest receives. Output is the HTML email clients agree on
-- nested tables, every style inline, a 600 px card -- plus a plain text
twin for clients that ask for one.

A port of Muster's email-render.ts; the markup is kept identical so a
template designed there reads the same here.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from app.core.layout.tokens import parse_ticket_template, render_ticket_template as _render_tokens
from app.core.mail.schema import EMAIL_FONT_STACK, EMAIL_TOKEN_KEYS, EmailDesign, EmailTheme

# The card is fluid up to this width: `width="100%"` with `max-width` so a
# phone (or the editor's narrow column) wraps the text instead of scrolling
# sideways. Outlook ignores max-width, so a conditional wrapper hands it a
# fixed table.
CARD_WIDTH = 600


def render_tokens(src: str, values: dict) -> str:
    """Every ticket token plus the email-only ones."""
    return _render_tokens(src, values, set(EMAIL_TOKEN_KEYS))


def parse_email_template(src: str):
    """The editor's check of a piece of text: the errors for the person writing it."""
    return parse_ticket_template(src, set(EMAIL_TOKEN_KEYS))


@dataclass
class RenderInput:
    subject: str
    design: EmailDesign
    # Token values for this guest (ticket_token_values plus the email-only ones).
    values: dict
    # The club's primary colour, for a theme whose accent is 'brand'.
    brand_primary: str
    logo_url: Optional[str] = None
    event_icon_url: Optional[str] = None
    # Uploaded image key -> an address the recipient's client can fetch.
    images: dict = field(default_factory=dict)
    # The guest's QR, as an address or a cid: reference.
    qr_url: Optional[str] = None


@dataclass
class RenderedEmail:
    subject: str
    html: str
    text: str


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_HREF_OK = re.compile(r"^(https?://|mailto:|tel:)", re.I)


def safe_href(href: str) -> str:
    h = href.strip()
    return h if _HREF_OK.match(h) else ""


_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(^|[\s(])_([^_]+)_(?=[\s.,;:!?)]|$)")


def inline(src: str, values: dict, link_color: str) -> str:
    """Tokens first, then escaping, then the little inline markup: **bold**, _italic_, [label](url)."""
    rendered = _esc(render_tokens(src, values))

    def link(m):
        href = safe_href(m.group(2).replace("&amp;", "&"))
        return f'<a href="{_esc(href)}" style="color:{link_color};text-decoration:underline">{m.group(1)}</a>' if href else m.group(1)

    rendered = _LINK.sub(link, rendered)
    rendered = _BOLD.sub(r"<strong>\1</strong>", rendered)
    rendered = _ITALIC.sub(r"\1<em>\2</em>", rendered)
    return rendered.replace("\n", "<br />")


def plain(src: str, values: dict) -> str:
    out = render_tokens(src, values)
    out = _LINK.sub(lambda m: f"{m.group(1)} ({safe_href(m.group(2))})" if safe_href(m.group(2)) else m.group(1), out)
    out = _BOLD.sub(r"\1", out)
    out = _ITALIC.sub(r"\1\2", out)
    return out


def accent_of(theme: EmailTheme, brand_primary: str) -> str:
    return brand_primary if theme.accent == "brand" else theme.accent


def _cell(inner: str, style: str) -> str:
    return f'<tr><td style="{style}">{inner}</td></tr>'


def _block_html(b, inp: RenderInput) -> str:
    theme = inp.design.theme
    accent = accent_of(theme, inp.brand_primary)
    font = EMAIL_FONT_STACK[theme.font]
    s = b.style
    color = s.color or theme.text
    pad = f"padding:{s.padding_top}px 32px {s.padding_bottom}px 32px;"
    bg = f"background:{s.background};" if s.background else ""
    base = f"{pad}{bg}text-align:{s.align};font-family:{font};color:{color};letter-spacing:{s.letter_spacing:g}px;"
    v = inp.values
    kind = b.kind
    if kind == "heading":
        size = s.size or (30 if b.level == 1 else 22)
        return _cell(f'<div style="font-size:{size}px;line-height:1.2;font-weight:{s.weight or "700"};margin:0">{inline(b.text, v, accent)}</div>', base)
    if kind == "paragraph":
        return _cell(f'<div style="font-size:{s.size or 16}px;line-height:1.55;font-weight:{s.weight or "400"};margin:0">{inline(b.text, v, accent)}</div>', base)
    if kind == "list":
        tag = "ol" if b.ordered else "ul"
        items = "".join(f'<li style="margin:0 0 6px 0">{inline(i, v, accent)}</li>' for i in b.items)
        return _cell(f'<{tag} style="font-size:{s.size or 16}px;line-height:1.55;margin:0;padding-left:22px">{items}</{tag}>', base)
    if kind == "button":
        href = safe_href(render_tokens(b.href, v))
        primary = b.variant == "primary"
        btn_bg = (s.background or accent) if primary else theme.surface
        btn_color = (s.color or theme.accent_text) if primary else accent
        border = "" if primary else f"border:2px solid {accent};"
        label = inline(b.label, v, btn_color)
        button = (
            f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" style="display:inline-table"><tr><td style="background:{btn_bg};border-radius:{theme.radius}px;{border}">'
            f'<a href="{_esc(href or "#")}" style="display:inline-block;padding:14px 28px;font-family:{font};font-size:{s.size or 16}px;font-weight:{s.weight or "600"};color:{btn_color};text-decoration:none;letter-spacing:{s.letter_spacing:g}px">{label}</a></td></tr></table>'
        )
        return _cell(button, f"{pad}text-align:{s.align};")
    if kind == "image":
        src = (
            inp.logo_url if b.source == "org_logo"
            else inp.event_icon_url if b.source == "event_icon"
            else inp.qr_url if b.source == "qr"
            else inp.images.get(b.image_key) if b.image_key else None
        )
        if not src:
            return ""
        width = round((CARD_WIDTH - 64) * b.width / 100)
        radius = 8 if b.source == "qr" else theme.radius
        img = f'<img src="{_esc(src)}" alt="{_esc(b.alt)}" width="{width}" style="display:inline-block;width:{width}px;max-width:100%;height:auto;border:0;border-radius:{radius}px" />'
        href = safe_href(render_tokens(b.href, v)) if b.href else ""
        return _cell(f'<a href="{_esc(href)}" style="text-decoration:none">{img}</a>' if href else img, f"{pad}text-align:{s.align};")
    if kind == "divider":
        return _cell(f'<div style="height:1px;line-height:1px;font-size:1px;background:{b.color or "#E1E1DC"}">&nbsp;</div>', pad)
    if kind == "spacer":
        return _cell(f'<div style="height:{b.height}px;line-height:{b.height}px;font-size:1px">&nbsp;</div>', "padding:0 32px;")
    if kind == "details":
        rows = "".join(
            f'<tr><td style="padding:8px 0;border-bottom:1px solid #E9ECE6;font-family:{font};font-size:12px;letter-spacing:1px;text-transform:uppercase;color:{theme.muted};vertical-align:top;width:34%">{inline(r.label, v, accent)}</td>'
            f'<td style="padding:8px 0;border-bottom:1px solid #E9ECE6;font-family:{font};font-size:{s.size or 15}px;color:{color};vertical-align:top">{inline(r.value, v, accent)}</td></tr>'
            for r in b.rows
        )
        return _cell(f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">{rows}</table>', base)
    if kind == "ticket_card":
        qr = (
            f'<td style="width:132px;vertical-align:top;padding-left:20px"><img src="{_esc(inp.qr_url)}" alt="Your ticket QR code" width="132" style="display:block;width:132px;height:132px;border:0;background:#FFFFFF;border-radius:8px" /></td>'
            if b.show_qr and inp.qr_url else ""
        )

        def line(label: str, value: str) -> str:
            if not value:
                return ""
            return f'<div style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:{theme.muted};margin-top:10px">{_esc(label)}</div><div style="font-size:15px;color:{color}">{_esc(value)}</div>'

        # Rows chosen in the designer, or the four this card always showed.
        when = v.get("event_start") or f'{v.get("event_date", "")} · {v.get("event_time", "")}'
        body = (
            "".join(line(r.label, plain(r.value, v)) for r in b.rows)
            if b.rows
            else f'{line("Event", v.get("event_name", ""))}{line("When", when)}{line("Where", v.get("venue", "")) if b.show_venue else ""}{line("Admission", v.get("admission_no", ""))}'
        )
        eyebrow = plain(b.eyebrow, v)
        title = plain(b.title, v)
        card = (
            f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="border:1px solid #E1E1DC;border-radius:{theme.radius}px;border-collapse:separate"><tr><td style="padding:20px 22px;font-family:{font};vertical-align:top">'
            f'{f"<div style=&quot;font-size:12px;letter-spacing:2px;text-transform:uppercase;color:{accent};font-weight:700&quot;>{_esc(eyebrow)}</div>".replace("&quot;", chr(34)) if eyebrow else ""}'
            f'{f"<div style=&quot;font-size:20px;font-weight:700;color:{color};margin-top:4px&quot;>{_esc(title)}</div>".replace("&quot;", chr(34)) if title else ""}'
            f"{body}</td>{qr}</tr>"
            f'{f"<tr><td colspan=&quot;2&quot; style=&quot;padding:0 22px 18px 22px;font-family:{font};font-size:13px;color:{theme.muted}&quot;>{inline(b.note, v, accent)}</td></tr>".replace("&quot;", chr(34)) if b.note else ""}'
            "</table>"
        )
        return _cell(card, pad)
    return ""


def _block_text(b, inp: RenderInput) -> str:
    v = inp.values
    kind = b.kind
    if kind == "heading":
        return plain(b.text, v).upper()
    if kind == "paragraph":
        return plain(b.text, v)
    if kind == "list":
        return "\n".join(f"{f'{n + 1}.' if b.ordered else '•'} {plain(i, v)}" for n, i in enumerate(b.items))
    if kind == "button":
        href = safe_href(render_tokens(b.href, v))
        return f"{plain(b.label, v)}{f': {href}' if href else ''}"
    if kind == "image":
        return f"[{b.alt}]" if b.alt else ""
    if kind == "divider":
        return "—"
    if kind == "spacer":
        return ""
    if kind == "details":
        return "\n".join(f"{plain(r.label, v)}: {plain(r.value, v)}" for r in b.rows)
    if kind == "ticket_card":
        # The plain-text copy says the same as the card, chosen rows included --
        # a guest whose client shows no HTML reads this one.
        when = v.get("event_start") or f'{v.get("event_date", "")} · {v.get("event_time", "")}'
        rows = (
            [f"{r.label}: {plain(r.value, v)}" for r in b.rows]
            if b.rows
            else [v.get("event_name", ""), when, v.get("venue", "") if b.show_venue else "", f"Admission {v['admission_no']}" if v.get("admission_no") else ""]
        )
        return "\n".join(x for x in [plain(b.eyebrow, v), plain(b.title, v), *rows, plain(b.note, v) if b.note else ""] if x)
    return ""


def render_email(inp: RenderInput) -> RenderedEmail:
    theme = inp.design.theme
    blocks = inp.design.blocks
    font = EMAIL_FONT_STACK[theme.font]
    subject = re.sub(r"\s+", " ", plain(inp.subject, inp.values)).strip()
    logo = (
        _cell(f'<img src="{_esc(inp.logo_url)}" alt="{_esc(inp.values.get("org_name", ""))}" height="40" style="display:block;height:40px;width:auto;border:0" />', "padding:28px 32px 8px 32px;")
        if theme.logo == "org_logo" and inp.logo_url
        else _cell("", "padding:16px 0 0 0;font-size:1px;line-height:1px;")
    )
    body = "".join(_block_html(b, inp) for b in blocks)
    footer = (
        f'<tr><td style="padding:22px 32px 8px 32px;font-family:{font};font-size:12px;line-height:1.5;color:{theme.muted};text-align:center">{inline(theme.footer, inp.values, theme.muted)}</td></tr>'
        if theme.footer else ""
    )
    first_paragraph = next((b for b in blocks if b.kind == "paragraph"), None)
    preheader = _esc(plain(first_paragraph.text, inp.values)[:120] if first_paragraph else subject)
    html = (
        f'<!DOCTYPE html><html lang="en"><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" /><meta name="color-scheme" content="light only" /><title>{_esc(subject)}</title></head>'
        f'<body style="margin:0;padding:0;background:{theme.background};-webkit-text-size-adjust:100%">\n'
        f'<div style="display:none;max-height:0;overflow:hidden;font-size:1px;line-height:1px;color:{theme.background}">{preheader}</div>\n'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="background:{theme.background}"><tr><td align="center" style="padding:24px 12px">\n'
        f'<!--[if mso]><table role="presentation" width="{CARD_WIDTH}" cellspacing="0" cellpadding="0" border="0"><tr><td><![endif]-->\n'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:{CARD_WIDTH}px;background:{theme.surface};border-radius:{theme.radius}px;border-collapse:separate">{logo}{body}<tr><td style="padding:16px 0 0 0;font-size:1px;line-height:1px">&nbsp;</td></tr></table>\n'
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="width:100%;max-width:{CARD_WIDTH}px">{footer}</table>\n'
        "<!--[if mso]></td></tr></table><![endif]-->\n"
        "</td></tr></table>\n</body></html>"
    )
    parts = [t for t in (_block_text(b, inp) for b in blocks) if t.strip()]
    if theme.footer:
        parts.append(plain(theme.footer, inp.values))
    return RenderedEmail(subject=subject, html=html, text="\n\n".join(p for p in parts if p))


def uses_qr(design: EmailDesign) -> bool:
    return any((b.kind == "ticket_card" and b.show_qr) or (b.kind == "image" and b.source == "qr") for b in design.blocks)


def sample_email_values(values: dict, *, organiser_email: Optional[str] = None, api_origin: Optional[str] = None) -> dict:
    """The sample token values plus the email-only ones."""
    from app.core.mail.schema import EMAIL_ONLY_TOKENS

    out = dict(values)
    for t in EMAIL_ONLY_TOKENS:
        out[t["key"]] = t["sample"]
    out["organiser_email"] = organiser_email or ""
    out["event_start"] = f"{(out.get('event_day') or '')[:3]} {out.get('event_date', '')} · {out.get('event_time', '')}".strip()
    if api_origin:
        out["ticket_url"] = f"{api_origin}/api/v1/files/sample"
        out["certificate_url"] = f"{api_origin}/api/v1/files/sample"
    return out
