"""The live room's look, per event: a preset and a few controls, never CSS.

One model that all three renderers draw (the big screen on the web, the
website, the app): colours, fonts, the answers' style, the timer, the board,
a logo and a background, and what differs between the big screen and a
phone. CONTRACT.md section 1 is the reference.

Text colours are never the organiser's: for every fill the server picks the
one that reads (WCAG 2.1 AA, 4.5:1), and refuses an accent the room could
not see on its background (3:1). An event that never saved a theme is drawn
as it always was: `custom` is false and the renderers keep their own look.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Optional

import orjson

from app.response import CustomHTTPException

PRESETS = ("meridian_light", "meridian_dark", "high_contrast", "club")
PRESET_NAMES = {"meridian_light": "Meridian light", "meridian_dark": "Meridian dark", "high_contrast": "High contrast", "club": "Club colours"}
DISPLAY_FONTS = ("dm_sans", "bebas_neue", "poppins", "space_grotesk")
BODY_FONTS = ("dm_sans", "inter", "poppins", "noto_sans")

#: Text on a fill: white, the Meridian ink, and black when neither reads.
LIGHT, DARK, BLACK = "#FFFFFF", "#111827", "#000000"
#: AA for text, and the accent's minimum against the background (non-text, large text).
TEXT_MIN = 4.5
ACCENT_MIN = 3.0
#: Below this an answer tile blends into the background (a warning only).
TILE_WARN = 1.5

ANSWERS = ["#DC2626", "#2563EB", "#D97706", "#16A34A", "#7C3AED", "#0891B2"]
HIGH_CONTRAST_ANSWERS = ["#FF6B6B", "#4DA3FF", "#FFD400", "#3DDC84", "#C792EA", "#2EE6E6"]
LIGHT_BG, DARK_BG = "#F4F5F2", "#0B0E0D"

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _base() -> dict:
    return {
        "preset": "meridian_dark",
        "colors": {"background": DARK_BG, "accent": "#4FCDB0", "answers": list(ANSWERS)},
        "font": {"display": "dm_sans", "body": "dm_sans"},
        "answers": {"style": "shapes"},
        "timer": "bar",
        "leaderboard": {"size": 5, "podium": True, "animate": True, "own_rank": True},
        "logo": None,
        "background": {"kind": "color", "gradient": None, "image": None, "dim": 0.45},
        "screen": {"text_scale": 1.0, "logo": True, "sponsors": False, "join": "corner", "answer_layout": "grid"},
        "guest": {"text_scale": 1.0, "logo": True, "background": "plain", "answer_layout": "column"},
    }


# ─── Contrast (WCAG 2.1) ─────────────────────────────────────────────────


def _channel(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def luminance(hex_: str) -> float:
    h = hex_.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def text_on(*fills: str) -> Optional[str]:
    """The text colour that reads on every one of `fills`: the better of white
    and the ink when it reaches AA on all, else black or white if one does;
    None when nothing does (a gradient from light to dark)."""
    def worst(c: str) -> float:
        return min(contrast(c, f) for f in fills)

    best = max((LIGHT, DARK), key=worst)
    if worst(best) >= TEXT_MIN:
        return best
    for c in (BLACK, LIGHT):
        if worst(c) >= TEXT_MIN:
            return c
    return None


# ─── Presets ─────────────────────────────────────────────────────────────


def preset(key: str, club=None, club_logo: Optional[dict] = None) -> dict:
    """A preset's whole theme. `club` (the club's brand colours) and its logo
    make the `club` preset."""
    t = _base()
    t["preset"] = key
    if key == "meridian_light":
        t["colors"]["background"], t["colors"]["accent"] = LIGHT_BG, "#0C7A66"
    elif key == "high_contrast":
        t["colors"] = {"background": "#000000", "accent": "#FFD400", "answers": list(HIGH_CONTRAST_ANSWERS)}
        t["timer"] = "number"
        t["screen"]["text_scale"], t["guest"]["text_scale"] = 1.15, 1.1
    elif key == "club":
        primary = _hex_or(getattr(club, "brand_primary", None), "#4FCDB0")
        bg = DARK_BG if contrast(primary, DARK_BG) >= ACCENT_MIN else LIGHT_BG
        answers = list(ANSWERS)
        secondary = _hex_or(getattr(club, "brand_secondary", None), None)
        if secondary and secondary not in answers:
            answers[0] = secondary
        t["colors"] = {"background": bg, "accent": primary, "answers": answers}
        t["logo"] = club_logo
    return t


def _hex_or(v, fallback):
    v = (v or "").strip()
    return v.upper() if _HEX.match(v) else fallback


# ─── Checking and resolving ──────────────────────────────────────────────


class Verdict:
    def __init__(self):
        self.errors: list[dict] = []
        self.warnings: list[dict] = []

    def error(self, field: str, code: str, message: str, **more) -> None:
        self.errors.append({"field": field, "code": code, "message": message, **more})

    def warn(self, field: str, code: str, message: str, **more) -> None:
        self.warnings.append({"field": field, "code": code, "message": message, **more})


def _merge(base: dict, change: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (change or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **v}
        else:
            out[k] = copy.deepcopy(v)
    return out


def _invalid(field: str, message: str) -> CustomHTTPException:
    return CustomHTTPException(422, message=message, error_code="THEME_INVALID", errors={"field": field})


def _pick(v, allowed, field):
    if v not in allowed:
        raise _invalid(field, f"{field} is one of: {', '.join(str(a) for a in allowed)}.")
    return v


def _scale(v, lo, hi, field) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo - 1e-9 <= v <= hi + 1e-9):
        raise _invalid(field, f"{field} is {lo} to {hi}.")
    return round(round(float(v) / 0.05) * 0.05, 2)


def _flag(v, field) -> bool:
    if not isinstance(v, bool):
        raise _invalid(field, f"{field} is true or false.")
    return v


def _color(v, field) -> str:
    if not isinstance(v, str) or not _HEX.match(v.strip()):
        raise _invalid(field, f"{field} is a colour as #RRGGBB.")
    return v.strip().upper()


def clean(body: dict, club=None, club_logo: Optional[dict] = None) -> tuple[dict, Verdict]:
    """The saved form of a theme from the editor's body: the chosen preset
    with the body on top, every value checked (422 THEME_INVALID). Media
    stay ids here; contrast is judged (`Verdict`), not raised."""
    key = body.get("preset") or "meridian_dark"
    if key not in PRESETS:
        raise _invalid("preset", f"preset is one of: {', '.join(PRESETS)}.")
    base = preset(key, club, None)
    t = _merge(base, {k: v for k, v in body.items() if k != "preset" and v is not None or k in ("logo",)})
    if key == "club" and "logo" not in body:
        t["logo"] = "club" if club_logo else None
    unknown = set(t) - set(base) - {"logo"}
    if unknown:
        raise _invalid(sorted(unknown)[0], f"Unknown theme field: {sorted(unknown)[0]}.")
    c = t.get("colors") or {}
    answers = c.get("answers")
    if not isinstance(answers, list) or len(answers) != 6:
        raise _invalid("colors.answers", "Give exactly six answer colours.")
    out = {
        "preset": key,
        "colors": {
            "background": _color(c.get("background"), "colors.background"),
            "accent": _color(c.get("accent"), "colors.accent"),
            "answers": [_color(a, f"colors.answers.{i}") for i, a in enumerate(answers)],
        },
        "font": {
            "display": _pick((t.get("font") or {}).get("display"), DISPLAY_FONTS, "font.display"),
            "body": _pick((t.get("font") or {}).get("body"), BODY_FONTS, "font.body"),
        },
        "answers": {"style": _pick((t.get("answers") or {}).get("style"), ("shapes", "letters", "plain"), "answers.style")},
        "timer": _pick(t.get("timer"), ("bar", "ring", "number", "hidden"), "timer"),
        "leaderboard": {
            "size": _pick((t.get("leaderboard") or {}).get("size"), (5, 10), "leaderboard.size"),
            "podium": _flag((t.get("leaderboard") or {}).get("podium"), "leaderboard.podium"),
            "animate": _flag((t.get("leaderboard") or {}).get("animate"), "leaderboard.animate"),
            "own_rank": _flag((t.get("leaderboard") or {}).get("own_rank"), "leaderboard.own_rank"),
        },
        "logo": _media_ref(t.get("logo"), "logo"),
        "background": _background(t.get("background") or {}),
        "screen": {
            "text_scale": _scale((t.get("screen") or {}).get("text_scale"), 0.8, 1.6, "screen.text_scale"),
            "logo": _flag((t.get("screen") or {}).get("logo"), "screen.logo"),
            "sponsors": _flag((t.get("screen") or {}).get("sponsors"), "screen.sponsors"),
            "join": _pick((t.get("screen") or {}).get("join"), ("corner", "lobby", "hidden"), "screen.join"),
            "answer_layout": _pick((t.get("screen") or {}).get("answer_layout"), ("grid", "column"), "screen.answer_layout"),
        },
        "guest": {
            "text_scale": _scale((t.get("guest") or {}).get("text_scale"), 0.9, 1.3, "guest.text_scale"),
            "logo": _flag((t.get("guest") or {}).get("logo"), "guest.logo"),
            "background": _pick((t.get("guest") or {}).get("background"), ("plain", "theme"), "guest.background"),
            "answer_layout": _pick((t.get("guest") or {}).get("answer_layout"), ("grid", "column"), "guest.answer_layout"),
        },
    }
    for k in ("screen", "guest", "leaderboard", "font", "answers"):
        extra = set((t.get(k) or {})) - set(out[k])
        if extra:
            raise _invalid(f"{k}.{sorted(extra)[0]}", f"Unknown theme field: {k}.{sorted(extra)[0]}.")
    return out, judge(out)


def _media_ref(v, field) -> Optional[str]:
    if v is None:
        return None
    if v == "club":
        return "club"
    if isinstance(v, dict) and v.get("id"):
        v = v["id"]
    if not isinstance(v, str) or not re.match(r"^[0-9a-fA-F-]{32,36}$", v):
        raise CustomHTTPException(422, message="Pick a picture uploaded for this event.", error_code="THEME_MEDIA", errors={"field": field})
    return v.lower()


def _background(b: dict) -> dict:
    kind = _pick(b.get("kind"), ("color", "gradient", "image"), "background.kind")
    g = b.get("gradient")
    gradient = None
    if g is not None:
        if not isinstance(g, dict):
            raise _invalid("background.gradient", "A gradient has from, to and angle.")
        angle = g.get("angle", 180)
        if isinstance(angle, bool) or not isinstance(angle, int) or not (0 <= angle <= 359):
            raise _invalid("background.gradient.angle", "A gradient's angle is 0 to 359.")
        gradient = {"from": _color(g.get("from"), "background.gradient.from"), "to": _color(g.get("to"), "background.gradient.to"), "angle": angle}
    image = _media_ref(b.get("image"), "background.image")
    if kind == "gradient" and gradient is None:
        raise _invalid("background.gradient", "Pick the gradient's two colours.")
    if kind == "image" and image is None:
        raise _invalid("background.image", "Pick a background picture.")
    dim = _scale(b.get("dim", 0.45), 0.3, 0.8, "background.dim")
    return {"kind": kind, "gradient": gradient, "image": image, "dim": dim}


def judge(t: dict) -> Verdict:
    """The contrast rules on a cleaned theme: errors block saving, warnings do not."""
    v = Verdict()
    bg, accent = t["colors"]["background"], t["colors"]["accent"]
    ratio = round(contrast(accent, bg), 2)
    if ratio < ACCENT_MIN:
        v.error("colors.accent", "THEME_CONTRAST", f"The accent needs more contrast with the background ({ratio}:1, at least 3:1).", ratio=ratio)
    answers = [a.upper() for a in t["colors"]["answers"]]
    if len(set(answers)) != len(answers):
        v.error("colors.answers", "THEME_CONTRAST", "Give every answer its own colour.")
    letters = "ABCDEF"
    for i, a in enumerate(answers):
        r = round(contrast(a, bg), 2)
        if r < TILE_WARN:
            v.warn(f"colors.answers.{i}", "ANSWER_BLENDS", f"Answer {letters[i]}'s colour is close to the background.", ratio=r)
    g = t["background"].get("gradient") if t["background"]["kind"] == "gradient" else None
    if g is not None:
        if text_on(g["from"], g["to"]) is None:
            v.error("background.gradient", "THEME_CONTRAST", "Text cannot be read on both ends of this gradient; bring its colours closer.")
        for end in ("from", "to"):
            r = round(contrast(accent, g[end]), 2)
            if r < TILE_WARN:
                v.warn(f"background.gradient.{end}", "ACCENT_ON_GRADIENT", "The accent is hard to see on this end of the gradient.", ratio=r)
    return v


def resolve(saved: dict, media: dict, club_logo: Optional[dict]) -> dict:
    """The theme as the renderers get it: the computed text colours added and
    media ids turned into pictures (`media`: id -> Media dict). A picture that
    is gone shows none (a background image falls back to its colour)."""
    t = copy.deepcopy(saved)
    logo = t.get("logo")
    t["logo"] = club_logo if logo == "club" else media.get(logo) if logo else None
    bg = t["background"]
    bg["image"] = media.get(bg["image"]) if bg.get("image") else None
    if bg["kind"] == "image" and bg["image"] is None:
        bg["kind"] = "color"
    c = t["colors"]
    fills = (bg["gradient"]["from"], bg["gradient"]["to"]) if bg["kind"] == "gradient" and bg.get("gradient") else (c["background"],)
    t["text"] = {
        "on_background": text_on(*fills) or text_on(c["background"]) or LIGHT,
        "on_accent": text_on(c["accent"]) or LIGHT,
        "on_answers": [text_on(a) or LIGHT for a in c["answers"]],
    }
    return t


def version(theme: dict, custom: bool) -> str:
    return hashlib.sha256(orjson.dumps({"c": custom, "t": theme}, option=orjson.OPT_SORT_KEYS, default=str)).hexdigest()[:8]


def default_saved() -> dict:
    """The theme an event that never saved one is described by (the editor's start)."""
    t, _ = clean({"preset": "meridian_dark"})
    return t
