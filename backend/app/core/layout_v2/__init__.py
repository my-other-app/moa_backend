"""Layout v2: the one editable design model (layout-v2 contract, V1-V6).

A v2 layout is a fixed canvas of absolutely positioned layers (text, shape,
svg, image, qr), drawn as one HTML page through the render service's
Chromium -- the same page the editor's stage shows, built by the same rules
in TypeScript (moa_web packages/layout/src/v2). The 35 designer designs are
v2 starters here (starters/), converted from their templates by
scripts/convert_designs_v2.py.

The interface:

    parse(dict) -> LayoutV2                         the wire shape (pydantic ValidationError)
    problems(layout, kind) -> [{code, field, message, level}]
    build_html(layout, values, *, context, for_browser=False, base_url=None) -> str
    build_body(layout, values, *, context) -> str   the <div class="moa-doc"> alone (fixtures)
    await render_pdf(layout, values, context) -> bytes      via app.core.doc_designs.render_pdf
    await render_pages([(layout, values, context), ...]) -> bytes   one PDF, a page each
    fingerprint(layout, values, context=None) -> str
    starters(kind) -> [{id, kind, name, use, hasPhoto, colour, accent, preview, layout}]
    starter(id) -> dict                             a copy of the starter's layout (copy-on-use)
    from_v1(v1_layout) -> dict                      a saved v1 layout, mechanically
    from_preset(preset) -> dict                     a saved config.preset: its starter, its choices applied
    fonts_available() -> [{family, weights, styles, faces}]
    tokens(kind) -> [{key, group, label, sample, kinds}]    the designs' tokens (catalogue additions)

`values` maps a token to its text (the v1 layout values merged with the
doc-presets mapping: one function, the API's). `context` (Context or a
dict) carries what is not a token: the QR payload, the brand colours and
the pictures as data: URIs.
"""
from app.core.layout_v2.schema import (
    VERSION, LayoutV2, TextLayer, ShapeLayer, SvgLayer, ImageLayer, QrLayer, Span, parse, dump, is_v2,
)
from app.core.layout_v2.context import Context
from app.core.layout_v2.fonts import fonts_available
from app.core.layout_v2.vocabulary import tokens, known_tokens
from app.core.layout_v2.validate import problems
from app.core.layout_v2.build import build_html, build_body, fingerprint
from app.core.layout_v2.render import render_pdf, render_pages
from app.core.layout_v2.gallery import starters, starter, starter_ids, default_starter_id
from app.core.layout_v2.convert import from_v1, from_preset

__all__ = [
    "VERSION", "LayoutV2", "TextLayer", "ShapeLayer", "SvgLayer", "ImageLayer", "QrLayer", "Span",
    "Context", "parse", "dump", "is_v2", "problems", "build_html", "build_body", "fingerprint",
    "render_pdf", "render_pages", "starters", "starter", "starter_ids", "default_starter_id",
    "from_v1", "from_preset", "fonts_available", "tokens", "known_tokens",
]
