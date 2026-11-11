"""What the desk does to words: card numbers masked, the case number in a
subject, and the team's markdown-lite made into a mail's HTML."""
from __future__ import annotations

import html as html_lib
import re
from typing import Optional

# 13 to 19 digits, spaces or dashes between them allowed.
_CARDISH = re.compile(r"(?<![\d])(?:\d[ -]?){12,18}\d(?![\d])")
_NUMBER = re.compile(r"\[#(\d{1,12})\]")
_REPLY = re.compile(r"^\s*(?:(?:re|fwd?|fw|aw|sv)\s*:\s*)+", re.I)
_WS = re.compile(r"\s+")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def mask_cards(text: Optional[str]) -> str:
    """Every card-like number (13-19 digits that pass the Luhn check) as
    `**** **** **** 1234`: the help form warns against sending them, and
    some people will anyway."""
    if not text:
        return text or ""

    def repl(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if not (13 <= len(digits) <= 19) or not _luhn(digits):
            return match.group(0)
        return "**** **** **** " + digits[-4:]

    return _CARDISH.sub(repl, text)


def tag(number: int) -> str:
    return f"[#{int(number)}]"


def number_in(subject: Optional[str]) -> Optional[int]:
    """The case number a subject carries, `[#1042]`, or None."""
    found = _NUMBER.search(subject or "")
    return int(found.group(1)) if found else None


def subject_key(subject: Optional[str]) -> str:
    """A subject as two mails about one thing share it: without Re:/Fwd:,
    without the case tag, in lower case, single spaces."""
    value = _NUMBER.sub(" ", subject or "")
    value = _REPLY.sub("", value)
    return _WS.sub(" ", value).strip().lower()[:200]


def mail_subject(number: int, subject: Optional[str], *, reply: bool = True) -> str:
    """`Re: [#1042] Is parking free?` -- the tag the inbound path joins by."""
    base = _REPLY.sub("", _NUMBER.sub("", subject or "")).strip() or "Your request"
    return f"{'Re: ' if reply else ''}{tag(number)} {base}"[:300]


def first_words(text: Optional[str], limit: int = 80) -> str:
    words = _WS.sub(" ", text or "").strip()
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"


# ─── Markdown-lite ───────────────────────────────────────────────────────

_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s)]{1,2000})\)")
_BARE = re.compile(r"(?<![\"'=>])(https?://[^\s<>\"']{1,2000})")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![\w*])[*_](?=\S)(.+?)(?<=\S)[*_](?![\w*])")
_CODE = re.compile(r"`([^`\n]{1,500})`")
_BULLET = re.compile(r"^\s*[-*] ")
WRAP = (
    '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
    'font-size:14px;line-height:1.55;color:#1f2933;max-width:600px">'
)


def _inline(line: str) -> str:
    out = html_lib.escape(line, quote=False)
    links: list[str] = []

    def keep(markup: str) -> str:
        links.append(markup)
        return f"\x00{len(links) - 1}\x00"

    out = _LINK.sub(lambda m: keep(f'<a href="{html_lib.escape(html_lib.unescape(m.group(2)), quote=True)}">{m.group(1)}</a>'), out)
    out = _BARE.sub(lambda m: keep(f'<a href="{html_lib.escape(html_lib.unescape(m.group(1)), quote=True)}">{m.group(1)}</a>'), out)
    out = _CODE.sub(r"<code>\1</code>", out)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], out)


def markdown_lite(text: Optional[str]) -> str:
    """**bold**, *italic*, `code`, [words](https://link), bare links, `- `
    lists, paragraphs and line breaks -- and nothing else: every other
    character is escaped, so what the team types can't become markup."""
    blocks = re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip())
    parts: list[str] = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if all(_BULLET.match(ln) for ln in lines):
            items = "".join("<li>" + _inline(_BULLET.sub("", ln)) + "</li>" for ln in lines)
            parts.append(f'<ul style="margin:0 0 12px;padding-left:20px">{items}</ul>')
        else:
            parts.append('<p style="margin:0 0 12px">' + "<br>".join(_inline(ln) for ln in lines) + "</p>")
    return WRAP + "".join(parts) + "</div>"


def plain(text: Optional[str]) -> str:
    """The markdown-lite as the mail's text part: the marks taken off the
    emphasis, a link as "words (address)"."""
    value = (text or "").replace("\r\n", "\n").strip()
    value = _LINK.sub(r"\1 (\2)", value)
    value = _BOLD.sub(r"\1", value)
    value = _CODE.sub(r"\1", value)
    return value + "\n"


def fill(body: str, **values) -> str:
    """A saved reply's placeholders, {name} and {number}; others left as typed."""
    def repl(m: re.Match) -> str:
        key = m.group(1)
        return str(values[key]) if key in values and values[key] is not None else m.group(0)

    return re.sub(r"\{(name|number|subject)\}", repl, body or "")
