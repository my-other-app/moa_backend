"""The v2 starters: the 35 designer designs as editable layouts, the CR80
card and a blank canvas per kind (layout-v2 contract, V3).

Each is a JSON file under starters/{ticket,certificate,badge}/, written by
scripts/convert_designs_v2.py (the 35 by walking each template's rendered
page in Chromium; card_cr80 and the blanks from the v1 starters through
from_v1). A starter's id is "<kind>/<file id>": the 35 share their
doc_designs ids ("badge/06-thermal-mono"), and "badge/card_cr80",
"ticket/blank", "certificate/blank", "badge/blank" are the others.

Certificates have their wordings too: starters/certificate/wordings/
<id>.<wording>.json is the same design converted with that wording (a
saved preset that chose it opens on that one: from_preset).

Picking one copies it into the club's design (copy-on-use): `starter(id)`
returns a fresh dict every time.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.core import doc_designs

ROOT = Path(__file__).resolve().parent / "starters"
KINDS = ("ticket", "certificate", "badge")
EXTRA = {
    "ticket": ("blank",),
    "certificate": ("blank",),
    "badge": ("card_cr80", "blank"),
}
EXTRA_META = {
    "card_cr80": {"name": "Card (CR80)", "use": "For card printers: brand head, photo in a ring, first name large, type band with the QR"},
    "blank": {"name": "Blank", "use": "An empty canvas: the guest's name and the QR"},
}


class UnknownStarter(LookupError):
    pass


def _path(starter_id: str, wording: Optional[str] = None) -> Path:
    kind, _, key = starter_id.partition("/")
    if kind not in KINDS or not key or "/" in key or ".." in key:
        raise UnknownStarter(starter_id)
    if wording:
        return ROOT / kind / "wordings" / f"{key}.{wording}.json"
    return ROOT / kind / f"{key}.json"


@lru_cache(maxsize=128)
def _load(starter_id: str, wording: Optional[str] = None) -> dict:
    path = _path(starter_id, wording)
    if not path.exists():
        raise UnknownStarter(starter_id if not wording else f"{starter_id} ({wording})")
    return json.loads(path.read_text())


def starter_ids(kind: str) -> list[str]:
    """The starters of a kind, in the gallery's order: the designer's, then the others."""
    if kind not in KINDS:
        return []
    ids = [d.id for d in doc_designs.catalogue(kind)]
    ids += [f"{kind}/{k}" for k in EXTRA[kind]]
    return [i for i in ids if _path(i).exists()]


def starter(starter_id: str, wording: Optional[str] = None) -> dict:
    """A copy of the starter's layout (v2 wire form). `wording`: a certificate's
    other wording, when it has one converted (else the default)."""
    if wording:
        try:
            return copy.deepcopy(_load(starter_id, wording))
        except UnknownStarter:
            pass
    return copy.deepcopy(_load(starter_id))


def has(starter_id: str) -> bool:
    try:
        return _path(starter_id).exists()
    except UnknownStarter:
        return False


def default_starter_id(kind: str) -> Optional[str]:
    """The kind's default (doc-designs D7) as a starter."""
    d = doc_designs.DEFAULTS.get(kind)
    return d if d and has(d) else None


def meta(starter_id: str, base_url: Optional[str] = None) -> dict:
    kind, _, key = starter_id.partition("/")
    if key in EXTRA_META:
        m = EXTRA_META[key]
        return {"id": starter_id, "kind": kind, "name": m["name"], "use": m["use"], "hasPhoto": key == "card_cr80",
                "colour": "colour", "accent": None, "preview": None}
    d = doc_designs.get(starter_id)
    return {"id": d.id, "kind": d.kind, "name": d.name, "use": d.use, "hasPhoto": d.has_photo, "colour": d.colour,
            "accent": d.accent.to_dict() if d.accent else None, "preview": d.preview_url(base_url)}


def starters(kind: str, *, base_url: Optional[str] = None, with_layout: bool = True) -> list[dict]:
    """[{id, kind, name, use, hasPhoto, colour, accent, preview, layout}] for the gallery."""
    out = []
    for sid in starter_ids(kind):
        entry = meta(sid, base_url)
        if with_layout:
            entry["layout"] = starter(sid)
        out.append(entry)
    return out
