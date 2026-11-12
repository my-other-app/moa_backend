"""A design's preset: which of the designer's designs it is, and how it is set.

    config.preset = {"id": "<kind>/<file id>", "options": {...},
                     "bindings": {...}, "accent": "#rrggbb" | null}

stored beside `config.layout` on the same template row. With `preset` set
the document is the designer's design; without it, the paths that drew the
row before (the layered canvas, else the HTML template) draw it unchanged.
Choosing a preset makes it active; choosing the custom design clears it and
leaves the layout where it was.
"""
from __future__ import annotations

import re
from typing import Optional

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


def with_default(config: dict, kind: str) -> dict:
    """A system default config with the kind's D7 preset in it."""
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
