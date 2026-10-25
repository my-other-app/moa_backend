"""Mail's HTML, made safe to keep and to show.

What a mail's HTML may hold once it is kept: text formatting, tables, lists,
links, pictures and inline styles -- what newsletters and replies are made
of. Never a script, a form, an embed, an event handler, a `javascript:`
address or a meta refresh (nh3, Rust's ammonia, decides on an allowlist).

The dashboard shows it in a sandboxed iframe (no scripts, an opaque origin)
under a Content-Security-Policy of its own that blocks every remote load --
pictures, fonts, CSS imports -- until the reader asks to see the pictures.
So a remote picture is kept as the sender wrote it (`has_remote_images`
says there is one); a tracking pixel loads nothing unless asked. Links open
in a new tab, without the opener or the referrer.
"""
from __future__ import annotations

import re

import nh3

TAGS = frozenset({
    "a", "abbr", "address", "b", "big", "blockquote", "br", "caption", "center", "cite", "code", "col", "colgroup",
    "dd", "del", "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "font", "h1", "h2", "h3", "h4",
    "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "samp", "small", "span",
    "strike", "strong", "style", "sub", "summary", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time",
    "tr", "tt", "u", "ul", "var", "wbr",
})
_COMMON = {"style", "class", "id", "title", "dir", "lang", "align", "valign", "width", "height", "bgcolor", "color"}
ATTRIBUTES = {
    "*": _COMMON,
    "a": {"href", "name", "target"},
    "img": {"src", "alt", "border", "hspace", "vspace"},
    "table": {"border", "cellpadding", "cellspacing", "summary"},
    "td": {"colspan", "rowspan", "nowrap", "background"},
    "th": {"colspan", "rowspan", "nowrap", "scope", "background"},
    "col": {"span"},
    "colgroup": {"span"},
    "font": {"face", "size"},
    "ol": {"start", "type", "reversed"},
    "ul": {"type"},
    "li": {"value"},
    "blockquote": {"cite"},
    "q": {"cite"},
    "time": {"datetime"},
    "details": {"open"},
}
#: Schemes a link or a picture may use. `cid:` names a picture the mail carries.
SCHEMES = {"http", "https", "mailto", "tel", "cid", "data"}
MAX_HTML = 2_000_000

_REMOTE = re.compile(r"""(?:\bsrc\s*=\s*["']?\s*https?:|\bbackground\s*=\s*["']?\s*https?:|url\(\s*["']?\s*(?:https?:)?//)""", re.I)
_DATA_URL = re.compile(r"^data:image/(?:png|jpe?g|gif|webp);base64,", re.I)


def _attribute(element: str, attribute: str, value: str):
    """Last word on each attribute: a data: address only for a picture of a
    plain kind, a link always opening a new tab, ids and classes namespaced
    so they cannot reach the page around them."""
    if attribute in ("src", "background") and value.strip().lower().startswith("data:"):
        return value if element == "img" and _DATA_URL.match(value.strip()) else None
    if attribute == "href" and value.strip().lower().startswith(("data:", "cid:")):
        return None
    if attribute == "target":
        return "_blank"
    if attribute in ("id", "name"):
        return f"m-{value}"[:120]
    return value


def clean(html: str) -> str:
    """The HTML with nothing in it that runs."""
    if not html:
        return ""
    return nh3.clean(
        html[:MAX_HTML],
        tags=set(TAGS),
        clean_content_tags={"script", "noscript", "iframe", "object", "embed", "template", "textarea", "select", "title"},
        attributes={k: set(v) for k, v in ATTRIBUTES.items()},
        attribute_filter=_attribute,
        url_schemes=set(SCHEMES),
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def has_remote_images(html: str) -> bool:
    """Whether showing it would load something from elsewhere: a picture, a
    background, a stylesheet's url(). The reader decides."""
    return bool(html and _REMOTE.search(html))


def to_text(html: str, limit: int = 20_000) -> str:
    """Its words alone, for a snippet and the search (a mail with no text part)."""
    if not html:
        return ""
    bare = nh3.clean(html[:MAX_HTML], tags=set(), clean_content_tags={"script", "style", "head", "title", "noscript"})
    import html as html_lib

    text = html_lib.unescape(bare)
    return re.sub(r"[ \t\r\f\v]*\n[ \t\r\f\v\n]*", "\n", re.sub(r"[ \t\r\f\v]+", " ", text)).strip()[:limit]


def snippet(text: str, limit: int = 160) -> str:
    """The first words, on one line, without what was quoted from before."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip() and not ln.lstrip().startswith(">")]
    words = " ".join(" ".join(lines).split())
    return words[:limit]


def from_text(text: str) -> str:
    """A plain message as HTML, for the HTML part of what the club sends:
    escaped, paragraphs and lines kept, links made links."""
    import html as html_lib

    escaped = html_lib.escape(text or "")
    linked = re.sub(r"(https?://[^\s<>&\"']+)", r'<a href="\1">\1</a>', escaped)
    body = "<br>\n".join(linked.split("\n"))
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        f'font-size:14px;line-height:1.5;color:#1f2933">{body}</div>'
    )
