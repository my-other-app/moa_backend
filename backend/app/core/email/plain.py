"""The plain, branded wrapper the account mails share (verification, password
reset): the brand on top, short paragraphs, at most one button, a grey line at
the foot. For mails that are a few sentences, not a designed template."""
import html

from app.config import settings

_WRAP = (
    '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
    'color:#2C333D;max-width:480px;margin:0 auto;padding:24px 16px">'
)
_BUTTON = (
    'display:inline-block;background:#2C333D;color:#ffffff;text-decoration:none;'
    'padding:10px 18px;border-radius:6px;font-weight:600'
)


def brand() -> str:
    return settings.MAIL_FROM_NAME or "MyOtherApp"


def render(paragraphs: list[str], *, button: tuple[str, str] | None = None, footer: str | None = None) -> str:
    """HTML for the mail. Every string is plain text and is escaped here."""
    esc = html.escape
    parts = [_WRAP, f'<p style="font-size:20px;font-weight:700;margin:0 0 24px">{esc(brand())}</p>']
    parts += [f"<p>{esc(p)}</p>" for p in paragraphs if p]
    if button:
        label, url = button
        parts.append(f'<p style="margin:24px 0"><a href="{esc(url, quote=True)}" style="{_BUTTON}">{esc(label)}</a></p>')
    if footer:
        parts.append(f'<p style="color:#6B7280;font-size:13px;margin-top:32px">{esc(footer)}</p>')
    parts.append("</div>")
    return "".join(parts)


def text(paragraphs: list[str], *, button: tuple[str, str] | None = None, footer: str | None = None) -> str:
    """The same mail as plain text."""
    lines = [p for p in paragraphs if p]
    if button:
        label, url = button
        lines.append(f"{label}: {url}")
    if footer:
        lines.append(footer)
    return "\n\n".join(lines) + "\n"
