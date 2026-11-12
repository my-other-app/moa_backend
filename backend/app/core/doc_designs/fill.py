"""Filling a design: our values into the designer's tokens (contract, D4-D6).

`fill(design_id, values, options, bindings, accent)` returns a complete HTML
page for the render service. Every text value is HTML-escaped; images must be
data: URIs (D1: a design never makes the renderer fetch anything); every
token is replaced, or the block that holds it is left out (or, where a grid
needs its shape, hidden in place); and a `{{` or a designer's `[PLACEHOLDER]`
left over raises rather than prints.

**values** -- token -> value, from B's one mapping per kind (D5). An empty
or missing value is "we don't have it". Besides the tokens:

- `qr`: the text to encode (a ticket's code; a certificate's verify URL).
  The design's QR token may instead carry a ready data: URI.
- `signature_src`, `second_signature_src`: signature images (data: URIs)
  for the design's first and second signature lines.
- `field.<slug>`: a registration answer, for a binding with source "field".

**bindings** -- for an optional token, `{"source": "auto"|"text"|"field"|
"hidden", "value": ...}`: auto takes values[token], text the fixed `value`,
field values["field.<value>"], hidden nothing. **options** -- the design's
option values (`DesignInfo.options`; defaults fill the gaps). **accent** --
"#RRGGBB" or None for the design's default.

`preview_html(...)` is the same page for a browser: the fonts are links to
/static/doc-fonts/ instead of inline files. `validate(...)` lists what is
wrong with a saved choice before it is saved. `fingerprint(...)` is what the
document cache keys a render on (D8).
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional

from app.core.doc_designs import designs, fonts, qr
from app.core.doc_designs.designs import DesignInfo
from app.core.doc_designs.compile import TOKEN, Block, Tok, compile_fragment, text_nodes, tokens_in
from app.core.doc_designs.errors import BadValue, DesignError, LeftoverToken, MissingValue

HERE = Path(__file__).resolve().parent

#: Tokens whose value is an image source (always a data: URI).
IMAGE_TOKENS = frozenset({"qr_code_src", "qr_code_url", "logo_src", "photo_url", "sponsor_src"})
#: Images that are not tokens: the signature lines' pictures, in order.
SIGNATURE_KEYS = ("signature_src", "second_signature_src")
_DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$")
_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_FIELD = re.compile(r"^[a-z0-9_][a-z0-9_.-]{0,79}$")
#: The designers' placeholders, which must never print.
_PLACEHOLDERS = re.compile(r"\[(QR CODE|SPONSOR LOGOS)\]")
SOURCES = ("auto", "text", "field", "hidden")
MAX_TEXT = 200


@lru_cache(maxsize=1)
def _runtime() -> tuple[str, str]:
    """The fit script and the base styles injected into every page."""
    return (HERE / "fit.js").read_text(), (HERE / "base.css").read_text()


def runtime_digest() -> str:
    script, css = _runtime()
    code = (HERE / "fill.py").read_bytes() + (HERE / "compile.py").read_bytes()
    return hashlib.sha256(script.encode() + css.encode() + code).hexdigest()[:16]


def _esc(text: str) -> str:
    """HTML-escaped, braces too: a value can never read as a token."""
    return _html.escape(text, quote=True).replace("{", "&#123;").replace("}", "&#125;")


def _text(value: Any) -> str:
    """A value as one line of plain text."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _initials(values: Mapping[str, Any]) -> str:
    given = _text(values.get("initials"))
    if given:
        return given[:3].upper()
    first, last = _text(values.get("first_name")), _text(values.get("last_name"))
    if not (first or last):
        parts = _text(values.get("full_name")).split()
        first, last = (parts[0], parts[-1]) if len(parts) > 1 else (parts[0] if parts else "", "")
    return ((first[:1] + last[:1]) or "?").upper()


class _Context:
    def __init__(self, d: DesignInfo, values, options, bindings, accent):
        self.d = d
        self.values = dict(values or {})
        self.bindings = dict(bindings or {})
        self.options = {**d.option_defaults(), **(options or {})}
        self.accent = (accent or (d.accent.default if d.accent else None) or "").upper() or None
        if self.accent and not _HEX.match(self.accent):
            raise BadValue(f"accent must be #RRGGBB, not {accent!r}")
        self.qr_token = d.qr["token"]

    def raw(self, name: str) -> str:
        """A token's value, after its binding, as text ("" for none)."""
        if name == "accent_color":
            return self.accent or ""
        if name == self.qr_token:
            given = _text(self.values.get(name))
            if given:
                return given
            payload = _text(self.values.get("qr"))
            return qr.data_uri(payload, self.d.qr.get("ecc", "M"), int(self.d.qr.get("border", 0))) if payload else ""
        b = self.bindings.get(name) or {}
        source = b.get("source", "auto")
        if source == "hidden":
            return ""
        if source == "text":
            return _text(b.get("value"))
        if source == "field":
            return _text(self.values.get(f"field.{b.get('value')}"))
        if name == "initials":
            return _initials(self.values)
        return _text(self.values.get(name))

    def out(self, name: str, allow_empty: bool) -> str:
        value = self.raw(name)
        if not value:
            if allow_empty:
                return ""
            raise MissingValue(f"{self.d.id}: no value for {name}")
        if name in IMAGE_TOKENS:
            if not _DATA_IMAGE.match(value):
                raise BadValue(f"{name} must be a data:image/...;base64 URI")
            return value
        return _esc(value)

    def present(self, name: str) -> bool:
        return bool(self.raw(name))


def _render(nodes: list, ctx: _Context, allow_empty: bool = False) -> str:
    out: list[str] = []
    for n in nodes:
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, Tok):
            out.append(ctx.out(n.name, allow_empty))
        else:
            out.append(_block(n, ctx, allow_empty))
    return "".join(out)


def _block(b: Block, ctx: _Context, allow_empty: bool) -> str:
    if b.kind == "optional":
        show = all(ctx.present(t) for t in b.need_all) and (not b.need_any or any(ctx.present(t) for t in b.need_any))
        if show:
            return _render(b.open, ctx, allow_empty) + _render(b.children, ctx, allow_empty) + b.close
        if b.hide:
            return _render(b.hidden_open, ctx, True) + _render(b.children, ctx, True) + b.close
        return ""
    if b.kind == "option":
        nodes = _wording(ctx, b.slot)
        return _render(b.open, ctx, allow_empty) + _render(nodes if nodes is not None else b.children, ctx, allow_empty) + b.close
    if b.kind == "lanyard":
        open_nodes = b.open if ctx.options.get("lanyard", True) else b.hidden_open
        return _render(open_nodes, ctx, allow_empty) + _render(b.children, ctx, allow_empty) + b.close
    if b.kind == "signature":
        key = SIGNATURE_KEYS[int(b.slot)] if int(b.slot) < len(SIGNATURE_KEYS) else None
        src = _text(ctx.values.get(key)) if key else ""
        inner = _render(b.children, ctx, allow_empty)
        if src:
            if not _DATA_IMAGE.match(src):
                raise BadValue(f"{key} must be a data:image/...;base64 URI")
            inner = (f'<img src="{src}" alt="" style="display: block; width: 100%; height: 100%; '
                     'object-fit: contain; object-position: center bottom">')
        return _render(b.open, ctx, allow_empty) + inner + b.close
    if b.kind == "photo":
        if ctx.present("photo_url"):
            return _render(b.open, ctx, allow_empty)
        return _initials_tile(ctx)
    return _render(b.open, ctx, allow_empty) + _render(b.children, ctx, allow_empty) + b.close


def _initials_tile(ctx: _Context) -> str:
    """No photo: the person's initials in the frame, never an empty frame (D5)."""
    style = ctx.d.raw.get("initials") or {}
    return (
        '<div class="moa-initials" style="width: 100%; height: 100%; container-type: size; display: flex; '
        f"align-items: center; justify-content: center; color: {style.get('color', '#0E1330')}; "
        f"background: {style.get('background', 'transparent')}; font-family: {style.get('font', 'inherit')}; "
        f"font-weight: {style.get('weight', 700)}\"><span style=\"font-size: 42cqmin; line-height: 1; "
        f'letter-spacing: -0.02em">{_esc(_initials(ctx.values))}</span></div>'
    )


def _wording(ctx: _Context, slot: str) -> Optional[list]:
    custom = (ctx.options.get("text") or {}).get(slot)
    if custom:
        return _custom_nodes(ctx.d.id, _text(custom)[:MAX_TEXT])
    key = ctx.options.get("wording")
    return designs.wording(ctx.d.id, key, slot) if key else None


@lru_cache(maxsize=512)
def _custom_nodes(design_id: str, text: str) -> list:
    """A club's own words for a line: plain text, with the design's tokens."""
    nodes: list = []
    for piece in text_nodes(text):
        nodes.append(piece if isinstance(piece, Tok) else _esc(piece))
    return nodes


# ─── The page ────────────────────────────────────────────────────────────────


def _page_css(d: DesignInfo) -> str:
    p = d.page
    return (f".moa-page{{width:{p.width_px}px;height:{p.height_px}px}}"
            f"@media print{{.moa-page{{width:{p.width_mm:g}mm;height:{p.height_mm:g}mm}}}}")


def _document(d: DesignInfo, body: str, font_css: str, accent_var: Optional[str]) -> str:
    c = designs.compiled(d.id)
    script, base = _runtime()
    page_style = f' style="--accent:{accent_var}"' if accent_var else ""
    styles = "".join(f"<style>{s}</style>\n" for s in c.styles)
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        f'<meta name="moa-design" content="{d.id}">\n'
        f'<meta name="moa-page" content="{d.page.paper[0]} {d.page.paper[1]}">\n'
        f"<title>{c.title}</title>\n"
        "<!--moa:head-->\n"
        f'<style data-moa="fonts">\n{font_css}\n</style>\n'
        f"{styles}"
        f'<style data-moa="base">{base}{_page_css(d)}</style>\n'
        "<!--/moa:head-->\n"
        f'<script data-moa="fit">{script}</script>\n'
        "</head>\n<body>\n"
        f'<!--moa:page--><div class="moa-page"{page_style}>\n{body}\n</div><!--/moa:page-->\n'
        "</body>\n</html>\n"
    )


def _fill(design_id: str, values, options, bindings, accent, *, browser_base: Optional[str]) -> str:
    d = designs.get(design_id)
    ctx = _Context(d, values, options, bindings, accent)
    body = _render(designs.compiled(d.id).body, ctx)
    accent_var = ctx.accent if d.accent and d.accent.mode == "var" else None
    if browser_base is None:
        font_css = fonts.for_render(d.fonts, body)
    else:
        font_css = fonts.for_browser(d.fonts, browser_base)
    page = _document(d, body, font_css, accent_var)
    if "{{" in body or "{{" in page:
        raise LeftoverToken(f"{d.id}: a token is left in the page")
    if _PLACEHOLDERS.search(body):
        raise LeftoverToken(f"{d.id}: a designer's placeholder is left in the page")
    return page


def fill(design_id: str, values: Mapping[str, Any], options: Optional[Mapping] = None,
         bindings: Optional[Mapping] = None, accent: Optional[str] = None) -> str:
    """The complete page for the render service (fonts inline)."""
    return _fill(design_id, values, options, bindings, accent, browser_base=None)


def preview_html(design_id: str, values: Mapping[str, Any], options: Optional[Mapping] = None,
                 bindings: Optional[Mapping] = None, accent: Optional[str] = None, *,
                 base_url: Optional[str] = None) -> str:
    """The same page for a browser (an editor's srcdoc iframe): the fonts are
    loaded from `base_url`/static/doc-fonts/ (default: APP_PUBLIC_API_URL)."""
    if base_url is None:
        from app.config import settings

        base_url = settings.PUBLIC_API_URL
    return _fill(design_id, values, options, bindings, accent, browser_base=base_url)


# ─── Checking a choice before it is saved ────────────────────────────────────


def _luminance(hex_colour: str) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio of two #RRGGBB colours (1 to 21)."""
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _problem(code: str, field: str, message: str, level: str = "error", **extra) -> dict:
    return {"code": code, "field": field, "message": message, "level": level, **extra}


def validate(design_id: str, options: Optional[Mapping] = None, bindings: Optional[Mapping] = None,
             accent: Optional[str] = None) -> list[dict]:
    """What is wrong with a design choice, as problems: {code, field,
    message, level}. "error"s must be fixed before saving; a "warning" (an
    accent with low contrast) is shown and may be saved."""
    try:
        d = designs.get(design_id)
    except DesignError as exc:
        return [_problem("unknown_design", "id", str(exc))]
    out: list[dict] = []
    options = dict(options or {})
    for name, value in options.items():
        spec = d.options.get(name)
        if spec is None:
            out.append(_problem("unknown_option", f"options.{name}", f"{d.name} has no option {name}"))
        elif spec["type"] == "bool" and not isinstance(value, bool):
            out.append(_problem("bad_option", f"options.{name}", f"{name} must be true or false"))
        elif spec["type"] == "choice" and value not in spec["choices"]:
            out.append(_problem("bad_option", f"options.{name}", f"{name} must be one of {', '.join(spec['choices'])}"))
        elif spec["type"] == "text":
            if not isinstance(value, dict):
                out.append(_problem("bad_option", f"options.{name}", "text must map a line to its words"))
                continue
            for slot, words in value.items():
                field = f"options.text.{slot}"
                if slot not in spec["slots"]:
                    out.append(_problem("unknown_slot", field, f"{d.name} has no line called {slot}"))
                elif not isinstance(words, str) or len(words) > spec["max_length"]:
                    out.append(_problem("bad_text", field, f"at most {spec['max_length']} characters"))
                else:
                    unknown = set(TOKEN.findall(words)) - set(d.tokens)
                    if unknown:
                        out.append(_problem("unknown_token", field, f"{d.name} has no value {', '.join(sorted(unknown))}"))
    for token, b in (bindings or {}).items():
        field = f"bindings.{token}"
        if token not in d.tokens:
            out.append(_problem("unknown_token", field, f"{d.name} does not print {token}"))
            continue
        if not isinstance(b, dict) or b.get("source") not in SOURCES:
            out.append(_problem("bad_binding", field, f"source must be one of {', '.join(SOURCES)}"))
            continue
        source, value = b["source"], b.get("value")
        if token in IMAGE_TOKENS or token in ("accent_color", d.qr["token"]):
            if source != "auto":
                out.append(_problem("not_bindable", field, f"{token} is set by MyOtherApp"))
        elif source == "hidden" and d.tokens[token] == "required":
            out.append(_problem("required", field, f"{d.name} must print {token}"))
        elif source == "text" and (not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT):
            out.append(_problem("bad_text", field, f"fixed text of 1 to {MAX_TEXT} characters"))
        elif source == "field" and (not isinstance(value, str) or not _FIELD.match(value)):
            out.append(_problem("bad_field", field, "a registration question's id"))
    if accent is not None:
        if not d.accent:
            out.append(_problem("no_accent", "accent", f"{d.name} has no accent colour"))
        elif not isinstance(accent, str) or not _HEX.match(accent):
            out.append(_problem("bad_accent", "accent", "an accent is a colour like #1F3BD8"))
        else:
            worst = min((contrast(accent, c), c) for c in d.accent.against) if d.accent.against else None
            if worst and worst[0] < d.accent.min_contrast:
                out.append(_problem(
                    "low_contrast", "accent",
                    f"{accent.upper()} is hard to read against {worst[1]} ({worst[0]:.1f}:1; "
                    f"{d.accent.min_contrast:g}:1 or more reads well). {d.accent.note}".strip(),
                    level="warning", ratio=round(worst[0], 2),
                ))
    return out


# ─── The cache key ───────────────────────────────────────────────────────────


def fingerprint(design_id: str, values: Mapping[str, Any], options: Optional[Mapping] = None,
                bindings: Optional[Mapping] = None, accent: Optional[str] = None) -> str:
    """A short hash of everything a render of this design reads (D8): the
    template file and its manifest entry, the manifest's version, the fonts'
    lock, the fit script and filling code, the options, bindings, accent and
    values. Pass it to app.api.events.doc_cache.fingerprint as one part."""
    d = designs.get(design_id)
    blob = json.dumps(
        {
            "design": d.id, "template": d.digest, "manifest": d.version, "fonts": fonts.digest(),
            "runtime": runtime_digest(), "options": options or {}, "bindings": bindings or {},
            "accent": (accent or "").upper(), "values": dict(values or {}),
        },
        sort_keys=True, default=str, separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def tokens_of(design_id: str) -> set[str]:
    """Every token the design's template and wordings name (for the tests)."""
    d = designs.get(design_id)
    names = tokens_in(designs.compiled(d.id).body)
    for wording in (d.raw.get("wordings") or {}).values():
        for html in (wording or {}).values():
            names |= tokens_in(compile_fragment(html))
    return names
