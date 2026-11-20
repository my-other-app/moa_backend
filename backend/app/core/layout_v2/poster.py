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
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

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


# ─── Contact details: never on a poster ─────────────────────────────────────
#
# A poster is a picture its guest shares in public. Nothing on it -- a text
# layer, a caption -- may print an email address or a phone number: the v1
# {{email}}, or a form answer (field.<slug>) to an email or phone question,
# or one whose name says it is one. Refused when a design or a caption is
# saved (CONTRACT.md §2), left out of the board's tokens, and never among the
# values a poster is built from.

#: Token names that are contact details, whatever form asked for them.
CONTACT_TOKENS = frozenset({
    "email", "email_address", "e_mail", "mail", "phone", "phone_number", "mobile", "mobile_number", "mobile_no", "phone_no",
    "whatsapp", "whatsapp_number", "telephone", "tel", "contact", "contact_number", "contact_no", "cell",
})
#: A form question's slug that names a contact detail.
_CONTACT_SLUG = re.compile(r"(?:^|_)(?:e_?mail|email_?id|mail_?id|phone|mobile|whats_?app|wa|tel|telephone|cell|contact)(?:_|$|s?_?(?:no|num|number))")
#: Form question types whose answers are contact details (events.schemas.FieldTypes).
CONTACT_FIELD_TYPES = frozenset({"email", "tel"})


def contact_field_slugs(form: Optional[Iterable]) -> frozenset[str]:
    """The field.<slug>s (of each question's key and label) of a form's
    email and phone questions."""
    from app.core.layout.tokens import field_slug

    out = set()
    for f in form or []:
        if not isinstance(f, dict):
            continue
        kind = f.get("field_type")
        kind = getattr(kind, "value", kind)
        if kind in CONTACT_FIELD_TYPES:
            for name in (f.get("key"), f.get("label")):
                if name:
                    out.add(field_slug(str(name)))
    return frozenset(out)


def is_contact_token(name: str, slugs: Iterable[str] = ()) -> bool:
    """Whether a token (as written, or its canonical key) is a contact detail."""
    from app.core.layout.tokens import BY_NAME

    n = (name or "").strip().lower()
    if not n:
        return False
    if n in CONTACT_TOKENS or BY_NAME.get(n) == "email":
        return True
    if n.startswith("field."):
        slug = n[len("field."):]
        return slug in set(slugs) or bool(_CONTACT_SLUG.search(slug))
    return False


_TAG_NAMES = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)|\{%\s*if\s+(?:not\s+)?(?:\{\{\s*)?([A-Za-z0-9_.]+)", re.I)


def contact_tokens_in(src: str, slugs: Iterable[str] = ()) -> list[str]:
    """The contact tokens a template names ({{x}}, {{x | upper}}, {% if x %}), in order."""
    out = []
    for m in _TAG_NAMES.finditer(src or ""):
        name = (m.group(1) or m.group(2) or "").strip()
        if is_contact_token(name, slugs) and name not in out:
            out.append(name)
    return out


def contact_problems(layout: Any, slugs: Iterable[str] = ()) -> list[dict]:
    """A save-time error for each layer whose words (or showIf) name a contact detail."""
    if not isinstance(layout, dict):
        from app.core.layout_v2.schema import dump

        layout = dump(layout)
    slugs = frozenset(slugs)
    out = []
    for i, layer in enumerate(layout.get("layers") or []):
        if not isinstance(layer, dict):
            continue
        found: list[str] = []
        if layer.get("kind") == "text":
            sources = [s.get("text") or "" for s in layer.get("spans") or [] if isinstance(s, dict)] or [layer.get("text") or ""]
            for src in sources:
                found += [t for t in contact_tokens_in(src, slugs) if t not in found]
        for name in (layer.get("showIf") or "").replace("|", " ").split():
            if is_contact_token(name, slugs) and name not in found:
                found.append(name)
        if found:
            name = layer.get("name") or layer.get("id") or "A layer"
            out.append({
                "code": "poster_contact", "field": f"layers.{i}.text", "level": "error", "layer": layer.get("id"),
                "message": f"{name}: a poster is shared in public, so it cannot show an email address or phone number "
                           f"({', '.join('{{' + t + '}}' for t in found)}).",
            })
    return out


def without_contacts(values: dict, slugs: Iterable[str] = ()) -> dict:
    """The values with every contact detail taken out (and its showIf with it)."""
    slugs = frozenset(slugs)
    return {k: v for k, v in values.items() if not is_contact_token(k, slugs)}


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
