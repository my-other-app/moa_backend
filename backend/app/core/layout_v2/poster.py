"""Posters: the fourth document kind (feature-register/posters/CONTRACT.md).

A guest's "I'm attending" poster is a layout v2 like a ticket's, drawn by the
same builder and render service, in one of three formats. A poster *design*
holds one layout per format; a starter is the same, three layouts in one file:

    {"id": "poster/01-bold-frame", "name": ..., "use": ..., "hasPhoto": true,
     "formats": {"square": <layout>, "portrait": <layout>, "story": <layout>}}

under starters/poster/. A layout's canvas is the format's pixels and its page
the same at 96 dpi, so one CSS pixel is one pixel of the PNG.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent / "starters" / "poster"
KIND = "poster"
#: The formats, in the order they are offered.
FORMATS: dict[str, tuple[int, int]] = {"square": (1080, 1080), "portrait": (1080, 1350), "story": (1080, 1920)}
ORDER = tuple(FORMATS)
LABELS = {"square": "Square", "portrait": "Portrait", "story": "Story"}
MM_PER_PX = 25.4 / 96
#: How far a saved page may be from the format's, in mm (rounding in an editor).
MM_SLACK = 0.05
#: The built-in starter every event falls back to when nothing else is there.
BLANK = "poster/blank"


class UnknownStarter(LookupError):
    pass


def page_mm(fmt: str) -> dict:
    w, h = FORMATS[fmt]
    return {"widthMm": round(w * MM_PER_PX, 4), "heightMm": round(h * MM_PER_PX, 4)}


def format_of(layout: Any) -> Optional[str]:
    """The format a layout is drawn in (its canvas and page), or None."""
    if not isinstance(layout, dict):
        try:
            from app.core.layout_v2.schema import dump

            layout = dump(layout)
        except Exception:  # noqa: BLE001
            return None
    canvas, page = layout.get("canvas") or {}, layout.get("page") or {}
    try:
        size = (round(float(canvas.get("width"))), round(float(canvas.get("height"))))
        wmm, hmm = float(page.get("widthMm")), float(page.get("heightMm"))
    except (TypeError, ValueError):
        return None
    for fmt, (w, h) in FORMATS.items():
        if size == (w, h) and abs(wmm - w * MM_PER_PX) <= MM_SLACK and abs(hmm - h * MM_PER_PX) <= MM_SLACK:
            return fmt
    return None


# ─── Starters ───────────────────────────────────────────────────────────────


def _path(starter_id: str) -> Path:
    kind, _, key = (starter_id or "").partition("/")
    if kind != KIND or not key or "/" in key or ".." in key or key.startswith("."):
        raise UnknownStarter(starter_id)
    return ROOT / f"{key}.json"


@lru_cache(maxsize=64)
def _load(starter_id: str) -> dict:
    path = _path(starter_id)
    if not path.exists():
        raise UnknownStarter(starter_id)
    data = json.loads(path.read_text())
    formats = data.get("formats") if isinstance(data, dict) else None
    if not isinstance(formats, dict) or not formats:
        raise UnknownStarter(starter_id)
    return data


def starter_ids() -> list[str]:
    """The gallery's order: the designed starters by file name, the blank last."""
    if not ROOT.exists():
        return []
    ids = sorted(f"{KIND}/{p.stem}" for p in ROOT.glob("*.json") if not p.stem.startswith("."))
    if BLANK in ids:
        ids.remove(BLANK)
        ids.append(BLANK)
    return ids


def has(starter_id: str) -> bool:
    try:
        _load(starter_id)
        return True
    except (UnknownStarter, ValueError, OSError):
        return False


def default_starter_id() -> Optional[str]:
    """The platform's poster: the first designed starter, else the blank."""
    ids = starter_ids()
    return ids[0] if ids else None


def starter_formats(starter_id: str) -> dict:
    """A copy of the starter's layouts, by format (copy-on-use)."""
    return copy.deepcopy(_load(starter_id)["formats"])


def starter(starter_id: str, fmt: str) -> Optional[dict]:
    """A copy of the starter's layout for one format, or None when it has none."""
    layout = _load(starter_id)["formats"].get(fmt)
    return copy.deepcopy(layout) if isinstance(layout, dict) else None


def meta(starter_id: str) -> dict:
    data = _load(starter_id)
    return {
        "id": starter_id, "kind": KIND, "name": data.get("name") or starter_id.split("/", 1)[1],
        "use": data.get("use") or "", "hasPhoto": bool(data.get("hasPhoto", True)),
        "formats": [f for f in ORDER if isinstance(data["formats"].get(f), dict)],
        "colour": "colour", "accent": None, "preview": data.get("preview"),
    }


def starters(*, with_layout: bool = False) -> list[dict]:
    out = []
    for sid in starter_ids():
        try:
            entry = meta(sid)
        except (UnknownStarter, ValueError, OSError):
            continue
        if with_layout:
            entry["layouts"] = starter_formats(sid)
        out.append(entry)
    return out
