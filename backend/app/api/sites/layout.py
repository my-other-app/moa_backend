"""How a club arranges its template: the movable sections' order, which are
hidden, their titles, which library photo goes in which image slot, the
accent colour, and the join section's line.

Stored on club_sites.layout as

    {"order": [section, ...], "hidden": [section, ...], "titles": {section: title},
     "slots": {slot: image id}, "accent": null | "#rrggbb", "join_text": null | str}

`order` and `hidden` are kept for the current template (switching carries
the shared sections' order over); `titles` and `slots` are kept by key
whatever the template, so a club that tries another template and comes back
finds its titles and photos where it left them. What is read is always
`normalise(stored, package)`: valid for that package, whatever is stored.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.api.sites.registry import CANONICAL, FRAME, SLOT_KEY, TITLE_MAX, Package
from app.response import CustomHTTPException

_HEX = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
#: The join section's line, as the club words it (what membership gives, if anything).
JOIN_TEXT_MAX = 240
#: What the join section says when the club wrote nothing: true of every club.
DEFAULT_JOIN_TEXT = "Joining is free. Follow us to hear about every event first."


def hex_colour(v: Any) -> Optional[str]:
    """`#rrggbb` in lower case, from #rgb or #rrggbb; None for anything else."""
    if not isinstance(v, str) or not _HEX.match(v.strip()):
        return None
    h = v.strip().lower()
    if len(h) == 4:
        h = "#" + "".join(c * 2 for c in h[1:])
    return h


def _plain(v: Any) -> Optional[str]:
    """One line of plain text: whitespace runs as one space, trimmed; None when empty."""
    if not isinstance(v, str):
        return None
    return " ".join(v.split()) or None


def _title(v: Any) -> Optional[str]:
    v = _plain(v)
    return v[:TITLE_MAX] if v else None


def normalise(stored: Optional[dict], pkg: Package) -> dict:
    """The layout as it applies to `pkg`: `order` a permutation of its
    movable sections (unknown ones dropped, missing ones appended in its
    default order), `hidden` within its hideable ones, `titles` and `slots`
    for its own keys only, `accent` a colour or None, `join_text` the
    club's line or None."""
    stored = stored if isinstance(stored, dict) else {}
    movable = pkg.movable
    order = [k for k in dict.fromkeys(stored.get("order") or []) if k in movable]
    order += [k for k in movable if k not in order]
    hidden = [k for k in order if k in set(stored.get("hidden") or []) and k in pkg.hideable]
    titles = {}
    for k, v in (stored.get("titles") or {}).items():
        t = _title(v)
        if k in movable and t:
            titles[k] = t
    slots = {}
    for k, v in (stored.get("slots") or {}).items():
        if k in pkg.slot_keys and isinstance(v, int) and not isinstance(v, bool):
            slots[k] = v
    join = _plain(stored.get("join_text"))
    return {
        "order": order, "hidden": hidden, "titles": titles, "slots": slots, "accent": hex_colour(stored.get("accent")),
        "join_text": join[:JOIN_TEXT_MAX] if join else None,
    }


def switch(stored: Optional[dict], old: Optional[Package], new: Package) -> dict:
    """The stored layout after a switch from `old` to `new`: the sections
    both have keep their order (the new template's others follow in its
    default order), hidden ones stay hidden where the new one lets them;
    titles, slots and the accent are kept by key."""
    stored = dict(stored or {})
    current = normalise(stored, old)["order"] if old is not None else list(stored.get("order") or [])
    order = [k for k in current if k in new.movable]
    order += [k for k in new.movable if k not in order]
    hidden = [k for k in order if k in set(stored.get("hidden") or []) and k in new.hideable]
    return {**stored, "order": order, "hidden": hidden}


def apply(stored: Optional[dict], patch: dict, pkg: Package, image_ids: set[int]) -> tuple[dict, dict]:
    """`patch` (the PATCH body's `layout`, only the fields sent) applied to
    the stored layout for `pkg`: (the layout to store, what was adjusted).

    - order: replaces the order. Unknown sections are dropped and the
      template's missing ones appended in its default order; both are said
      in `adjusted` (order_dropped, order_appended).
    - hidden: replaces the hidden set. An unknown section is dropped (said);
      one the template does not let be hidden is a 422.
    - titles: merged by section; null or "" goes back to the template's
      title. At most 60 characters (a 422 past it); a section the template
      does not have is dropped (said).
    - slots: merged by slot; null empties it. A slot the template does not
      have, or a photo that is not in the club's library, is a 422.
    - accent: a #rrggbb colour (#rgb accepted), or null for the template's own.
    - join_text: the join section's line, plain text up to 240 characters
      (a 422 past it); null or "" for the default (DEFAULT_JOIN_TEXT).
    """
    out = dict(stored or {})
    adjusted: dict[str, list[str]] = {}
    current = normalise(stored, pkg)
    if "order" in patch and patch["order"] is not None:
        sent = [k for k in dict.fromkeys(patch["order"]) if isinstance(k, str)]
        order = [k for k in sent if k in pkg.movable]
        dropped = [k for k in sent if k not in pkg.movable]
        appended = [k for k in pkg.movable if k not in order]
        out["order"] = order + appended
        if dropped:
            adjusted["order_dropped"] = dropped
        if appended:
            adjusted["order_appended"] = appended
    else:
        out["order"] = current["order"]
    if "hidden" in patch and patch["hidden"] is not None:
        sent = [k for k in dict.fromkeys(patch["hidden"]) if isinstance(k, str)]
        fixed = [k for k in sent if k in pkg.movable and k not in pkg.hideable]
        frame = [k for k in sent if k in FRAME or k in pkg.fixed_top + pkg.fixed_side + pkg.fixed_bottom]
        if fixed or frame:
            names = ", ".join(fixed + frame)
            raise CustomHTTPException(422, message=f"This template always shows: {names}.", error_code="SITE_SECTION_NOT_HIDEABLE", errors={"hidden": fixed + frame})
        unknown = [k for k in sent if k not in pkg.movable]
        out["hidden"] = [k for k in sent if k in pkg.hideable]
        if unknown:
            adjusted["hidden_dropped"] = unknown
    else:
        out["hidden"] = current["hidden"]
    if patch.get("titles") is not None:
        titles = {k: v for k, v in ((stored or {}).get("titles") or {}).items() if k in CANONICAL}
        dropped = []
        for k, v in patch["titles"].items():
            if k not in pkg.movable:
                dropped.append(k)
                continue
            if v is not None and not isinstance(v, str):
                raise CustomHTTPException(422, message="A section's title is text.", error_code="SITE_TITLE_INVALID", errors={"titles": [k]})
            if isinstance(v, str) and len(" ".join(v.split())) > TITLE_MAX:
                raise CustomHTTPException(422, message=f"A section's title is at most {TITLE_MAX} characters.", error_code="SITE_TITLE_TOO_LONG", errors={"titles": [k]})
            t = _title(v)
            if t:
                titles[k] = t
            else:
                titles.pop(k, None)
        out["titles"] = titles
        if dropped:
            adjusted["titles_dropped"] = dropped
    if patch.get("slots") is not None:
        slots = {k: v for k, v in ((stored or {}).get("slots") or {}).items() if SLOT_KEY.match(k) and isinstance(v, int)}
        bad_slots = [k for k in patch["slots"] if k not in pkg.slot_keys]
        if bad_slots:
            raise CustomHTTPException(422, message=f"This template has no slot {', '.join(bad_slots)}.", error_code="SITE_SLOT_UNKNOWN", errors={"slots": bad_slots})
        bad_images = [k for k, v in patch["slots"].items() if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v not in image_ids)]
        if bad_images:
            raise CustomHTTPException(422, message="That photo is not in the club's library.", error_code="SITE_IMAGE_NOT_FOUND", errors={"slots": bad_images})
        for k, v in patch["slots"].items():
            if v is None:
                slots.pop(k, None)
            else:
                slots[k] = v
        out["slots"] = slots
    if "accent" in patch:
        v = patch["accent"]
        if v is None or v == "":
            out["accent"] = None
        else:
            colour = hex_colour(v)
            if colour is None:
                raise CustomHTTPException(422, message="The accent is a colour like #2b4bff.", error_code="SITE_ACCENT_INVALID", errors={"accent": v})
            out["accent"] = colour
    if "join_text" in patch:
        v = patch["join_text"]
        if v is not None and not isinstance(v, str):
            raise CustomHTTPException(422, message="The join text is text.", error_code="SITE_JOIN_TEXT_INVALID")
        line = _plain(v)
        if line and len(line) > JOIN_TEXT_MAX:
            raise CustomHTTPException(422, message=f"The join text is at most {JOIN_TEXT_MAX} characters.", error_code="SITE_JOIN_TEXT_TOO_LONG", errors={"join_text": len(line)})
        out["join_text"] = line
    return out, adjusted


def without_image(stored: Optional[dict], image_id: int) -> tuple[dict, bool]:
    """The stored layout with `image_id` taken out of every slot (in every
    template's keys); whether it was in any."""
    stored = dict(stored or {})
    slots = dict(stored.get("slots") or {})
    kept = {k: v for k, v in slots.items() if v != image_id}
    stored["slots"] = kept
    return stored, len(kept) != len(slots)
