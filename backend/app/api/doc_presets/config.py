"""A design's preset: which of the designer's designs it is, and how it is set.

    config.preset = {"id": "<kind>/<file id>", "options": {...},
                     "bindings": {...}, "accent": "#rrggbb" | null}

stored beside `config.layout` on the same template row. With `preset` set
the document is the designer's design; without it, the paths that drew the
row before (the layered canvas, else the HTML template) draw it unchanged.
Choosing a preset makes it active; choosing the custom design clears it and
leaves the layout where it was.

**Layout v2** (feature-register/layout-v2-contract.md, V4). `config.layout`
may hold an editable v2 layout (`"version": 2`, app.core.layout_v2) instead
of a v1 canvas. Which one a row holds is `is_v2`; what is drawn, in order:

    config.preset set           the designer's design (the preset path)
    config.layout is v2         the v2 layout, through the render service
    config.layout is v1         the v1 canvas, by reportlab, unchanged
    neither                     the HTML template

Saving a v2 layout keeps the v1 canvas it replaced under `layout_v1`, so a
certificate issued before it (pinned "legacy" or "custom") and the D9
fallback can still draw it (`classic`), and counts its revision under
`layout_revision`: a v1 canvas's `version` was its revision, a v2 layout's
`version` is the schema's 2.

The defaults (D7, V4) are the v2 starters of the three default designs
(`default_layout`), so the editor opens on exactly what prints.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

#: Where a saved v2 layout keeps the v1 canvas it replaced, and its revision.
V1_KEPT = "layout_v1"
REVISION = "layout_revision"

#: The kinds that have designer designs. The programme (agenda) has none.
KINDS = ("ticket", "certificate", "badge")

#: D7: what a document is when nothing is saved, and what the editor opens on.
DEFAULT_IDS = {
    "ticket": "ticket/03-daylight-boarding-pass",
    "certificate": "certificate/07-swiss-minimal",
    "badge": "badge/06-thermal-mono",
}

#: Options and bindings together, as JSON, at most.
SIZE_MAX = 20_000

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def default_preset(kind: str) -> Optional[dict]:
    """The code default for a kind (D7), as a stored preset; None for agenda."""
    design_id = DEFAULT_IDS.get(kind)
    if design_id is None:
        return None
    return {"id": design_id, "options": {}, "bindings": {}, "accent": None}


def preset_of(style) -> Optional[dict]:
    """The active preset of a design, normalised, or None (a custom or HTML design)."""
    if not isinstance(style, dict):
        return None
    raw = style.get("preset")
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw.get("id"):
        return None
    return {
        "id": raw["id"],
        "options": dict(raw.get("options") or {}),
        "bindings": dict(raw.get("bindings") or {}),
        "accent": raw.get("accent") or None,
    }


def kind_of(design_id: str) -> str:
    return (design_id or "").split("/", 1)[0]


def without_preset(style: dict) -> dict:
    """The design as the paths before presets read it: the preset left out."""
    return {k: v for k, v in (style or {}).items() if k != "preset"}


def is_v2(layout) -> bool:
    """Whether a stored layout is a v2 layout. Its `version` says 2, and its
    shape does too: a v1 canvas counted its saves in `version` (so a v1 saved
    twice says 2 as well), but its background is always a hex string, where a
    v2 layout's is a fill or null, and it never has `vars`."""
    if not isinstance(layout, dict) or layout.get("version") != 2 or not isinstance(layout.get("layers"), list):
        return False
    canvas = layout.get("canvas") if isinstance(layout.get("canvas"), dict) else {}
    return "vars" in layout or not isinstance(canvas.get("background"), str)


def layout_v2_of(style) -> Optional[dict]:
    """The v2 layout a design holds, or None (a v1 canvas, a template, nothing)."""
    layout = style.get("layout") if isinstance(style, dict) else None
    return layout if is_v2(layout) else None


def layout_v1_of(style) -> Optional[dict]:
    """The v1 canvas a design holds or kept when a v2 layout replaced it."""
    if not isinstance(style, dict):
        return None
    for key in ("layout", V1_KEPT):
        layout = style.get(key)
        if isinstance(layout, dict) and layout.get("layers") and not is_v2(layout):
            return layout
    return None


def revision_of(style) -> int:
    """A v2 layout's revision: the saves of the design's layout, v1 and v2."""
    try:
        return int((style or {}).get(REVISION) or 0)
    except (TypeError, ValueError):
        return 0


def classic(style: dict) -> dict:
    """The design as the classic renderer draws it: no preset and no v2
    layout -- the v1 canvas it replaced is back in its place, else the HTML
    template draws it. The D9 fallback of every ticket and certificate, and
    what a certificate issued before the design was a v2 one keeps."""
    out = {k: v for k, v in (style or {}).items() if k not in ("preset", REVISION)}
    if is_v2(out.get("layout")):
        out.pop("layout")
    kept = out.pop(V1_KEPT, None)
    if "layout" not in out and isinstance(kept, dict) and kept.get("layers"):
        out["layout"] = kept
    return out


def with_layout(config: dict, layout: dict) -> dict:
    """A config saving this v2 layout (already checked): the preset cleared
    (V4), the v1 canvas it replaces kept, the revision one up."""
    out = dict(config or {})
    previous = out.get("layout")
    if isinstance(previous, dict) and previous.get("layers") and not is_v2(previous):
        out[V1_KEPT] = previous
        before = int(previous.get("version") or 0)
    else:
        before = revision_of(out)
    out["layout"] = {**layout, "version": 2}
    out[REVISION] = max(before, revision_of(out)) + 1
    out.pop("preset", None)
    return out


def with_v1_layout(config: dict, layout: dict) -> dict:
    """A config saving this v1 canvas (the old editor's save): its version
    one past the design's last revision -- never past a v2 layout's schema
    number -- the preset cleared (D6), no v2 layout or kept canvas left."""
    out = dict(config or {})
    previous = out.get("layout")
    before = revision_of(out) if is_v2(previous) else int((previous or {}).get("version") or 0) if isinstance(previous, dict) else 0
    out["layout"] = {**layout, "version": before + 1}
    for key in ("preset", V1_KEPT, REVISION):
        out.pop(key, None)
    return out


def default_layout(kind: str) -> Optional[dict]:
    """The v2 starter of the kind's default design (D7, V4), a copy of its
    own; None for agenda, or when this build has no such starter (the D7
    preset is the default then)."""
    design_id = DEFAULT_IDS.get(kind)
    if design_id is None:
        return None
    try:
        from app.core import layout_v2

        # A copy of its own every time (P's starter() copies).
        return layout_v2.starter(design_id)
    except LookupError:
        return None
    except Exception:  # noqa: BLE001 - a broken starter: the preset default stands
        logger.warning("the v2 starter of the default %s design %s could not be read", kind, design_id, exc_info=True)
        return None


def with_default(config: dict, kind: str) -> dict:
    """A system default config: the kind's default design as its v2 starter
    (V4: the club's first edit starts from exactly what prints), any v1
    canvas in it kept beside; else the D7 preset."""
    layout = default_layout(kind)
    if layout is not None:
        out = dict(config)
        previous = out.get("layout")
        if isinstance(previous, dict) and previous.get("layers") and not is_v2(previous):
            out[V1_KEPT] = previous
        out["layout"] = {**layout, "version": 2}
        return out
    preset = default_preset(kind)
    return {**config, "preset": preset} if preset else dict(config)


def shape_problems(kind: str, body: dict) -> dict:
    """What is wrong with a preset's shape as sent, by field; empty when it
    is an object of the right parts. What the options and bindings mean is
    the design's to say (engine.problems: A's validate, against the design's
    own schema -- a `text` option is a map of line to words, a binding is
    {source, value?})."""
    out: dict = {}
    design_id = body.get("id")
    if not isinstance(design_id, str) or kind_of(design_id) != kind:
        out["id"] = f"Pick one of the {kind} designs."
    options, bindings = body.get("options"), body.get("bindings")
    if options is not None and not isinstance(options, dict):
        out["options"] = "Options are an object."
    if bindings is not None and not isinstance(bindings, dict):
        out["bindings"] = "Bindings are an object."
    elif isinstance(bindings, dict):
        for token, binding in bindings.items():
            if not isinstance(binding, dict):
                out[f"bindings.{token}"] = "A binding is {source, value}."
    accent = body.get("accent")
    if accent not in (None, "") and (not isinstance(accent, str) or not _HEX.match(accent)):
        out["accent"] = "A hex colour like #1db9a0."
    if not out:
        import json

        if len(json.dumps([options or {}, bindings or {}], default=str)) > SIZE_MAX:
            out["options"] = "This design's settings are too large."
    return out


def normalised(body: dict) -> dict:
    """The preset as it is stored (after shape_problems found nothing): the
    options and bindings exactly as sent -- A's fill reads them as they are --
    and the accent in upper case, as A writes it, or null for the design's own."""
    accent = body.get("accent") or None
    return {
        "id": body["id"],
        "options": dict(body.get("options") or {}),
        "bindings": dict(body.get("bindings") or {}),
        "accent": accent.upper() if isinstance(accent, str) else None,
    }
