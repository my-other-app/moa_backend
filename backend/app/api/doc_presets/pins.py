"""The design an issued certificate keeps (contract §6).

A certificate already in someone's hands never changes design on its own:
not when the club picks another one, and not when the defaults change.
`event_registrations_link.certificate_design` holds what it was issued with:

    {"mode": "legacy"}   issued before the designer's designs (the migration
                         certificate_design marked every one): drawn as it
                         was then -- the event's design without a preset,
                         through the layered or HTML paths, unchanged.
    {"mode": "custom", "version": n}
                         issued while the event's design was a custom one:
                         drawn the same way.
    {"mode": "preset", "preset": {...}, "images": {...}}
                         issued with one of the designer's designs: that
                         design, with these options, bindings and accent,
                         and the logo and signatures it had then.
    {"mode": "layout", "layout": {...v2...}, "revision": n, "images": {...}}
                         issued with an editable (v2) layout: a snapshot of
                         the layout itself (layout v2 contract, Q), and the
                         logo and signatures its design row had then. A
                         later save of the layout changes no issued one.

A certificate with none (issued by code before the column, in the moment a
deploy takes) is a legacy one. A guest not yet issued one has no pin; the
designer's previews draw the event's current design.

"legacy" and "custom" are drawn by the classic paths (config.classic): when
the event's design has become a v2 layout since, the v1 canvas it replaced
(kept beside it) or the HTML template draws them, as they were.

The club's "Apply the current design to issued certificates" (per event,
confirmed) writes the current design over every issued certificate's pin.
"""
from __future__ import annotations

import json
from typing import Optional

from app.api.doc_presets.config import REVISION, classic, layout_v2_of, preset_of, revision_of, without_preset

#: The images a preset certificate prints from its design row, kept with its pin.
PINNED_IMAGES = ("logo_key", "signature_1_key", "signature_2_key")
LEGACY = {"mode": "legacy"}


def _images(style: dict) -> dict:
    from app.api.templates_images import key_of

    return {f"{f}_key": key_of(style, f) for f in ("logo", "signature_1", "signature_2")}


def snapshot(style: dict) -> dict:
    """What a certificate issued now keeps, from the design it resolves to."""
    preset = preset_of(style)
    if preset is not None:
        return {"mode": "preset", "preset": preset, "images": _images(style)}
    layout = layout_v2_of(style)
    if layout is not None:
        return {"mode": "layout", "layout": layout, "revision": revision_of(style), "images": _images(style)}
    from app.api.events.tickets import design_version_of

    return {"mode": "custom", "version": design_version_of(style)}


def as_text(pin: dict) -> str:
    """A pin as the JSON text it is stored as (compared as text in SQL)."""
    return json.dumps(pin, sort_keys=True, separators=(",", ":"), default=str)


def pin_of(registration) -> Optional[dict]:
    """The registration's pin, or None when it holds no certificate."""
    if getattr(registration, "certificate_issued_at", None) is None:
        return None
    pin = getattr(registration, "certificate_design", None)
    return pin if isinstance(pin, dict) and pin.get("mode") else LEGACY


def pinned_style(style: dict, registration) -> dict:
    """The design this registration's certificate is drawn from, given the
    design its event resolves to now."""
    pin = pin_of(registration)
    if pin is None:
        return style
    if pin.get("mode") == "preset" and isinstance(pin.get("preset"), dict) and pin["preset"].get("id"):
        out = without_preset(style)
        for key in PINNED_IMAGES:
            out[key] = (pin.get("images") or {}).get(key)
        out["preset"] = pin["preset"]
        return out
    if pin.get("mode") == "layout" and layout_v2_of(pin) is not None:
        # Its own snapshot; the D9 fallback (classic) is the event's as now.
        out = without_preset(style)
        for key in PINNED_IMAGES:
            out[key] = (pin.get("images") or {}).get(key)
        out["layout"] = pin["layout"]
        out[REVISION] = pin.get("revision") or 0
        return out
    return classic(style)
